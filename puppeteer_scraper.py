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

Same input and the same shared loop as playwright_scraper.py
(`page_flow.run`); this file only provides the pyppeteer page session.
Over `--cdp-endpoint` one connection serves the
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
from typing import Optional

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
import scraper_api_engine
import scraper_api_client
import page_parser as pp
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH
from fingerprint_client import fetch_fingerprint, refuse_if_cdp, user_agent_from
from proxy_pool import Proxy, ProxyPool, ProxyParseError, load_proxies, redact_credentials
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
        description="perplexity.ai article scraper (Pages and Discover) — pyppeteer (Puppeteer) engine",
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
    p.add_argument("--cdp-endpoint", default=None)
    p.add_argument("--scraper-api", action="store_true", help="Fetch through 2Captcha's Scraper API, routed through --cdp-endpoint's Scraping Browser profile, instead of driving this browser (needs TWOCAPTCHA_KEY and PERPLEXITY_CDP_ENDPOINT)")
    p.add_argument("--allow-empty", action="store_true")
    p.add_argument("--dump-html", action="store_true")
    p.add_argument("--headless", dest="headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    return p


def _default_out(fmt: str) -> str:
    return f"perplexity_results.{fmt}"


_resolve_urls = page_flow.resolve_urls  # the one implementation is page_flow's


async def _launch(*, headless: bool, proxy: Optional[Proxy], cdp_endpoint: Optional[str]):
    if cdp_endpoint:
        # pyppeteer's connect() has no timeout of its own and never resolves a
        # refused handshake; connect_with_retry bounds each try, retries a
        # locked profile and names an expired one (CLAUDE.md §26).
        return await scraper_api_client.connect_with_retry(
            lambda: pyppeteer_connect(browserWSEndpoint=cdp_endpoint, defaultViewport=None),
            redact=redact_credentials, log=log,
        )
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
    log.warning("Local captcha solving is disabled: token delivery is not implemented; no paid task created. "
                "Use the Scraping Browser CDP auto-solve integration.")
    return {"action": "unsupported_delivery"}


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


class _PyppeteerSession:
    """page_flow.PageSession over one pyppeteer page; a local session owns
    its browser (one per URL, on that URL's proxy)."""

    def __init__(self, page, browser, *, owns_browser: bool):
        self.page, self.browser, self.owns_browser = page, browser, owns_browser

    async def goto(self, url: str) -> Optional[int]:
        response = await self.page.goto(url, {"waitUntil": "domcontentloaded", "timeout": NAV_TIMEOUT_MS})
        await asyncio.sleep(READINESS_WAIT_S)
        return response.status if response is not None else None

    async def content(self) -> str:
        return await self.page.content()

    async def fetch_json(self, url: str) -> tuple:
        return await _fetch_json(self.page, url)

    async def wait(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def close(self) -> None:
        try:
            await self.page.close()
        except Exception:  # noqa: BLE001 — cleanup only
            pass
        if self.owns_browser:
            await _release(self.browser, remote=False)


class _PyppeteerEngine:
    """page_flow.Engine for pyppeteer: over CDP one connection for the
    run (disconnected at the end, never closed)."""

    name = ENGINE_NAME
    readiness_s = READINESS_WAIT_S

    def __init__(self, args: argparse.Namespace, *, remote_browser, autosolve: bool, user_agent: Optional[str], client):
        self.args, self.remote_browser, self.autosolve, self.user_agent, self.client = args, remote_browser, autosolve, user_agent, client

    async def open(self, proxy) -> _PyppeteerSession:
        browser = self.remote_browser or await _launch(headless=self.args.headless, proxy=proxy, cdp_endpoint=None)
        page = await _open_page(browser, proxy=proxy, user_agent=self.user_agent, autosolve=self.autosolve)
        return _PyppeteerSession(page, browser, owns_browser=self.remote_browser is None)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def solve_captcha(self, session, *, html: str, url: str):
        return await _maybe_solve_captcha(html=html, url=url, client=self.client, policy=self.args.solve_captcha,
                                          min_score=self.args.min_score)


_CLOSED_TARGET_NOISE = ("Target closed", "No session with given id")


def _quiet_target_closed(loop, context) -> None:
    """pyppeteer leaves a detachFromTarget/sendMessageToTarget future
    failing with "Target closed" or "No session with given id" behind a
    page.close() — logged as an ERROR although nothing went wrong (seen on
    every live run). Everything else still reaches the default handler."""
    exc = context.get("exception")
    if isinstance(exc, NetworkError) and any(m in str(exc) for m in _CLOSED_TARGET_NOISE):
        return
    # A refused CDP handshake leaves pyppeteer's own connect task failing
    # after connect_with_retry moved on (CLAUDE.md §26): silence that shape.
    if exc is not None and type(exc).__name__ in ("InvalidStatusCode", "InvalidStatus", "InvalidHandshake", "AbortHandshake"):
        return
    loop.default_exception_handler(context)


async def run(args: argparse.Namespace) -> int:
    started_at = time.time()
    asyncio.get_running_loop().set_exception_handler(_quiet_target_closed)
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
    user_agent = None
    if args.fingerprint and not refuse_if_cdp(args.cdp_endpoint):
        if client is None:
            log.warning("--fingerprint requested but no --twocaptcha-key/TWOCAPTCHA_KEY set — continuing without one.")
        else:
            profile = fetch_fingerprint(client, tags=args.fp_tags, country=args.fp_country)
            if profile:
                user_agent = user_agent_from(profile)

    remote_browser = None
    try:
        if args.cdp_endpoint:
            try:
                remote_browser = await _launch(headless=args.headless, proxy=None, cdp_endpoint=args.cdp_endpoint)
            except RuntimeError as exc:
                log.error("CDP connection failed — treating as remote_api_error, not a crash: %s", exc)
                return page_flow.finish(args, products=[], blocked=False, remote_api_error=True, engine_name=ENGINE_NAME,
                                        urls=urls, discover_topic=discover_topic, started_at=started_at,
                                        pages_completed=0, failed_pages=[])
        engine = _PyppeteerEngine(args, remote_browser=remote_browser,
                                  autosolve=bool(args.cdp_endpoint) and args.solve_captcha != "off",
                                  user_agent=user_agent, client=client)
        return await page_flow.run(engine, args, urls=urls, discover_topic=discover_topic,
                                   proxy_pool=proxy_pool, client=client, started_at=started_at)
    except Exception:
        log.exception("Unhandled error — this is a crash, not a normal blocked/empty run")
        return EXIT_CRASH
    finally:
        if remote_browser is not None:
            await _release(remote_browser, remote=True)


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
