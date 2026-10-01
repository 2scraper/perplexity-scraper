"""Audit regressions: exercise the shared flow without network or paid tasks."""
import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import page_flow as flow
import page_parser as parser
import playwright_scraper as playwright
import puppeteer_scraper as puppeteer
import selenium_scraper as selenium
import diff_runs

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/perplexity_article_discover_live_20260930.json').read_text())
BLOCK = (Path(__file__).parent / 'fixtures/perplexity_cloudflare_block_real.html').read_text()


def url(n):
    return 'https://www.perplexity.ai/page/article-' + str(n).zfill(22)


def article(n):
    data = copy.deepcopy(FIXTURE)
    data['entries'][0]['thread_url_slug'] = 'article-' + str(n).zfill(22)
    return data


class Session:
    def __init__(self, engine):
        self.engine, self.url = engine, ''

    async def goto(self, target):
        self.url = target
        if self.engine.mode == 'navigation_error':
            raise RuntimeError('navigation failed')
        return 200

    async def content(self):
        if self.engine.mode == 'content_error' and self.url == url(2):
            raise RuntimeError('navigation raced with content')
        if self.engine.mode == 'feed_challenge_race' and self.url == parser.DISCOVER_URL:
            self.engine.content_calls += 1
            if self.engine.content_calls == 1:
                return BLOCK
            if self.engine.content_calls == 2:
                raise RuntimeError('navigation raced with content')
        return ''

    async def fetch_json(self, target):
        e = self.engine
        if '/rest/discover/feed' in target:
            e.feed_calls += 1
            if e.mode == 'feed_blocked' or e.feed_calls > 1:
                return 403, None, 'challenge'
            if e.mode == 'bad_feed':
                return 200, {'unexpected': []}, None
            return 200, {'items': [{'slug': 'article-' + str(1).zfill(22)}], 'next_token': 'more'}, None
        n = 2 if self.url == url(2) else 1
        e.article_calls += 1
        if e.mode == 'timeout':
            return 0, None, 'timeout'
        if e.mode == 'not_found':
            return 404, {'detail': 'missing'}, None
        if e.mode == 'recover_500' and e.article_calls == 1:
            return 500, {'detail': 'unavailable'}, None
        if e.mode in ('api500', 'rate_limited') and n == 2:
            return (429 if e.mode == 'rate_limited' else 500), {'detail': 'unavailable'}, None
        data = article(n)
        if e.mode == 'bad_article' and n == 2:
            data['entries'][0]['social_info'] = 'unexpected'
        return 200, data, None

    async def wait(self, seconds):
        pass

    async def close(self):
        self.engine.closed += 1
        if self.engine.mode == 'close_error':
            raise RuntimeError('cleanup failed')


class Engine:
    name = 'regression'
    readiness_s = 0

    def __init__(self, mode):
        self.mode = mode
        self.feed_calls = self.article_calls = self.closed = self.content_calls = 0

    async def open(self, proxy):
        return Session(self)

    async def sleep(self, seconds):
        pass

    async def solve_captcha(self, *args, **kwargs):
        raise AssertionError('no paid solving in regression tests')


class Regressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_flow(self, mode, *, discover=False, numbers=(1, 2), name='result'):
        out = str(Path(self.tmp.name) / (name + '.json'))
        args = playwright.build_arg_parser().parse_args([
            '--url', url(1), '--out', out, '--max-results', '5',
            '--solve-captcha', 'off', '--retries', '0', '--allow-empty'])
        engine = Engine(mode)
        code = asyncio.run(flow.run(engine, args, urls=[url(n) for n in numbers],
                                   discover_topic='top' if discover else None,
                                   proxy_pool=None, client=None, started_at=0))
        return code, json.loads(Path(out + '.meta.json').read_text()), json.loads(Path(out).read_text()), engine, out

    def test_success_and_not_found(self):
        code, meta, rows, _, _ = self.run_flow('good')
        self.assertEqual((code, meta['status'], len(rows)), (0, 'complete', 2))
        self.assertEqual(self.run_flow('not_found')[0], 4)

    def test_partial_article_failures_keep_good_rows(self):
        for mode in ('bad_article', 'api500', 'content_error'):
            with self.subTest(mode=mode):
                code, meta, rows, _, _ = self.run_flow(mode)
                self.assertEqual((code, meta['status'], len(rows)), (6, 'partial', 1))
                self.assertEqual(meta['failed_pages'], [2])
                self.assertEqual(meta['failed_urls'][0]['url'], url(2))

    def test_unread_is_not_empty(self):
        for mode in ('timeout', 'navigation_error'):
            self.assertEqual(self.run_flow(mode)[0], 5)

    def test_500_retry_recovers(self):
        code, _, rows, engine, _ = self.run_flow('recover_500', numbers=(1,))
        self.assertEqual((code, len(rows), engine.article_calls), (0, 1, 2))

    def test_discovery_failure_keeps_rows_and_blocks_diff(self):
        code, meta, rows, engine, out = self.run_flow('feed_partial', discover=True)
        self.assertEqual((code, meta['status'], len(rows)), (6, 'partial', 1))
        self.assertFalse(meta['discovery']['complete'])
        self.assertGreater(engine.feed_calls, 2)
        with self.assertRaises(SystemExit):
            diff_runs.diff(out, out)

    def test_feed_challenge_content_race_is_waited_out(self):
        code, meta, rows, engine, _ = self.run_flow('feed_challenge_race', discover=True)
        self.assertEqual((meta['discovery']['urls_collected'], len(rows)), (1, 1))
        self.assertEqual(engine.content_calls, 3)

    def test_first_feed_block_and_bad_schema(self):
        self.assertEqual(self.run_flow('feed_blocked', discover=True)[0], 3)
        self.assertEqual(self.run_flow('bad_feed', discover=True)[0], 5)

    def test_rate_limit_reason(self):
        code, meta, rows, _, _ = self.run_flow('rate_limited')
        self.assertEqual((code, meta['stop_reason'], len(rows)), (6, 'rate_limited', 1))

    def test_cleanup_failure_preserves_rows(self):
        self.assertEqual(self.run_flow('close_error')[0], 0)

    def test_batch_scope(self):
        a = self.run_flow('good', numbers=(1, 2), name='a')[-1]
        b = self.run_flow('good', numbers=(1, 3), name='b')[-1]
        with self.assertRaises(SystemExit):
            diff_runs.diff(a, b)
        self.assertEqual(diff_runs.diff(a, a)['removed'], [])

    def test_url_validation(self):
        for value in ('https://evilperplexity.ai/page/x', 'file:///page/x', '/page/x',
                      'https://user:pass@www.perplexity.ai/page/x', 'https://www.perplexity.ai:8080/page/x'):
            self.assertFalse(parser.is_page_url(value), value)
        self.assertTrue(parser.is_page_url(url(1)))

    def test_local_solver_never_buys_undeliverable_token(self):
        client = Mock()
        for engine in (playwright, puppeteer):
            result = asyncio.run(engine._maybe_solve_captcha(html='<div class="cf-turnstile" data-sitekey="x"></div>',
                                 url=url(1), client=client, policy='when-blocked'))
            self.assertEqual(result['action'], 'unsupported_delivery')
        result = selenium._maybe_solve_captcha(html='challenge', url=url(1), client=client, policy='always')
        self.assertEqual(result['action'], 'unsupported_delivery')
        self.assertEqual(client.mock_calls, [])


if __name__ == '__main__':
    unittest.main()
