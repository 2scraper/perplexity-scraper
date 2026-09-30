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
and parse the JSON. The rendered HTML is only a fallback: it has no
counters, no sources, and site-default Open Graph tags.

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
from pathlib import Path
from typing import List, Optional

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
import page_parser as pp
from captcha_solver import solve_when_blocked
from fingerprint_client import fetch_fingerprint, refuse_if_cdp, user_agent_from
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH, Product, finish_run
from proxy_pool import Proxy, ProxyPool, ProxyParseError, is_proxy_dead_error, load_proxies, redact_credentials
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
        description="perplexity.ai Pages scraper — Playwright engine",
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
    p.add_argument("--min-score", type=float, default=0.3, help="Minimum acceptable reCAPTCHA v3 score (2Captcha's minScore task field)")
    p.add_argument("--cdp-endpoint", default=None, help="Connect to a remote CDP session (e.g. the 2Captcha Scraping Browser API) instead of launching locally (or set PERPLEXITY_CDP_ENDPOINT) — opt-in, not required for a normal run")
    p.add_argument("--fingerprint", action="store_true", help="Fetch and apply a 2Captcha Fingerprint API profile (ignored with --cdp-endpoint — see fingerprint_client.refuse_if_cdp)")
    p.add_argument("--fp-tags", default=None, help="Fingerprint API filter, e.g. 'Windows,Chrome'")
    p.add_argument("--fp-country", default=None, help="Fingerprint API filter, e.g. 'us'")
    p.add_argument("--allow-empty", action="store_true", help="Write output even if zero Pages were found")
    p.add_argument("--dump-html", action="store_true", help="Save each fetched page's HTML next to --out, on success too")
    p.add_argument("--headless", dest="headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    return p


def _default_out(fmt: str) -> str:
    return f"perplexity_results.{fmt}"


def _resolve_urls(args: argparse.Namespace) -> tuple:
    """Returns (urls, skipped_disallowed). `--url` takes priority over
    `--urls-file` when both are given, matching the family's "most
    specific input wins" convention for overlapping selector inputs (see
    lidl_parser.search_url's own docstring on the same rule).

    A robots.txt-disallowed path is filtered out here — logged and
    skipped, never fetched — rather than attempted and reported as a
    failure: refusing to request a disallowed path at all is a decision,
    not an error."""
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
    """See every sibling repo's playwright_scraper.py for why this wraps
    the connection error rather than letting it propagate: connect_over_cdp
    repeats a failed endpoint's login:password in its own message and
    "Call log" several times over."""
    try:
        return await pw.chromium.connect_over_cdp(cdp_endpoint)
    except Exception as exc:
        raise RuntimeError(f"CDP connection failed: {redact_credentials(str(exc))}") from None


async def scrape_one_page(
    *, url: str, index: int, args: argparse.Namespace, browser: Browser,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
    autosolve: bool, user_agent: Optional[str],
) -> tuple:
    """Returns (product_or_none, blocked, nav_failed, not_found)."""
    reuse_default = bool(args.cdp_endpoint)

    proxy = proxy_pool.next() if proxy_pool else None
    log.info("Fetching %s (proxy: %s)", url, proxy.masked() if proxy else "(none — direct, or the --cdp-endpoint session's own exit)")
    context = await _new_context(browser, proxy, user_agent, reuse_default=reuse_default)
    page = await context.new_page()
    if autosolve:
        await _enable_scraping_browser_auto_solve(context, page)

    last_error = None
    status = None
    for attempt in range(args.retries + 1):
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            await page.wait_for_timeout(READINESS_WAIT_MS)
            status = response.status if response is not None else None
            last_error = None
            break
        except Exception as exc:  # noqa: BLE001 — every remote call must be bounded and reported
            last_error = redact_credentials(str(exc))
            log.warning("Navigation attempt %d/%d for %s failed: %s", attempt + 1, args.retries + 1, url, last_error)
            if proxy_pool is not None and proxy is not None and is_proxy_dead_error(last_error):
                proxy_pool.report_failure(proxy, dead=True)
            if attempt < args.retries:
                await asyncio.sleep(args.retry_delay)

    if last_error is not None:
        await _close(page, context, reuse_default=reuse_default)
        log.error("%s permanently failed to load: %s", url, last_error)
        return None, False, True, False

    html = await page.content()
    waited = 0.0
    while page_flow.is_challenge(html) and waited < CHALLENGE_WAIT_S:
        await page.wait_for_timeout(1000)
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
            await page.wait_for_timeout(READINESS_WAIT_MS)
            html = await page.content()

    _topic, ref = pp.article_ref(url)
    api_status, article_json, api_error = (0, None, "skipped: page is a bot challenge")
    if not page_flow.is_challenge(html):
        api_status, article_json, api_error = await _fetch_json(page, pp.article_api_url(ref))
        for delay in page_flow.API_RETRY_DELAYS_S:
            if not page_flow.should_retry_api(api_status, api_error):
                break
            log.info("%s: article API HTTP %s — retrying in %ss (a fresh profile needs the page's own bot check first).", url, api_status or "-", delay)
            await asyncio.sleep(delay)
            api_status, article_json, api_error = await _fetch_json(page, pp.article_api_url(ref))
    outcome = page_flow.decide(url=url, http_status=status, html=html, api_status=api_status,
                               article_json=article_json, api_error=api_error)
    for message in outcome.warnings:
        log.warning("%s", message)
    blocked = outcome.blocked
    if proxy_pool is not None and proxy is not None:
        if blocked and status in (403, 429):
            proxy_pool.report_failure(proxy, dead=True)
        elif not blocked:
            proxy_pool.report_success(proxy)

    if args.dump_html:
        Path(_dump_path(args.out, index)).write_text(html, encoding="utf-8")

    await _close(page, context, reuse_default=reuse_default)
    return outcome.product, blocked, False, outcome.not_found


async def collect_discover_urls(
    *, topic: str, limit: int, args: argparse.Namespace, browser: Browser,
    proxy_pool: Optional[ProxyPool], user_agent: Optional[str],
) -> tuple:
    """(urls, error) from the Discover feed, paged 20 at a time by offset
    until `limit` distinct URLs or the feed runs out. `error` is set when
    even the first page could not be read."""
    reuse_default = bool(args.cdp_endpoint)
    proxy = proxy_pool.next() if proxy_pool else None
    context = await _new_context(browser, proxy, user_agent, reuse_default=reuse_default)
    page = await context.new_page()
    urls: List[str] = []
    try:
        try:
            await page.goto(pp.DISCOVER_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            await page.wait_for_timeout(READINESS_WAIT_MS)
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
        await _close(page, context, reuse_default=reuse_default)


async def scrape_urls(
    *, urls: List[str], args: argparse.Namespace, browser: Browser,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
    autosolve: bool, user_agent: Optional[str],
) -> tuple:
    """Returns (products, blocked, remote_api_error, pages_completed,
    failed_pages). One URL's failure is a per-URL skip, never a crash that
    loses the batch (CLAUDE.md §6). A URL whose article does not exist
    counts as completed with no row: it is a fact about the site."""
    products: List[Product] = []
    failed_pages: List[int] = []
    any_blocked = False
    completed = 0

    batch = urls[: args.max_results]
    for i, url in enumerate(batch, start=1):
        product, blocked, nav_failed, _not_found = await scrape_one_page(
            url=url, index=i, args=args, browser=browser, proxy_pool=proxy_pool,
            client=client, autosolve=autosolve, user_agent=user_agent,
        )
        if nav_failed:
            failed_pages.append(i)
        else:
            completed += 1
            if blocked:
                any_blocked = True
            if product is not None:
                products.append(product)
        if i < len(batch):
            await asyncio.sleep(args.delay_between_pages)

    return products, any_blocked, False, completed, failed_pages


async def run(args: argparse.Namespace) -> int:
    started_at = time.time()
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
    if async_playwright is None:
        print(f"Error: playwright is not installed ({_PLAYWRIGHT_IMPORT_ERROR}). "
              f"pip install -r requirements-playwright.txt && playwright install chromium", file=sys.stderr)
        return EXIT_CRASH
    args.out = args.out or _default_out(args.format)

    try:
        proxies = load_proxies(args.proxy, args.proxy_file)
    except ProxyParseError as exc:
        # A malformed --proxy / --proxy-file line is a USAGE mistake, not a
        # crash — see every sibling repo's playwright_scraper.py for the
        # full incident writeup this guard fixes.
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_BAD_USAGE
    proxy_pool = ProxyPool(proxies, shuffle=args.proxy_shuffle, block_retries=args.proxy_block_retries) if proxies else None

    client = None
    if args.twocaptcha_key and (args.solve_captcha != "off" or args.fingerprint):
        client = TwoCaptchaClient(args.twocaptcha_key, api_base=args.captcha_api)

    user_agent = None
    cdp_refused_fingerprint = refuse_if_cdp(args.cdp_endpoint)
    if args.fingerprint and not cdp_refused_fingerprint:
        if client is None:
            log.warning("--fingerprint requested but no --twocaptcha-key/TWOCAPTCHA_KEY set — continuing without one.")
        else:
            profile = fetch_fingerprint(client, tags=args.fp_tags, country=args.fp_country)
            if profile:
                user_agent = user_agent_from(profile)

    cdp_connect_failed = False
    try:
        async with async_playwright() as pw:
            if args.cdp_endpoint:
                if args.proxy or args.proxy_file:
                    log.warning("Ignoring --proxy: a --cdp-endpoint session already carries its own exit IP.")
                    proxy_pool = None
                try:
                    browser = await _connect_over_cdp(pw, args.cdp_endpoint)
                except RuntimeError as exc:
                    # A broken/misconfigured remote CDP session is a
                    # remote-API failure, not a bug in this scraper.
                    log.error("CDP connection failed — treating as remote_api_error, not a crash: %s", exc)
                    cdp_connect_failed = True
                    browser = None
            else:
                browser = await pw.chromium.launch(headless=args.headless)

            if not cdp_connect_failed and discover_topic:
                urls, discover_error = await collect_discover_urls(
                    topic=discover_topic, limit=args.max_results, args=args, browser=browser,
                    proxy_pool=proxy_pool, user_agent=user_agent,
                )
                if discover_error:
                    log.error("Discover feed unavailable — treating as remote_api_error: %s", discover_error)
                    cdp_connect_failed = True
                elif not urls:
                    log.warning("Discover topic %r returned no articles.", discover_topic)
                else:
                    log.info("Discover topic %r: %d article URL(s) to fetch.", discover_topic, len(urls))
            if cdp_connect_failed:
                products, blocked, remote_api_error, completed, failed_pages = [], False, True, 0, []
                if browser is not None:
                    await browser.close()
            else:
                autosolve = bool(args.cdp_endpoint) and args.solve_captcha != "off"
                products, blocked, remote_api_error, completed, failed_pages = await scrape_urls(
                    urls=urls, args=args, browser=browser, proxy_pool=proxy_pool,
                    client=client, autosolve=autosolve, user_agent=user_agent,
                )
                await browser.close()
    except Exception:
        log.exception("Unhandled error — this is a crash, not a normal blocked/empty run")
        return EXIT_CRASH

    price_confirmed_pct = None  # not applicable — Pages carry no price (see output_writer.Product docstring)

    return finish_run(
        products=products,
        out_path=args.out,
        fmt=args.format,
        engine=ENGINE_NAME,
        url=(urls[0] if urls else "") if not discover_topic else f"{pp.DISCOVER_URL} (topic={discover_topic})",
        pages_requested=len(urls[: args.max_results]),
        pages_completed=completed,
        failed_pages=failed_pages or None,
        blocked=blocked,
        remote_api_error=remote_api_error,
        allow_empty=args.allow_empty,
        started_at=started_at,
        price_confirmed_pct=price_confirmed_pct,
    )


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
