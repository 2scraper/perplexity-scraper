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

**CLI divergence from every sibling repo — see playwright_scraper.py's
module docstring** for why this takes `--url`/`--urls-file` instead of
`--query`/`--category`: Perplexity Pages have no site-search mechanism to
point a query at.
"""
from __future__ import annotations

import argparse
import asyncio
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
import page_parser as pp
from captcha_solver import detect_from_html, solve_when_blocked
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH, Product, finish_run
from fingerprint_client import fetch_fingerprint, refuse_if_cdp, user_agent_from
from proxy_pool import Proxy, ProxyPool, ProxyParseError, is_proxy_dead_error, load_proxies, redact_credentials
from scraper_api_client import TwoCaptchaClient

ENGINE_NAME = "puppeteer"
# UNVERIFIED for perplexity.ai — carried over from lidl-scraper's own
# values pending a real capture (see page_parser.py's module docstring).
NAV_TIMEOUT_MS = 30_000
READINESS_WAIT_S = 3.0

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
    p.add_argument("--url", default=None)
    p.add_argument("--urls-file", default=None)
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
        urls.append(url)
    return urls, skipped


def _dump_path(out_path: str, index: int) -> str:
    stem = Path(out_path).with_suffix("")
    return f"{stem}_debug_{index}.html"


async def _launch(*, headless: bool, proxy: Optional[Proxy], cdp_endpoint: Optional[str]):
    if cdp_endpoint:
        try:
            return await pyppeteer_connect(browserWSEndpoint=cdp_endpoint, defaultViewport=None)
        except Exception as exc:
            raise RuntimeError(f"CDP connection failed: {redact_credentials(str(exc))}") from None
    args = ["--no-sandbox", "--disable-dev-shm-usage"]
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


async def scrape_one_page(
    *, url: str, index: int, args: argparse.Namespace,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient], autosolve: bool = False,
    user_agent: Optional[str] = None,
) -> Tuple[Optional[Product], bool, bool]:
    """Returns (product_or_none, blocked, nav_failed). See
    playwright_scraper.scrape_one_page for the full rationale — one Page
    URL, fetched and parsed once, no pagination within a single Page."""
    blocked = False

    proxy = proxy_pool.next() if proxy_pool else None
    log.info("Fetching %s (proxy: %s)", url, proxy.masked() if proxy else "(no local proxy pool — direct connection, or a --cdp-endpoint session providing its own exit)")
    try:
        browser = await _launch(headless=args.headless, proxy=proxy, cdp_endpoint=args.cdp_endpoint)
    except RuntimeError as exc:
        log.error("CDP connection failed — treating as remote_api_error, not a crash: %s", exc)
        return None, False, False  # remote_api_error is surfaced by the caller when the FIRST url fails to even launch
    page = await browser.newPage()
    if user_agent:
        await page.setUserAgent(user_agent)
    await _authenticate_if_needed(page, proxy)
    if autosolve:
        await _enable_scraping_browser_auto_solve(page)

    last_error = None
    status = None
    for attempt in range(args.retries + 1):
        try:
            response = await page.goto(url, {"waitUntil": "domcontentloaded", "timeout": NAV_TIMEOUT_MS})
            await asyncio.sleep(READINESS_WAIT_S)
            try:
                await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(0.5)
                await page.evaluate("() => window.scrollTo(0, 0)")
            except Exception:  # noqa: BLE001 — best-effort only, never fatal to the fetch
                pass
            status = response.status if response is not None else None
            if proxy_pool is not None and proxy is not None:
                proxy_pool.report_success(proxy)
            last_error = None
            break
        except (NetworkError, PageError, PyppeteerTimeoutError, Exception) as exc:  # noqa: BLE001
            message = str(exc)
            last_error = message
            dead = is_proxy_dead_error(message)
            if proxy_pool is not None and proxy is not None and dead:
                proxy_pool.report_failure(proxy, dead=True)
                log.warning("Proxy reported dead: %s", message)
            else:
                log.warning("Navigation attempt %d/%d for %s failed: %s", attempt + 1, args.retries + 1, url, message)
            if attempt < args.retries:
                await asyncio.sleep(args.retry_delay)

    if last_error is not None:
        await browser.close()
        log.error("%s permanently failed to load: %s", url, last_error)
        return None, False, True

    if status is not None and status >= 400:
        log.warning("%s returned HTTP %d — treating as blocked, not empty.", url, status)
        blocked = True

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
            blocked = False
    if result.products:
        blocked = False

    if result.source_used == "none" and not blocked:
        log.warning(
            "Nothing recognised on %s — either this isn't a real Page, or "
            "page_parser.py's selectors need updating (no live capture has "
            "confirmed them yet). Re-run with --dump-html to inspect the "
            "captured page.", url,
        )

    if args.dump_html:
        Path(_dump_path(args.out, index)).write_text(html, encoding="utf-8")

    await browser.close()
    product = result.products[0] if result.products else None
    return product, blocked, False


async def scrape_urls(
    *, urls: List[str], args: argparse.Namespace,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient], autosolve: bool = False,
    user_agent: Optional[str] = None,
) -> tuple:
    """Returns (products, blocked, remote_api_error, pages_completed,
    failed_pages). See playwright_scraper.scrape_urls for the full
    rationale (a single URL's navigation failure degrades to a per-page
    skip, never a crash that discards the rest of the batch). A
    `--cdp-endpoint` that fails to connect at all fails EVERY url the same
    way (there is only one browser instance for the whole batch here,
    unlike a per-page proxy rotation), so that specific failure IS
    reported as `remote_api_error` for the whole run rather than per-page,
    matching every sibling engine's own connect-once-per-run shape."""
    products: List[Product] = []
    failed_pages: List[int] = []
    any_blocked = False
    completed = 0
    remote_api_error = False

    capped = urls[: args.max_results]
    for i, url in enumerate(capped, start=1):
        if args.cdp_endpoint:
            try:
                probe = await _launch(headless=args.headless, proxy=None, cdp_endpoint=args.cdp_endpoint)
                await probe.close()
            except RuntimeError as exc:
                log.error("CDP connection failed — treating as remote_api_error, not a crash: %s", exc)
                remote_api_error = True
                break
        product, blocked, nav_failed = await scrape_one_page(
            url=url, index=i, args=args, proxy_pool=proxy_pool, client=client, autosolve=autosolve,
            user_agent=user_agent,
        )
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

    return products, any_blocked, remote_api_error, completed, failed_pages


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

    try:
        products, blocked, remote_api_error, completed, failed_pages = await scrape_urls(
            urls=urls, args=args, proxy_pool=proxy_pool, client=client, autosolve=autosolve,
            user_agent=user_agent,
        )
        price_confirmed_pct = None  # not applicable — Pages carry no price (see output_writer.Product docstring)
    except Exception:
        log.exception("Unhandled error — this is a crash, not a normal blocked/empty run")
        return EXIT_CRASH

    return finish_run(
        products=products, out_path=args.out, fmt=args.format, engine=ENGINE_NAME,
        url=urls[0] if urls else "",
        pages_requested=len(urls[: args.max_results]), pages_completed=completed,
        failed_pages=failed_pages or None,
        blocked=blocked, remote_api_error=remote_api_error, allow_empty=args.allow_empty,
        started_at=started_at, price_confirmed_pct=price_confirmed_pct,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = build_arg_parser().parse_args()
    args = env_config.apply_env(args)
    try:
        return asyncio.get_event_loop().run_until_complete(run(args))
    except KeyboardInterrupt:
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())
