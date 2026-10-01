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
import asyncio
import json
import logging
import os
import sys
import time
from typing import Optional
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
import scraper_api_engine
import page_parser as pp
from captcha_solver import solve_when_blocked
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH
from proxy_pool import Proxy, ProxyPool, ProxyParseError, load_proxies
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
        description="perplexity.ai article scraper (Pages and Discover) — Selenium engine",
        epilog="Credentials belong in .env / PERPLEXITY_PROXY / TWOCAPTCHA_KEY — never on this command line.",
    )
    p.add_argument("--url", default=None, help="One article URL (/page/... or /discover/{topic}/...) — overrides --urls-file/--discover")
    p.add_argument("--urls-file", default=None)
    p.add_argument("--discover", default=None, metavar="TOPIC", help="Scrape the Discover feed for TOPIC ('top'), up to --max-results articles")
    p.add_argument("--max-results", type=_positive_int, default=30)
    p.add_argument("--delay-between-pages", type=float, default=2.0)
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
    p.add_argument("--max-solves", type=int, default=8, help="Cap on PAID 2Captcha solves for the whole run (0 = never pay); recorded as solves_spent")
    p.add_argument("--min-score", type=float, default=0.3, help="Minimum acceptable reCAPTCHA v3 score (2Captcha's minScore task field)")
    p.add_argument("--fingerprint", action="store_true", help="Fetch and apply a 2Captcha Fingerprint API profile's user agent (ignored with --cdp-endpoint — see fingerprint_client.refuse_if_cdp)")
    p.add_argument("--fp-tags", default=None, help="Fingerprint API filter, e.g. 'Windows'")
    p.add_argument("--fp-country", default=None, help="Fingerprint API filter, e.g. 'us'")
    p.add_argument("--cdp-endpoint", default=None,
                    help="NOTE: refused if it carries credentials — Selenium cannot authenticate a remote CDP session")
    p.add_argument("--scraper-api", action="store_true", help="Fetch through 2Captcha's Scraper API, routed through --cdp-endpoint's Scraping Browser profile, instead of driving this browser (needs TWOCAPTCHA_KEY and PERPLEXITY_CDP_ENDPOINT)")
    p.add_argument("--allow-empty", action="store_true")
    p.add_argument("--dump-html", action="store_true")
    p.add_argument("--headless", dest="headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    return p


def _default_out(fmt: str) -> str:
    return f"perplexity_results.{fmt}"


_resolve_urls = page_flow.resolve_urls  # the one implementation is page_flow's


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


def _quit(driver) -> None:
    try:
        driver.quit()
    except Exception:  # noqa: BLE001 — cleanup only
        pass


class _SeleniumSession:
    """page_flow.PageSession over one Selenium driver (blocking WebDriver
    calls inside coroutines; the shared loop runs under asyncio.run)."""

    def __init__(self, driver):
        self.driver = driver

    async def goto(self, url: str) -> Optional[int]:
        self.driver.set_page_load_timeout(NAV_TIMEOUT_S)
        self.driver.get(url)
        time.sleep(READINESS_WAIT_S)
        try:
            reported = self.driver.execute_script(_STATUS_JS)
            return int(reported) if reported else None
        except WebDriverException:
            return None

    async def content(self) -> str:
        return _page_source(self.driver)

    async def fetch_json(self, url: str) -> tuple:
        return _fetch_json(self.driver, url)

    async def wait(self, seconds: float) -> None:
        time.sleep(seconds)

    async def close(self) -> None:
        _quit(self.driver)


class _SeleniumEngine:
    """page_flow.Engine for Selenium: a fresh driver per URL."""

    name = ENGINE_NAME
    readiness_s = READINESS_WAIT_S

    def __init__(self, args: argparse.Namespace, *, user_agent: Optional[str], client):
        self.args, self.user_agent, self.client = args, user_agent, client

    async def open(self, proxy) -> _SeleniumSession:
        return _SeleniumSession(_build_driver(headless=self.args.headless, proxy=proxy,
                                              cdp_endpoint=self.args.cdp_endpoint, user_agent=self.user_agent))

    async def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    async def solve_captcha(self, session, *, html: str, url: str):
        return _maybe_solve_captcha(html=html, url=url, client=self.client, policy=self.args.solve_captcha,
                                    min_score=self.args.min_score)


def run(args: argparse.Namespace) -> int:
    started_at = time.time()
    args._solve_budget = page_flow.SolveBudget(args.max_solves)
    try:
        urls, skipped_disallowed = _resolve_urls(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_BAD_USAGE
    stop = page_flow.validate_common(args, urls=urls, skipped=skipped_disallowed, print_err=lambda m: print(m, file=sys.stderr))
    if stop is not None:
        return stop
    discover_topic = None if urls or args.url or args.urls_file else args.discover
    if args.scraper_api:
        args.out = args.out or _default_out(args.format)
        return asyncio.run(scraper_api_engine.run(args, urls=urls, discover_topic=discover_topic, started_at=started_at))
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

    try:
        return asyncio.run(page_flow.run(_SeleniumEngine(args, user_agent=user_agent, client=client), args, urls=urls,
                                         discover_topic=discover_topic, proxy_pool=proxy_pool, client=client,
                                         started_at=started_at))
    except Exception:
        log.exception("Unhandled error — this is a crash, not a normal blocked/empty run")
        return EXIT_CRASH


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
