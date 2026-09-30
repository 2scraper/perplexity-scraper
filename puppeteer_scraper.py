#!/usr/bin/env python3
"""puppeteer_scraper.py — pyppeteer engine, parity copy of
playwright_scraper.py (same flags, same exit codes and status semantics —
see output_writer.finish_run). Not the primary engine (Playwright is); kept
for parity, and because it — like Playwright, unlike Selenium — CAN open an
authenticated `ws://login:pass@host:port` CDP session, so it is the second
engine able to use the Scraping Browser API's own `Captcha.setAutoSolve`.

Named and shaped like every sibling repo's own `puppeteer_scraper.py`
(Python + pyppeteer, not a separate Node.js file) — this repo follows that
family convention for the same reason: one shared output contract and one
set of family modules across all three engines.

pyppeteer itself is effectively unmaintained (its own README points at
Playwright) — this file exists for parity/completeness, not as a
recommendation to prefer it.

Chromium binary: sourced from `PYPPETEER_EXECUTABLE_PATH` /
`PUPPETEER_EXECUTABLE_PATH` if set (handy for reusing an existing
Playwright/system Chromium instead of pyppeteer's own bundled download),
otherwise pyppeteer's own default.

Same input and the same read path as playwright_scraper.py (see its
module docstring): navigate to the article, `fetch()` its own
`/rest/article/{ref}` from inside the page, decide the outcome in
`page_flow.decide()`. Over `--cdp-endpoint` one connection serves the
whole run and is DISCONNECTED at the end, never closed — `close()` on a
connected pyppeteer browser ends the remote Browser API session itself.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

try:
    from pyppeteer import connect as pyppeteer_connect
    from pyppeteer import launch as pyppeteer_launch
    from pyppeteer.errors import NetworkError, PageError, TimeoutError as PyppeteerTimeoutError
except ImportError as _IMPORT_ERROR:  # pragma: no cover — exercised by smoke_test's no-engine path
    pyppeteer_launch = None
    pyppeteer_connect = None
    NetworkError = PageError = PyppeteerTimeoutError = Exception
    _PYPPETEER_IMPORT_ERROR = _IMPORT_ERROR
else:
    _PYPPETEER_IMPORT_ERROR = None

import env_config
import page_flow
import page_parser as pp
from captcha_solver import solve_when_blocked
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH, Product, finish_run
from fingerprint_client import fetch_fingerprint, refuse_if_cdp, user_agent_from
from proxy_pool import Proxy, ProxyPool, ProxyParseError, is_proxy_dead_error, load_proxies, redact_credentials
from scraper_api_client import TwoCaptchaClient

ENGINE_NAME = "puppeteer"
NAV_TIMEOUT_MS = 45_000
READINESS_WAIT_S = 3.0
API_TIMEOUT_MS = 30_000
CHALLENGE_WAIT_S = 15
CDP_CONNECT_TIMEOUT_S = 60

_FETCH_JS = """
async (u, timeoutMs) => {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(u, {credentials: 'include', headers: {accept: 'application/json'}, signal: ctl.signal});
    return {status: r.status, text: await r.text()};
  } catch (e) {
    return {status: 0, error: String(e)};
  } finally { clearTimeout(t); }
}
"""

log = logging.getLogger("puppeteer_scraper")

_CHROMIUM_EXECUTABLE = os.environ.get("PYPPETEER_EXECUTABLE_PATH") or os.environ.get("PUPPETEER_EXECUTABLE_PATH")


def _positive_int(value: str) -> int:
    ivalue = int(value)
    if ivalue < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer (got {value!r})")
    return ivalue


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="perplexity.ai Pages scraper — pyppeteer (Puppeteer) engine",
        epilog="Credentials belong in .env / PERPLEXITY_PROXY / TWOCAPTCHA_KEY — never on this command line.",
    )
    p.add_argument("--url", default=None, help="One article URL (/page/... or /discover/{topic}/...) — overrides --urls-file/--discover")
    p.add_argument("--urls-file", default=None)
    p.add_argument("--discover", default=None, metavar="TOPIC", help="Scrape the Discover feed for TOPIC ('top'), up to --max-results articles")
    p.add_argument("--max-results", type=_positive_int, default=30)
    p.add_argument("--delay-between-pages", type=float, default=1.0)
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--out", default=None)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--retry-delay", type=float, default=3.0)
    p.add_argument("--proxy", default=None)
    p.add_argument("--proxy-file", default=None)
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=3)
    p.add_argument("--twocaptcha-key", default=None)
    p.add_argument("--captcha-api", default=None, help="Override the 2Captcha API base URL (testing only)")
    p.add_argument("--solve-captcha", choices=["off", "when-blocked", "always"], default="when-blocked")
    p.add_argument("--min-score", type=float, default=0.3, help="Minimum acceptable reCAPTCHA v3 score (2Captcha's minScore task field)")
    p.add_argument("--fingerprint", action="store_true", help="Fetch and apply a 2Captcha Fingerprint API profile's user agent (ignored with --cdp-endpoint — see fingerprint_client.refuse_if_cdp)")
    p.add_argument("--fp-tags", default=None, help="Fingerprint API filter, e.g. 'Windows'")
    p.add_argument("--fp-country", default=None, help="Fingerprint API filter, e.g. 'us'")
    p.add_argument("--cdp-endpoint", default=None)
    p.add_argument("--allow-empty", action="store_true")
    p.add_argument("--dump-html", action="store_true")
    p.add_argument("--headless", dest="headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    return p


def _default_out(fmt: str) -> str:
    return f"perplexity_results.{fmt}"


def _resolve_urls(args: argparse.Namespace) -> tuple:
    """See playwright_scraper.py's identical helper for the full rationale
    (robots.txt-disallowed paths are filtered out, never fetched)."""
    if args.url:
        candidates = [args.url]
    elif args.urls_file:
        try:
            lines = Path(args.urls_file).read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ValueError(f"could not read --urls-file {args.urls_file!r}: {exc}")
        candidates = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
    else:
        return [], 0

    urls, skipped = [], 0
    for url in candidates:
        if pp.is_disallowed_path(url):
            log.warning("Skipping %s — its path is disallowed by perplexity.ai's robots.txt; this tool never requests a disallowed path.", url)
            skipped += 1
            continue
        if not pp.is_page_url(url):
            log.warning("Skipping %s — not a perplexity.ai article URL (/page/... or /discover/{topic}/...).", url)
            skipped += 1
            continue
        urls.append(url)
    return urls, skipped


def _dump_path(out_path: str, index: int) -> str:
    stem = Path(out_path).with_suffix("")
    return f"{stem}_debug_{index}.html"


async def _launch(*, headless: bool, proxy: Optional[Proxy], cdp_endpoint: Optional[str]):
    if cdp_endpoint:
        # pyppeteer's connect() has no timeout of its own: a rejected
        # handshake (live: 401 from the Browser API) hung the run forever.
        try:
            return await asyncio.wait_for(
                pyppeteer_connect(browserWSEndpoint=cdp_endpoint, defaultViewport=None), CDP_CONNECT_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(f"CDP connection timed out after {CDP_CONNECT_TIMEOUT_S}s") from None
        except Exception as exc:
            raise RuntimeError(f"CDP connection failed: {redact_credentials(str(exc))}") from None
    args = ["--no-sandbox", "--disable-dev-shm-usage", "--lang=en-US"]  # see selenium_scraper: titles follow the browser language
    if proxy is not None:
        args.append(proxy.pyppeteer_launch_arg())
    kwargs = dict(headless=headless, args=args)
    if _CHROMIUM_EXECUTABLE:
        kwargs["executablePath"] = _CHROMIUM_EXECUTABLE
    return await pyppeteer_launch(**kwargs)


async def _authenticate_if_needed(page, proxy: Optional[Proxy]) -> None:
    if proxy is not None:
        auth = proxy.pyppeteer_auth_dict()
        if auth:
            await page.authenticate(auth)


async def _enable_scraping_browser_auto_solve(page) -> None:
    try:
        client = await page.target.createCDPSession()
        client.on("Captcha.detected", lambda *_: log.info("[Scraping Browser API] captcha detected"))
        client.on("Captcha.solveFinished", lambda *_: log.info("[Scraping Browser API] captcha solved"))
        client.on("Captcha.solveFailed", lambda *_: log.warning("[Scraping Browser API] captcha solve failed"))
        await client.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
    except Exception as exc:  # noqa: BLE001 — optional enhancement, never fatal
        log.warning("Captcha.setAutoSolve unavailable on this CDP session (continuing without it): %s", exc)


async def _maybe_solve_captcha(*, html: str, url: str, client: Optional[TwoCaptchaClient], policy: str, min_score: float = 0.3) -> Optional[dict]:
    if policy == "off" or client is None:
        return None
    result = solve_when_blocked(
        client=client, page_url=url, html=html, count_product_links=pp.count_result_cards,
        extra_markers=pp.BOT_CHALLENGE_MARKERS, min_score=min_score,
    )
    action = result.get("action")
    if action == "warning_no_key":
        log.warning("Captcha solving skipped: %s", result.get("detail"))
    elif action == "warning_solver_error":
        log.warning("Captcha solve failed: %s", result.get("detail"))
    elif action == "solved":
        log.info("Captcha solved via 2Captcha (%s).", result.get("captcha_type"))
    return result


async def _release(browser, *, remote: bool) -> None:
    try:
        if remote:
            await browser.disconnect()
        else:
            await browser.close()
    except Exception as exc:  # noqa: BLE001 — cleanup only
        log.debug("browser release failed: %s", exc)


async def _fetch_json(page, url: str) -> tuple:
    """(status, decoded JSON or None, error text) — see playwright_scraper._fetch_json."""
    try:
        res = await page.evaluate(_FETCH_JS, url, API_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001
        return 0, None, str(exc)
    status = int((res or {}).get("status") or 0)
    if status == 0:
        return 0, None, (res or {}).get("error") or "fetch failed"
    try:
        return status, json.loads(res.get("text") or ""), None
    except ValueError:
        return status, None, "response was not JSON (likely a challenge page)"


async def _open_page(browser, *, proxy: Optional[Proxy], user_agent: Optional[str], autosolve: bool):
    page = await browser.newPage()
    if user_agent:
        await page.setUserAgent(user_agent)
    await _authenticate_if_needed(page, proxy)
    if autosolve:
        await _enable_scraping_browser_auto_solve(page)
    return page


async def scrape_one_page(
    *, url: str, index: int, args: argparse.Namespace, browser,
    proxy: Optional[Proxy], proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
    autosolve: bool = False, user_agent: Optional[str] = None,
) -> Tuple[Optional[Product], bool, bool, bool]:
    """Returns (product_or_none, blocked, nav_failed, not_found) — see
    playwright_scraper.scrape_one_page."""
    log.info("Fetching %s (proxy: %s)", url, proxy.masked() if proxy else "(none — direct, or the --cdp-endpoint session's own exit)")
    page = await _open_page(browser, proxy=proxy, user_agent=user_agent, autosolve=autosolve)
    try:
        last_error = None
        status = None
        for attempt in range(args.retries + 1):
            try:
                response = await page.goto(url, {"waitUntil": "domcontentloaded", "timeout": NAV_TIMEOUT_MS})
                await asyncio.sleep(READINESS_WAIT_S)
                status = response.status if response is not None else None
                last_error = None
                break
            except (NetworkError, PageError, PyppeteerTimeoutError, Exception) as exc:  # noqa: BLE001
                last_error = redact_credentials(str(exc))
                if proxy_pool is not None and proxy is not None and is_proxy_dead_error(last_error):
                    proxy_pool.report_failure(proxy, dead=True)
                log.warning("Navigation attempt %d/%d for %s failed: %s", attempt + 1, args.retries + 1, url, last_error)
                if attempt < args.retries:
                    await asyncio.sleep(args.retry_delay)
        if last_error is not None:
            log.error("%s permanently failed to load: %s", url, last_error)
            return None, False, True, False

        html = await page.content()
        waited = 0.0
        while page_flow.is_challenge(html) and waited < CHALLENGE_WAIT_S:
            await asyncio.sleep(1)
            waited += 1
            try:
                html = await page.content()
            except Exception:  # noqa: BLE001 — mid-navigation while the challenge redirects
                continue
        if waited and not page_flow.is_challenge(html):
            log.info("%s: Cloudflare challenge cleared by itself after %.0fs.", url, waited)
        if page_flow.is_challenge(html):
            captcha_result = await _maybe_solve_captcha(html=html, url=url, client=client, policy=args.solve_captcha, min_score=args.min_score)
            if captcha_result and captcha_result.get("action") == "solved":
                await asyncio.sleep(READINESS_WAIT_S)
                html = await page.content()

        _topic, ref = pp.article_ref(url)
        api_status, article_json, api_error = (0, None, "skipped: page is a bot challenge")
        if not page_flow.is_challenge(html):
            api_status, article_json, api_error = await _fetch_json(page, pp.article_api_url(ref))
        outcome = page_flow.decide(url=url, http_status=status, html=html, api_status=api_status,
                                   article_json=article_json, api_error=api_error)
        for message in outcome.warnings:
            log.warning("%s", message)
        if proxy_pool is not None and proxy is not None:
            if outcome.blocked and status in (403, 429):
                proxy_pool.report_failure(proxy, dead=True)
            elif not outcome.blocked:
                proxy_pool.report_success(proxy)
        if args.dump_html:
            Path(_dump_path(args.out, index)).write_text(html, encoding="utf-8")
        return outcome.product, outcome.blocked, False, outcome.not_found
    finally:
        try:
            await page.close()
        except Exception:  # noqa: BLE001
            pass


async def collect_discover_urls(*, topic: str, limit: int, args: argparse.Namespace, browser,
                                proxy: Optional[Proxy], user_agent: Optional[str]) -> tuple:
    """(urls, error) — see playwright_scraper.collect_discover_urls."""
    page = await _open_page(browser, proxy=proxy, user_agent=user_agent, autosolve=False)
    urls: List[str] = []
    try:
        try:
            await page.goto(pp.DISCOVER_URL, {"waitUntil": "domcontentloaded", "timeout": NAV_TIMEOUT_MS})
            await asyncio.sleep(READINESS_WAIT_S)
        except Exception as exc:  # noqa: BLE001
            return [], f"could not open {pp.DISCOVER_URL}: {redact_credentials(str(exc))}"
        offset = 0
        while len(urls) < limit:
            status, data, error = await _fetch_json(page, pp.discover_feed_api_url(topic, offset=offset))
            if error or status != 200:
                if not urls:
                    return [], f"Discover feed HTTP {status or '-'}: {error or 'unexpected status'}"
                log.warning("Discover feed page at offset %d failed (HTTP %s) — keeping the %d URLs collected.", offset, status or "-", len(urls))
                break
            page_urls, has_more = pp.parse_discover_feed(data, topic=topic)
            new = [u for u in page_urls if u not in urls]
            urls.extend(new)
            if not new or not has_more:
                break
            offset += len(page_urls)
            await asyncio.sleep(args.delay_between_pages)
        return urls[:limit], None
    finally:
        try:
            await page.close()
        except Exception:  # noqa: BLE001
            pass


async def scrape_urls(
    *, urls: List[str], args: argparse.Namespace, remote_browser,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient], autosolve: bool = False,
    user_agent: Optional[str] = None,
) -> tuple:
    """Returns (products, blocked, remote_api_error, pages_completed,
    failed_pages). Over CDP every URL shares `remote_browser`; locally
    each URL gets a fresh browser on its own exit (a rotation is a fresh
    browser — CLAUDE.md §8)."""
    products: List[Product] = []
    failed_pages: List[int] = []
    any_blocked = False
    completed = 0

    capped = urls[: args.max_results]
    for i, url in enumerate(capped, start=1):
        proxy = proxy_pool.next() if proxy_pool else None
        browser = remote_browser or await _launch(headless=args.headless, proxy=proxy, cdp_endpoint=None)
        try:
            product, blocked, nav_failed, _not_found = await scrape_one_page(
                url=url, index=i, args=args, browser=browser, proxy=proxy, proxy_pool=proxy_pool,
                client=client, autosolve=autosolve, user_agent=user_agent,
            )
        finally:
            if remote_browser is None:
                await _release(browser, remote=False)
        if nav_failed:
            failed_pages.append(i)
        else:
            completed += 1
            if blocked:
                any_blocked = True
            if product is not None:
                products.append(product)
        if i < len(capped):
            await asyncio.sleep(args.delay_between_pages)

    return products, any_blocked, False, completed, failed_pages


_CLOSED_TARGET_NOISE = ("Target closed", "No session with given id")


def _quiet_target_closed(loop, context) -> None:
    """pyppeteer leaves a detachFromTarget/sendMessageToTarget future
    failing with "Target closed" or "No session with given id" behind a
    page.close() — logged as an ERROR although nothing went wrong (seen on
    every live run). Everything else still reaches the default handler."""
    exc = context.get("exception")
    if isinstance(exc, NetworkError) and any(m in str(exc) for m in _CLOSED_TARGET_NOISE):
        return
    loop.default_exception_handler(context)


async def run(args: argparse.Namespace) -> int:
    started_at = time.time()
    asyncio.get_running_loop().set_exception_handler(_quiet_target_closed)
    try:
        urls, skipped_disallowed = _resolve_urls(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_BAD_USAGE
    discover_topic = None if urls or args.url or args.urls_file else args.discover
    if not urls and not discover_topic:
        if skipped_disallowed:
            print(f"Error: none of the URLs given is a fetchable article ({skipped_disallowed} skipped) — nothing left to fetch", file=sys.stderr)
        else:
            print("Error: provide --url, --urls-file or --discover", file=sys.stderr)
        return EXIT_BAD_USAGE
    if args.format not in ("json", "csv"):
        print(f"Error: unsupported --format {args.format!r}", file=sys.stderr)
        return EXIT_BAD_USAGE
    if pyppeteer_launch is None:
        print(f"Error: pyppeteer is not installed ({_PYPPETEER_IMPORT_ERROR}). "
              f"pip install -r requirements-puppeteer.txt", file=sys.stderr)
        return EXIT_CRASH
    args.out = args.out or _default_out(args.format)

    try:
        proxies = load_proxies(args.proxy, args.proxy_file)
    except ProxyParseError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_BAD_USAGE
    proxy_pool = ProxyPool(proxies, shuffle=args.proxy_shuffle, block_retries=args.proxy_block_retries) if proxies else None
    if args.cdp_endpoint and proxy_pool is not None:
        log.warning("Ignoring --proxy: a --cdp-endpoint session already carries its own exit IP.")
        proxy_pool = None

    client = None
    if args.twocaptcha_key and (args.solve_captcha != "off" or args.fingerprint):
        client = TwoCaptchaClient(args.twocaptcha_key, api_base=args.captcha_api)
    autosolve = bool(args.cdp_endpoint) and args.solve_captcha != "off"

    user_agent = None
    cdp_refused_fingerprint = refuse_if_cdp(args.cdp_endpoint)
    if args.fingerprint and not cdp_refused_fingerprint:
        if client is None:
            log.warning("--fingerprint requested but no --twocaptcha-key/TWOCAPTCHA_KEY set — continuing without one.")
        else:
            profile = fetch_fingerprint(client, tags=args.fp_tags, country=args.fp_country)
            if profile:
                user_agent = user_agent_from(profile)

    remote_browser = None
    remote_api_error = False
    products, blocked, completed, failed_pages = [], False, 0, []
    try:
        if args.cdp_endpoint:
            try:
                remote_browser = await _launch(headless=args.headless, proxy=None, cdp_endpoint=args.cdp_endpoint)
            except RuntimeError as exc:
                log.error("CDP connection failed — treating as remote_api_error, not a crash: %s", exc)
                remote_api_error = True
        if not remote_api_error and discover_topic:
            proxy = proxy_pool.next() if proxy_pool else None
            browser = remote_browser or await _launch(headless=args.headless, proxy=proxy, cdp_endpoint=None)
            try:
                urls, discover_error = await collect_discover_urls(
                    topic=discover_topic, limit=args.max_results, args=args, browser=browser,
                    proxy=proxy, user_agent=user_agent,
                )
            finally:
                if remote_browser is None:
                    await _release(browser, remote=False)
            if discover_error:
                log.error("Discover feed unavailable — treating as remote_api_error: %s", discover_error)
                remote_api_error = True
            elif not urls:
                log.warning("Discover topic %r returned no articles.", discover_topic)
            else:
                log.info("Discover topic %r: %d article URL(s) to fetch.", discover_topic, len(urls))
        if not remote_api_error:
            products, blocked, remote_api_error, completed, failed_pages = await scrape_urls(
                urls=urls, args=args, remote_browser=remote_browser, proxy_pool=proxy_pool, client=client,
                autosolve=autosolve, user_agent=user_agent,
            )
    except Exception:
        log.exception("Unhandled error — this is a crash, not a normal blocked/empty run")
        return EXIT_CRASH
    finally:
        if remote_browser is not None:
            await _release(remote_browser, remote=True)

    return finish_run(
        products=products, out_path=args.out, fmt=args.format, engine=ENGINE_NAME,
        url=(urls[0] if urls else "") if not discover_topic else f"{pp.DISCOVER_URL} (topic={discover_topic})",
        pages_requested=len(urls[: args.max_results]), pages_completed=completed,
        failed_pages=failed_pages or None,
        blocked=blocked, remote_api_error=remote_api_error, allow_empty=args.allow_empty,
        started_at=started_at, price_confirmed_pct=None,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = build_arg_parser().parse_args()
    args = env_config.apply_env(args)
    try:
        # asyncio.run(), not get_event_loop(): the latter raises in any
        # process that already ran and closed a loop (shein-scraper hit it).
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())
