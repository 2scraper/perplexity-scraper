#!/usr/bin/env python3
"""playwright_scraper.py — Playwright engine for the perplexity-scraper
family member. Playwright is the primary engine (see selenium_scraper.py /
puppeteer_scraper.py for parity copies — all three must agree on exit
codes, run status and whether a run crashes or spends money — CLAUDE.md
§4).

**Input**: `--url` (one article), `--urls-file` (one per line), or
`--discover top` (the Discover feed, paged until `--max-results`). Both
URL kinds work: classic `/page/{slug}-{id}` Pages and
`/discover/{topic}/{slug}-{id}` articles. There is no site search to point
a query at (robots.txt disallows `/search*`), hence no `--query`.

**How one article is read (confirmed live 2026-09-30, see page_parser.py)**:
navigate to the article so the browser holds the site's own cookies
(and clears Cloudflare, if it challenges), then call the article's own
data endpoint `/rest/article/{ref}` with `fetch()` from INSIDE that page
and parse the JSON. The rendered HTML is never row data (see
page_parser.py). The loop itself is `page_flow.run`, shared by all three
engines; this file only provides the Playwright page session.

Example:
    python3 playwright_scraper.py --url "https://www.perplexity.ai/page/How-to-Generate-VzUTuvQVSIqru3QGvPihlg"
    python3 playwright_scraper.py --discover top --max-results 40 --format csv
    python3 playwright_scraper.py --urls-file pages.txt --max-results 20 --dump-html
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from typing import Optional

try:
    from playwright.async_api import Browser, BrowserContext, Page, async_playwright
except ImportError as _IMPORT_ERROR:  # pragma: no cover — exercised by smoke_test's no-engine path
    Browser = BrowserContext = Page = None
    async_playwright = None
    _PLAYWRIGHT_IMPORT_ERROR = _IMPORT_ERROR
else:
    _PLAYWRIGHT_IMPORT_ERROR = None

import env_config
import page_flow
import scraper_api_engine
import scraper_api_client
import page_parser as pp
from captcha_solver import solve_when_blocked
from fingerprint_client import fetch_fingerprint, refuse_if_cdp, user_agent_from
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH
from proxy_pool import Proxy, ProxyPool, ProxyParseError, load_proxies, redact_credentials
from scraper_api_client import TwoCaptchaClient

ENGINE_NAME = "playwright"

# --- the handful of engine constants that vary per site (CLAUDE.md §5) ---
NAV_TIMEOUT_MS = 45_000
READINESS_WAIT_MS = 3_000  # the /rest/ calls need the origin's cookies, not a painted article
API_TIMEOUT_MS = 30_000
CHALLENGE_WAIT_S = 15  # a Cloudflare managed challenge can clear by itself; give it this long

# In-page fetch of a same-origin /rest/ URL. Returns {status, text} or
# {status: 0, error}; never throws into the engine.
_FETCH_JS = """
async ([u, timeoutMs]) => {
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
MIN_CARD_MATCHES = pp.MIN_CARD_MATCHES

log = logging.getLogger("playwright_scraper")


def _positive_int(value: str) -> int:
    ivalue = int(value)
    if ivalue < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer (got {value!r})")
    return ivalue


def _nonnegative_int(value: str) -> int:
    ivalue = int(value)
    if ivalue < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0 (got {value!r})")
    return ivalue


def _nonnegative_float(value: str) -> float:
    fvalue = float(value)
    if fvalue < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0 (got {value!r})")
    return fvalue


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="perplexity.ai article scraper (Pages and Discover) — Playwright engine",
        epilog="Credentials belong in .env / PERPLEXITY_PROXY / TWOCAPTCHA_KEY — never on this command line.",
    )
    p.add_argument("--url", default=None, help="One article URL: https://www.perplexity.ai/page/... or .../discover/{topic}/... (or set PERPLEXITY_URL) — overrides --urls-file/--discover")
    p.add_argument("--urls-file", default=None, help="Path to a file with one article URL per line")
    p.add_argument("--discover", default=None, metavar="TOPIC", help="Scrape the Discover feed for TOPIC ('top' is the one with items for an anonymous visitor), up to --max-results articles")
    p.add_argument("--max-results", type=_positive_int, default=30, help="Cap on how many articles are fetched this run")
    p.add_argument("--delay-between-pages", type=_nonnegative_float, default=2.0, help="Politeness delay between fetches when processing more than one URL, seconds")
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--out", default=None, help="Output path (default: perplexity_results.<format>)")
    p.add_argument("--retries", type=_nonnegative_int, default=2, help="Retries on a single page's navigation failure")
    p.add_argument("--retry-delay", type=_nonnegative_float, default=3.0)
    p.add_argument("--proxy", default=None, help="A single proxy, e.g. http://login:pass@host:port (or set PERPLEXITY_PROXY)")
    p.add_argument("--proxy-file", default=None, help="One proxy per line, same formats as --proxy")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=3)
    p.add_argument("--twocaptcha-key", default=None, help="(or set TWOCAPTCHA_KEY)")
    p.add_argument("--captcha-api", default=None, help="Override the 2Captcha API base URL (testing only)")
    p.add_argument("--solve-captcha", choices=["off", "when-blocked", "always"], default="when-blocked")
    p.add_argument("--max-solves", type=_nonnegative_int, default=8, help="Cap on PAID 2Captcha solves for the whole run (0 = never pay); recorded as solves_spent")
    p.add_argument("--min-score", type=float, default=0.3, help="Minimum acceptable reCAPTCHA v3 score (2Captcha's minScore task field)")
    p.add_argument("--cdp-endpoint", default=None, help="Connect to a remote CDP session (e.g. the 2Captcha Scraping Browser API) instead of launching locally (or set PERPLEXITY_CDP_ENDPOINT) — the recommended setup: Cloudflare blocks local browsers")
    p.add_argument("--scraper-api", action="store_true", help="Fetch through 2Captcha's Scraper API, routed through --cdp-endpoint's Scraping Browser profile, instead of driving this browser (needs TWOCAPTCHA_KEY and PERPLEXITY_CDP_ENDPOINT)")
    p.add_argument("--fingerprint", action="store_true", help="Fetch and apply a 2Captcha Fingerprint API profile (ignored with --cdp-endpoint — see fingerprint_client.refuse_if_cdp)")
    p.add_argument("--fp-tags", default=None, help="Fingerprint API filter, e.g. 'Windows,Chrome'")
    p.add_argument("--fp-country", default=None, help="Fingerprint API filter, e.g. 'us'")
    p.add_argument("--allow-empty", action="store_true", help="Write output even if zero articles were found")
    p.add_argument("--dump-html", action="store_true", help="Save each fetched page's HTML next to --out, on success too")
    p.add_argument("--headless", dest="headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    return p


def _default_out(fmt: str) -> str:
    return f"perplexity_results.{fmt}"


_resolve_urls = page_flow.resolve_urls  # the one implementation is page_flow's


async def _new_context(
    browser: Browser, proxy: Optional[Proxy], user_agent: Optional[str], *, reuse_default: bool = False,
) -> BrowserContext:
    """Over --cdp-endpoint, reuse the profile's own default context: it
    holds the cookies (Cloudflare clearance included) that make the
    profile worth reusing — the same fix shein-scraper needed live. A
    local browser gets a fresh isolated context per URL."""
    if reuse_default and browser.contexts:
        return browser.contexts[0]
    kwargs = {"locale": "en-US"}  # titles follow the browser language (see selenium_scraper)
    if proxy is not None:
        kwargs["proxy"] = proxy.playwright_proxy_dict()
    if user_agent:
        kwargs["user_agent"] = user_agent
    return await browser.new_context(**kwargs)


async def _close(page: Page, context: BrowserContext, *, reuse_default: bool) -> None:
    try:
        await page.close()
        if not reuse_default:
            await context.close()
    except Exception as exc:  # noqa: BLE001 — cleanup only
        log.debug("close failed: %s", exc)


async def _fetch_json(page: Page, url: str) -> tuple:
    """(status, decoded JSON or None, error text) for a same-origin /rest/ URL."""
    try:
        res = await page.evaluate(_FETCH_JS, [url, API_TIMEOUT_MS])
    except Exception as exc:  # noqa: BLE001 — a closed page / navigation race is a failed fetch, not a crash
        return 0, None, str(exc)
    status = int(res.get("status") or 0)
    if status == 0:
        return 0, None, res.get("error") or "fetch failed"
    try:
        return status, json.loads(res.get("text") or ""), None
    except ValueError:
        return status, None, "response was not JSON (likely a challenge page)"


async def _enable_scraping_browser_auto_solve(context: BrowserContext, page: Page) -> None:
    """Only meaningful over --cdp-endpoint — see the identical helper in
    every sibling repo's playwright_scraper.py."""
    try:
        session = await context.new_cdp_session(page)
        session.on("Captcha.detected", lambda *_: log.info("[Scraping Browser API] captcha detected"))
        session.on("Captcha.solveFinished", lambda *_: log.info("[Scraping Browser API] captcha solved"))
        session.on("Captcha.solveFailed", lambda *_: log.warning("[Scraping Browser API] captcha solve failed"))
        await session.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
    except Exception as exc:  # noqa: BLE001 — optional enhancement, never fatal
        log.warning("Captcha.setAutoSolve unavailable on this CDP session (continuing without it): %s", exc)


async def _maybe_solve_captcha(
    *, html: str, url: str, client: Optional[TwoCaptchaClient], policy: str, min_score: float = 0.3,
) -> Optional[dict]:
    if policy == "off" or client is None:
        return None
    result = solve_when_blocked(
        client=client, page_url=url, html=html, count_product_links=pp.count_result_cards,
        extra_markers=pp.BOT_CHALLENGE_MARKERS, min_score=min_score,
    )
    action = result.get("action")
    if action == "no_captcha_detected":
        pass
    elif action == "skipped_products_present":
        log.info("Captcha widget present but the article already rendered — not solving.")
    elif action == "warning_no_key":
        log.warning("Captcha solving skipped: %s", result.get("detail"))
    elif action == "warning_solver_error":
        log.warning("Captcha solve failed: %s", result.get("detail"))
    elif action == "solved":
        log.info("Captcha solved via 2Captcha (%s).", result.get("captcha_type"))
    elif action == "detected_unidentified_widget":
        log.warning("A captcha-like marker was detected but no known widget/sitekey could be extracted.")
    return result


async def _connect_over_cdp(pw, cdp_endpoint: str):
    """Bounded, retried through a still-locked profile, and a 401 says the
    endpoint expired (scraper_api_client.connect_with_retry, CLAUDE.md §26).
    Credentials never reach the message."""
    return await scraper_api_client.connect_with_retry(
        lambda: pw.chromium.connect_over_cdp(cdp_endpoint), redact=redact_credentials, log=log,
    )


class _PlaywrightSession:
    """page_flow.PageSession over one Playwright page."""

    def __init__(self, page: Page, context: BrowserContext, *, reuse_default: bool):
        self.page, self.context, self.reuse_default = page, context, reuse_default

    async def goto(self, url: str) -> Optional[int]:
        response = await self.page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        await self.page.wait_for_timeout(READINESS_WAIT_MS)
        return response.status if response is not None else None

    async def content(self) -> str:
        return await self.page.content()

    async def fetch_json(self, url: str) -> tuple:
        return await _fetch_json(self.page, url)

    async def wait(self, seconds: float) -> None:
        await self.page.wait_for_timeout(int(seconds * 1000))

    async def close(self) -> None:
        await _close(self.page, self.context, reuse_default=self.reuse_default)


class _PlaywrightEngine:
    """page_flow.Engine: a page per URL on the run's browser. Over
    --cdp-endpoint the profile's default context is reused (its cookies,
    Cloudflare clearance included, are the point of reusing a profile)."""

    name = ENGINE_NAME
    readiness_s = READINESS_WAIT_MS / 1000

    def __init__(self, browser: Browser, args: argparse.Namespace, *, autosolve: bool, user_agent: Optional[str], client):
        self.browser, self.args, self.autosolve, self.user_agent, self.client = browser, args, autosolve, user_agent, client

    async def open(self, proxy) -> _PlaywrightSession:
        reuse_default = bool(self.args.cdp_endpoint)
        context = await _new_context(self.browser, proxy, self.user_agent, reuse_default=reuse_default)
        page = await context.new_page()
        if self.autosolve:
            await _enable_scraping_browser_auto_solve(context, page)
        return _PlaywrightSession(page, context, reuse_default=reuse_default)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def solve_captcha(self, session, *, html: str, url: str):
        return await _maybe_solve_captcha(html=html, url=url, client=self.client, policy=self.args.solve_captcha,
                                          min_score=self.args.min_score)


async def run(args: argparse.Namespace) -> int:
    started_at = time.time()
    args._solve_budget = page_flow.SolveBudget(args.max_solves)
    try:
        urls, skipped = _resolve_urls(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_BAD_USAGE
    stop = page_flow.validate_common(args, urls=urls, skipped=skipped, print_err=lambda m: print(m, file=sys.stderr))
    if stop is not None:
        return stop
    discover_topic = None if urls or args.url or args.urls_file else args.discover
    if args.scraper_api:
        args.out = args.out or _default_out(args.format)
        return await scraper_api_engine.run(args, urls=urls, discover_topic=discover_topic, started_at=started_at)
    if async_playwright is None:
        print(f"Error: playwright is not installed ({_PLAYWRIGHT_IMPORT_ERROR}). "
              f"pip install -r requirements-playwright.txt && playwright install chromium", file=sys.stderr)
        return EXIT_CRASH
    args.out = args.out or _default_out(args.format)

    try:
        proxies = load_proxies(args.proxy, args.proxy_file)
    except ProxyParseError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_BAD_USAGE
    proxy_pool = ProxyPool(proxies, shuffle=args.proxy_shuffle, block_retries=args.proxy_block_retries) if proxies else None

    client = None
    if args.twocaptcha_key and (args.solve_captcha != "off" or args.fingerprint):
        client = TwoCaptchaClient(args.twocaptcha_key, api_base=args.captcha_api)
    user_agent = None
    if args.fingerprint and not refuse_if_cdp(args.cdp_endpoint):
        if client is None:
            log.warning("--fingerprint requested but no --twocaptcha-key/TWOCAPTCHA_KEY set — continuing without one.")
        else:
            profile = fetch_fingerprint(client, tags=args.fp_tags, country=args.fp_country)
            if profile:
                user_agent = user_agent_from(profile)

    try:
        async with async_playwright() as pw:
            if args.cdp_endpoint:
                if args.proxy or args.proxy_file:
                    log.warning("Ignoring --proxy: a --cdp-endpoint session already carries its own exit IP.")
                    proxy_pool = None
                try:
                    browser = await _connect_over_cdp(pw, args.cdp_endpoint)
                except RuntimeError as exc:
                    log.error("CDP connection failed — treating as remote_api_error, not a crash: %s", exc)
                    return page_flow.finish(args, products=[], blocked=False, remote_api_error=True, engine_name=ENGINE_NAME,
                                            urls=urls, discover_topic=discover_topic, started_at=started_at,
                                            pages_completed=0, failed_pages=[])
            else:
                browser = await pw.chromium.launch(headless=args.headless)
            engine = _PlaywrightEngine(browser, args, autosolve=bool(args.cdp_endpoint) and args.solve_captcha != "off",
                                       user_agent=user_agent, client=client)
            try:
                return await page_flow.run(engine, args, urls=urls, discover_topic=discover_topic,
                                           proxy_pool=proxy_pool, client=client, started_at=started_at)
            finally:
                await browser.close()
    except Exception:
        log.exception("Unhandled error — this is a crash, not a normal blocked/empty run")
        return EXIT_CRASH


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = build_arg_parser()
    args = parser.parse_args()
    args = env_config.apply_env(args)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())
