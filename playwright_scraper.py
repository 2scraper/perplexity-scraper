#!/usr/bin/env python3
"""playwright_scraper.py — Playwright engine for the perplexity-scraper
family member. Playwright is the primary engine (see selenium_scraper.py /
puppeteer_scraper.py for parity copies — all three must agree on exit
codes, run status and whether a run crashes or spends money — CLAUDE.md
§4).

**Genuine CLI divergence from every sibling repo (CLAUDE.md §1) — read
this before comparing this file to lidl-scraper's/skyscanner-scraper's/
stockx-scraper's own playwright_scraper.py**: those three all take
`--query`/`--category` and scroll/paginate a search-RESULTS listing.
Perplexity Pages have no site-search mechanism to point a query at —
`robots.txt` explicitly disallows `/*?*q=` and `/search*` for every
crawler, and no `?q=`-style endpoint against perplexity.ai itself surfaces
Pages (see `page_parser.py`'s module docstring for the full research this
is based on). A Page is reachable only via its own specific URL — from an
external search engine's result, or a shared link — so this engine takes
`--url` (one Page) or `--urls-file` (a batch of Page URLs, one per line)
instead. There is no scroll/pagination loop here: each URL is fetched
once, parsed once, done. `--max-results` here caps how many URLs from
`--urls-file` are actually fetched this run, not a product count within
one page (there is exactly zero or one Product per Page).

**No live capture of this site exists yet** (see page_parser.py's module
docstring — direct network access to perplexity.ai is blocked from every
shell available in this environment). `NAV_TIMEOUT_MS`/`READINESS_WAIT_MS`
below are therefore carried over unchanged from lidl-scraper's own
values, not independently measured against perplexity.ai — a reasonable
starting guess for a client-rendered SPA, not a confirmed one. Same for
`BOT_CHALLENGE_MARKERS` (empty, per page_parser.py) — this engine still
wires in the family's generic bot-challenge detection so a real block
found by a future live run is EXIT_BLOCKED (3), not misreported as an
empty/crashed run.

Example:
    python3 playwright_scraper.py --url "https://www.perplexity.ai/page/some-article-AbCdEfGhIjKlMnOpQrStUv" --format json --out results.json
    python3 playwright_scraper.py --urls-file pages.txt --max-results 20 --dump-html
"""
from __future__ import annotations

import argparse
import asyncio
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
import page_parser as pp
from captcha_solver import detect_from_html, solve_when_blocked
from fingerprint_client import fetch_fingerprint, refuse_if_cdp, user_agent_from
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH, Product, finish_run
from proxy_pool import Proxy, ProxyPool, ProxyParseError, is_proxy_dead_error, load_proxies, redact_credentials
from scraper_api_client import TwoCaptchaClient

ENGINE_NAME = "playwright"

# --- the handful of engine constants that vary per site (CLAUDE.md §5) ---
# UNVERIFIED for perplexity.ai — carried over from lidl-scraper's own
# values pending a real capture (see module docstring above).
NAV_TIMEOUT_MS = 30_000
READINESS_WAIT_MS = 3_000
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
    p.add_argument("--url", default=None, help="A single Perplexity Page URL, e.g. https://www.perplexity.ai/page/... (or set PERPLEXITY_URL) — overrides --urls-file")
    p.add_argument("--urls-file", default=None, help="Path to a file with one Page URL per line")
    p.add_argument("--max-results", type=_positive_int, default=30, help="Cap on how many URLs from --urls-file are actually fetched this run")
    p.add_argument("--delay-between-pages", type=_nonnegative_float, default=1.0, help="Politeness delay between fetches when processing more than one URL, seconds")
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
        urls.append(url)
    return urls, skipped


def _dump_path(out_path: str, index: int) -> str:
    stem = Path(out_path).with_suffix("")
    return f"{stem}_debug_{index}.html"


async def _new_context(browser: Browser, proxy: Optional[Proxy], user_agent: Optional[str]) -> BrowserContext:
    kwargs = {}
    if proxy is not None:
        kwargs["proxy"] = proxy.playwright_proxy_dict()
    if user_agent:
        kwargs["user_agent"] = user_agent
    return await browser.new_context(**kwargs)


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
    """Returns (product_or_none, blocked, nav_failed). One Page URL is one
    unit of work — unlike the sibling repos' scroll loop, there is no
    pagination within a single Page, so this is a straight fetch-once,
    parse-once function."""
    blocked = False

    proxy = proxy_pool.next() if proxy_pool else None
    log.info("Fetching %s (proxy: %s)", url, proxy.masked() if proxy else "(no local proxy pool — direct connection, or a --cdp-endpoint session providing its own exit)")
    context = await _new_context(browser, proxy, user_agent)
    page = await context.new_page()
    if autosolve:
        await _enable_scraping_browser_auto_solve(context, page)

    last_error = None
    status = None
    for attempt in range(args.retries + 1):
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            await page.wait_for_timeout(READINESS_WAIT_MS)
            # One gentle scroll-to-bottom-and-back: Perplexity's own Pages
            # announcement describes a scrollable article with images —
            # UNCONFIRMED whether any of it is lazy-loaded, but a single
            # scroll pass is cheap insurance against missing lazy content,
            # same reasoning as lidl-scraper's own scroll loop, just
            # without the repeated "click load more" pagination this site
            # has no equivalent of.
            try:
                await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(500)
                await page.evaluate("() => window.scrollTo(0, 0)")
            except Exception:  # noqa: BLE001 — best-effort only, never fatal to the fetch
                pass
            status = response.status if response is not None else None
            last_error = None
            break
        except Exception as exc:  # noqa: BLE001 — every remote call must be bounded and reported
            last_error = str(exc)
            log.warning("Navigation attempt %d/%d for %s failed: %s", attempt + 1, args.retries + 1, url, last_error)
            if attempt < args.retries:
                await asyncio.sleep(args.retry_delay)

    if last_error is not None:
        await context.close()
        log.error("%s permanently failed to load: %s", url, last_error)
        return None, False, True

    if status is not None and status >= 400:
        log.warning("%s returned HTTP %d — treating as blocked, not empty.", url, status)
        blocked = True
        if proxy_pool is not None and proxy is not None and status in (403, 429):
            proxy_pool.report_failure(proxy, dead=True)
    elif status is not None and proxy_pool is not None and proxy is not None:
        proxy_pool.report_success(proxy)

    html = await page.content()
    captcha_detected = detect_from_html(html, pp.BOT_CHALLENGE_MARKERS)
    result = pp.safe_parse_page(html, url=url)
    if captcha_detected and not result.products:
        blocked = True
    captcha_result = await _maybe_solve_captcha(html=html, url=url, client=client, policy=args.solve_captcha, min_score=args.min_score)
    if captcha_result and captcha_result.get("action") in ("warning_no_key", "warning_solver_error", "detected_unidentified_widget"):
        if not result.products:
            blocked = True
        else:
            # A successful solve, or a challenge marker alongside an
            # already-rendered article, is not a block — same precedent
            # as every sibling repo's own scroll loop.
            blocked = False
    if result.products:
        blocked = False

    if result.source_used == "none" and not blocked:
        log.warning(
            "Nothing recognised on %s — either this isn't a real Page, or "
            "page_parser.py's selectors need updating for the current "
            "perplexity.ai markup (see its module docstring; no live "
            "capture has confirmed them yet). Re-run with --dump-html to "
            "inspect the captured page.", url,
        )

    if args.dump_html:
        Path(_dump_path(args.out, index)).write_text(html, encoding="utf-8")

    await context.close()
    product = result.products[0] if result.products else None
    return product, blocked, False


async def scrape_urls(
    *, urls: List[str], args: argparse.Namespace, browser: Browser,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
    autosolve: bool, user_agent: Optional[str],
) -> tuple:
    """Returns (products, blocked, remote_api_error, pages_completed,
    failed_pages). A single URL's navigation failure degrades to a
    per-page skip (added to failed_pages), never a crash that discards
    every Page already collected from earlier URLs in the same batch —
    same family invariant (CLAUDE.md §6) as every sibling engine's round
    loop, just at "URL in the batch" granularity instead of "scroll
    round"."""
    products: List[Product] = []
    failed_pages: List[int] = []
    any_blocked = False
    completed = 0

    for i, url in enumerate(urls[: args.max_results], start=1):
        product, blocked, nav_failed = await scrape_one_page(
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
        if i < len(urls[: args.max_results]):
            await asyncio.sleep(args.delay_between_pages)

    return products, any_blocked, False, completed, failed_pages


async def run(args: argparse.Namespace) -> int:
    started_at = time.time()
    try:
        urls, skipped_disallowed = _resolve_urls(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_BAD_USAGE
    if not urls:
        if skipped_disallowed:
            print(f"Error: every URL given was disallowed by robots.txt ({skipped_disallowed} skipped) — nothing left to fetch", file=sys.stderr)
        else:
            print("Error: provide --url or --urls-file", file=sys.stderr)
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

            if cdp_connect_failed:
                products, blocked, remote_api_error, completed, failed_pages = [], False, True, 0, []
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
        url=urls[0] if urls else "",
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
