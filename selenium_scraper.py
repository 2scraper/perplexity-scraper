#!/usr/bin/env python3
"""selenium_scraper.py — Selenium engine, parity copy of
playwright_scraper.py (same flags, same exit codes, same status semantics
— see output_writer.finish_run). Selenium is not the primary engine
(Playwright is); it exists for parity, not because it is preferred.

Same hard engine limits as the rest of the family (see CLAUDE.md §6):

  - Selenium CANNOT use an AUTHENTICATED remote CDP endpoint. A
    `--cdp-endpoint` carrying credentials (the Scraping Browser API shape)
    is refused outright here with EXIT_BAD_USAGE — no half-working
    attempt — with a pointer to playwright_scraper.py / puppeteer_scraper.py.
  - Selenium's `--proxy-server` CANNOT authenticate at all. A `--proxy`
    with a login/password has its credentials stripped before being handed
    to Chrome, and this engine WARNS rather than silently dropping them.

Chrome binary: normally auto-detected by Selenium/Selenium Manager from a
regular Chrome/Chromium install. Set `CHROME_BIN` or `SELENIUM_CHROME_BIN`
to point at a specific binary instead; `SELENIUM_CHROMEDRIVER_PATH` /
`CHROMEDRIVER_PATH` pin a specific chromedriver (Selenium Manager's
default network auto-resolve fails outright in an offline/network-
restricted environment — see the constant below).

**CLI divergence from every sibling repo — see playwright_scraper.py's
module docstring** for why this takes `--url`/`--urls-file` instead of
`--query`/`--category`: Perplexity Pages have no site-search mechanism to
point a query at.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

try:
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
except ImportError as _IMPORT_ERROR:  # pragma: no cover — exercised by smoke_test's no-engine path
    webdriver = None
    WebDriverException = Exception
    Options = None
    Service = None
    _SELENIUM_IMPORT_ERROR = _IMPORT_ERROR
else:
    _SELENIUM_IMPORT_ERROR = None

import env_config
import page_flow
import page_parser as pp
from captcha_solver import solve_when_blocked
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH, Product, finish_run
from proxy_pool import Proxy, ProxyPool, ProxyParseError, is_proxy_dead_error, load_proxies, redact_credentials
from fingerprint_client import fetch_fingerprint, refuse_if_cdp, user_agent_from
from scraper_api_client import TwoCaptchaClient

ENGINE_NAME = "selenium"
# UNVERIFIED for perplexity.ai — carried over from lidl-scraper's own
# values pending a real capture (see page_parser.py's module docstring).
NAV_TIMEOUT_S = 45
READINESS_WAIT_S = 3.0
API_TIMEOUT_S = 30
CHALLENGE_WAIT_S = 15

# execute_async_script takes a function BODY; the last argument is the
# callback. Same fetch as the other engines' _FETCH_JS.
_FETCH_ASYNC_JS = """
const [u, timeoutMs, done] = arguments;
const ctl = new AbortController();
const t = setTimeout(() => ctl.abort(), timeoutMs);
fetch(u, {credentials: 'include', headers: {accept: 'application/json'}, signal: ctl.signal})
  .then(r => r.text().then(text => done({status: r.status, text})))
  .catch(e => done({status: 0, error: String(e)}))
  .finally(() => clearTimeout(t));
"""
_CHROME_BINARY = os.environ.get("SELENIUM_CHROME_BIN") or os.environ.get("CHROME_BIN")
_CHROMEDRIVER_PATH = os.environ.get("SELENIUM_CHROMEDRIVER_PATH") or os.environ.get("CHROMEDRIVER_PATH")

# Selenium Manager phones home to plausible.io with usage stats by default
# (confirmed live on stockx-scraper/skyscanner-scraper/lidl-scraper) —
# opted out the same way here, before this module's own SECURITY.md
# promise is tested.
os.environ.setdefault("SE_AVOID_STATS", "true")

log = logging.getLogger("selenium_scraper")


def _positive_int(value: str) -> int:
    ivalue = int(value)
    if ivalue < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer (got {value!r})")
    return ivalue


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="perplexity.ai Pages scraper — Selenium engine",
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
    p.add_argument("--cdp-endpoint", default=None,
                    help="NOTE: refused if it carries credentials — Selenium cannot authenticate a remote CDP session")
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


def _cdp_endpoint_has_credentials(cdp_endpoint: str) -> bool:
    parts = urlparse(cdp_endpoint)
    return bool(parts.username or parts.password)


def _build_driver(*, headless: bool, proxy: Optional[Proxy], cdp_endpoint: Optional[str], user_agent: Optional[str] = None):
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    # Perplexity translates article titles to the browser's language (live:
    # a Russian-locale Mac got a Russian title) — pin English so rows are
    # comparable across machines.
    options.add_argument("--lang=en-US")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    if _CHROME_BINARY:
        options.binary_location = _CHROME_BINARY
    if user_agent:
        options.add_argument(f"--user-agent={user_agent}")

    if cdp_endpoint:
        options.debugger_address = urlparse(cdp_endpoint).netloc.split("@")[-1]
        return webdriver.Chrome(options=options)
    # chromedriver refuses prefs on an attached (debuggerAddress) browser
    options.add_experimental_option("prefs", {"intl.accept_languages": "en-US,en"})

    if proxy is not None:
        if proxy.has_auth:
            log.warning(
                "Selenium's --proxy-server cannot authenticate — using %s:%s "
                "with credentials STRIPPED, not silently dropped.",
                proxy.host, proxy.port,
            )
        options.add_argument(f"--proxy-server={proxy.server_only()}")

    if Service and _CHROMEDRIVER_PATH:
        service = Service(executable_path=_CHROMEDRIVER_PATH)
    elif Service:
        service = Service()  # Selenium Manager: resolves/downloads over the network
    else:
        service = None
    return webdriver.Chrome(service=service, options=options) if service else webdriver.Chrome(options=options)


_STATUS_JS = (
    "try { return performance.getEntriesByType('navigation')[0].responseStatus || 0; } "
    "catch (e) { return 0; }"
)


def _maybe_solve_captcha(*, html: str, url: str, client: Optional[TwoCaptchaClient], policy: str, min_score: float = 0.3) -> Optional[dict]:
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
        log.warning(
            "Captcha token obtained but NOT auto-injected on the Selenium "
            "engine (unverified widget-specific step) — use "
            "playwright_scraper.py or puppeteer_scraper.py with "
            "--cdp-endpoint for the Scraping Browser API's built-in solve."
        )
    return result


def _fetch_json(driver, url: str) -> tuple:
    """(status, decoded JSON or None, error text) — see playwright_scraper._fetch_json."""
    try:
        driver.set_script_timeout(API_TIMEOUT_S + 5)
        res = driver.execute_async_script(_FETCH_ASYNC_JS, url, API_TIMEOUT_S * 1000) or {}
    except WebDriverException as exc:
        return 0, None, str(exc).splitlines()[0]
    status = int(res.get("status") or 0)
    if status == 0:
        return 0, None, res.get("error") or "fetch failed"
    try:
        return status, json.loads(res.get("text") or ""), None
    except ValueError:
        return status, None, "response was not JSON (likely a challenge page)"


def _page_source(driver) -> str:
    try:
        return driver.page_source
    except WebDriverException:
        return ""


def scrape_one_page(
    *, url: str, index: int, args: argparse.Namespace, driver,
    proxy: Optional[Proxy], proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
) -> Tuple[Optional[Product], bool, bool, bool]:
    """Returns (product_or_none, blocked, nav_failed, not_found) — see
    playwright_scraper.scrape_one_page."""
    log.info("Fetching %s (proxy: %s)", url, proxy.masked() if proxy else "(none — direct connection)")
    last_error = None
    status = None
    for attempt in range(args.retries + 1):
        try:
            driver.set_page_load_timeout(NAV_TIMEOUT_S)
            driver.get(url)
            time.sleep(READINESS_WAIT_S)
            try:
                reported = driver.execute_script(_STATUS_JS)
                status = int(reported) if reported else None
            except WebDriverException:
                pass
            last_error = None
            break
        except WebDriverException as exc:
            last_error = redact_credentials(str(exc).splitlines()[0])
            if proxy_pool is not None and proxy is not None and is_proxy_dead_error(last_error):
                proxy_pool.report_failure(proxy, dead=True)
            log.warning("Navigation attempt %d/%d for %s failed: %s", attempt + 1, args.retries + 1, url, last_error)
            if attempt < args.retries:
                time.sleep(args.retry_delay)
    if last_error is not None:
        log.error("%s permanently failed to load: %s", url, last_error)
        return None, False, True, False

    html = _page_source(driver)
    waited = 0.0
    while page_flow.is_challenge(html) and waited < CHALLENGE_WAIT_S:
        time.sleep(1)
        waited += 1
        html = _page_source(driver) or html
    if waited and not page_flow.is_challenge(html):
        log.info("%s: Cloudflare challenge cleared by itself after %.0fs.", url, waited)
    if page_flow.is_challenge(html):
        captcha_result = _maybe_solve_captcha(html=html, url=url, client=client, policy=args.solve_captcha, min_score=args.min_score)
        if captcha_result and captcha_result.get("action") == "solved":
            time.sleep(READINESS_WAIT_S)
            html = _page_source(driver) or html

    _topic, ref = pp.article_ref(url)
    api_status, article_json, api_error = (0, None, "skipped: page is a bot challenge")
    if not page_flow.is_challenge(html):
        api_status, article_json, api_error = _fetch_json(driver, pp.article_api_url(ref))
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


def collect_discover_urls(*, topic: str, limit: int, args: argparse.Namespace, driver) -> tuple:
    """(urls, error) — see playwright_scraper.collect_discover_urls."""
    urls: List[str] = []
    try:
        driver.set_page_load_timeout(NAV_TIMEOUT_S)
        driver.get(pp.DISCOVER_URL)
        time.sleep(READINESS_WAIT_S)
    except WebDriverException as exc:
        return [], f"could not open {pp.DISCOVER_URL}: {redact_credentials(str(exc).splitlines()[0])}"
    offset = 0
    while len(urls) < limit:
        status, data, error = _fetch_json(driver, pp.discover_feed_api_url(topic, offset=offset))
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
        time.sleep(args.delay_between_pages)
    return urls[:limit], None


def _quit(driver) -> None:
    try:
        driver.quit()
    except Exception:  # noqa: BLE001 — cleanup only
        pass


def scrape_urls(
    *, urls: List[str], args: argparse.Namespace,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
    user_agent: Optional[str] = None,
) -> tuple:
    """Returns (products, blocked, remote_api_error, pages_completed,
    failed_pages). A fresh driver per URL, on its own exit (a rotation is a
    fresh browser — CLAUDE.md §8)."""
    products: List[Product] = []
    failed_pages: List[int] = []
    any_blocked = False
    completed = 0

    capped = urls[: args.max_results]
    for i, url in enumerate(capped, start=1):
        proxy = proxy_pool.next() if proxy_pool else None
        driver = _build_driver(headless=args.headless, proxy=proxy, cdp_endpoint=args.cdp_endpoint, user_agent=user_agent)
        try:
            product, blocked, nav_failed, _not_found = scrape_one_page(
                url=url, index=i, args=args, driver=driver, proxy=proxy, proxy_pool=proxy_pool, client=client,
            )
        finally:
            _quit(driver)
        if nav_failed:
            failed_pages.append(i)
        else:
            completed += 1
            if blocked:
                any_blocked = True
            if product is not None:
                products.append(product)
        if i < len(capped):
            time.sleep(args.delay_between_pages)

    return products, any_blocked, False, completed, failed_pages


def run(args: argparse.Namespace) -> int:
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
    if args.cdp_endpoint and _cdp_endpoint_has_credentials(args.cdp_endpoint):
        print(
            "Error: --cdp-endpoint carries credentials — Selenium/chromedriver's "
            "debuggerAddress takes a bare host:port and cannot authenticate a "
            "remote session. Use playwright_scraper.py or puppeteer_scraper.py "
            "for the Scraping Browser API.", file=sys.stderr,
        )
        return EXIT_BAD_USAGE
    if webdriver is None:
        print(f"Error: selenium is not installed ({_SELENIUM_IMPORT_ERROR}). "
              f"pip install -r requirements-selenium.txt", file=sys.stderr)
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

    user_agent = None
    cdp_refused_fingerprint = refuse_if_cdp(args.cdp_endpoint)
    if args.fingerprint and not cdp_refused_fingerprint:
        if client is None:
            log.warning("--fingerprint requested but no --twocaptcha-key/TWOCAPTCHA_KEY set — continuing without one.")
        else:
            profile = fetch_fingerprint(client, tags=args.fp_tags, country=args.fp_country)
            if profile:
                user_agent = user_agent_from(profile)

    products, blocked, remote_api_error, completed, failed_pages = [], False, False, 0, []
    try:
        if discover_topic:
            proxy = proxy_pool.next() if proxy_pool else None
            driver = _build_driver(headless=args.headless, proxy=proxy, cdp_endpoint=args.cdp_endpoint, user_agent=user_agent)
            try:
                urls, discover_error = collect_discover_urls(topic=discover_topic, limit=args.max_results, args=args, driver=driver)
            finally:
                _quit(driver)
            if discover_error:
                log.error("Discover feed unavailable — treating as remote_api_error: %s", discover_error)
                remote_api_error = True
            elif not urls:
                log.warning("Discover topic %r returned no articles.", discover_topic)
            else:
                log.info("Discover topic %r: %d article URL(s) to fetch.", discover_topic, len(urls))
        if not remote_api_error:
            products, blocked, remote_api_error, completed, failed_pages = scrape_urls(
                urls=urls, args=args, proxy_pool=proxy_pool, client=client, user_agent=user_agent,
            )
        price_confirmed_pct = None  # not applicable — articles carry no price (see output_writer.Product docstring)
    except Exception:
        log.exception("Unhandled error — this is a crash, not a normal blocked/empty run")
        return EXIT_CRASH

    return finish_run(
        products=products, out_path=args.out, fmt=args.format, engine=ENGINE_NAME,
        url=(urls[0] if urls else "") if not discover_topic else f"{pp.DISCOVER_URL} (topic={discover_topic})",
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
        return run(args)
    except KeyboardInterrupt:
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())
