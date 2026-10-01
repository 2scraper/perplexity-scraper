#!/usr/bin/env python3
"""scraper_api_engine.py — `--scraper-api`: fetch through 2Captcha's
Scraper API instead of driving a browser.

The data this repo reads is JSON (`/rest/article/` and the Discover feed),
so a browserless fetch of those URLs is all a row needs. Measured live on
2026-10-01:

  - the Scraper API's OWN pool got Cloudflare's "Just a moment..." page
    (target HTTP 403) on the article page, `/rest/article/` and the feed;
  - routed through a Scraping Browser API profile (`cdpurl`), the same
    URLs answered 200 with the JSON: the feed (20 URLs), four articles
    parsed into full rows, a dead id answered 400. No page had to be
    opened first.

So this mode needs both `TWOCAPTCHA_KEY` (the Scraper API's own auth) and
`PERPLEXITY_CDP_ENDPOINT` (the profile it routes through). It plugs into
the same `page_flow` loop as the browser engines: opening a "page" is a
no-op, `fetch_json` is one Scraper API call, and retries, parsing, exit
codes and the sidecar are unchanged.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import page_flow
from output_writer import EXIT_BAD_USAGE
from proxy_pool import redact_credentials
from scraper_api_client import TwoCaptchaAuthError, TwoCaptchaClient, TwoCaptchaError

log = logging.getLogger("scraper_api_engine")

ENGINE_NAME = "scraper_api"
SCRAPE_TIMEOUT_S = 60  # the Scraper API's own wait for the target (its limit is 1-120s)


class _ScraperApiSession:
    """page_flow.PageSession with no page: only `fetch_json` does work."""

    def __init__(self, engine: "ScraperApiEngine"):
        self.engine = engine

    async def goto(self, url: str) -> Optional[int]:
        return 200  # nothing to open: the JSON endpoints answer on their own

    async def content(self) -> str:
        return ""

    async def fetch_json(self, url: str) -> tuple:
        return await self.engine.fetch_json(url)

    async def wait(self, seconds: float) -> None:
        await self.engine.sleep(seconds)

    async def close(self) -> None:
        return None


class ScraperApiEngine:
    """page_flow.Engine over the Scraper API, routed through one Scraping
    Browser profile. `last_remote_error` says whether the most recent call
    failed on the Scraper API's side (not the site's), so a run that read
    nothing for that reason ends as remote_api_error (exit 5), not empty."""

    name = ENGINE_NAME
    readiness_s = 0.0

    def __init__(self, client: TwoCaptchaClient, cdp_url: str):
        self.client, self.cdp_url = client, cdp_url
        self.last_remote_error = False
        self.fatal: Optional[str] = None  # a bad key or no balance: every later call would fail the same way

    async def open(self, proxy) -> _ScraperApiSession:
        return _ScraperApiSession(self)

    async def sleep(self, seconds: float) -> None:
        if self.fatal is None:  # no point waiting out a refused key
            await asyncio.sleep(seconds)

    async def solve_captcha(self, session, *, html: str, url: str):
        return None  # the profile behind cdpurl solves its own challenges

    async def fetch_json(self, url: str) -> tuple:
        """(target status, decoded JSON or None, error text), like the browser engines' fetch()."""
        if self.fatal is not None:
            self.last_remote_error = True
            return 0, None, self.fatal
        try:
            result = await asyncio.to_thread(
                self.client.scrape_url, url, data_format="raw", timeout=SCRAPE_TIMEOUT_S, cdp_url=self.cdp_url,
            )
        except TwoCaptchaError as exc:
            message = redact_credentials(str(exc))
            if isinstance(exc, TwoCaptchaAuthError) or "insufficient" in message:
                self.fatal = message
                log.error("%s — stopping Scraper API calls for this run.", message)
            self.last_remote_error = True
            return 0, None, message
        self.last_remote_error = False
        status = int(result.target_status or 0)
        try:
            return status, json.loads(result.body), None
        except ValueError:
            return status, None, "response was not JSON (likely a challenge page)"


async def run(args, *, urls, discover_topic: Optional[str], started_at: float) -> int:
    """The whole run in `--scraper-api` mode; the caller has already
    validated the selection and set `args.out`."""
    if not args.twocaptcha_key:
        log.error("--scraper-api needs TWOCAPTCHA_KEY (the Scraper API's own auth) in .env.")
        return EXIT_BAD_USAGE
    if not args.cdp_endpoint:
        log.error("--scraper-api needs PERPLEXITY_CDP_ENDPOINT: on its own pool the Scraper API gets "
                  "Cloudflare's challenge page; routed through a Scraping Browser profile it gets the data.")
        return EXIT_BAD_USAGE
    if args.proxy or args.proxy_file:
        log.warning("Ignoring --proxy: --scraper-api fetches through the Scraping Browser profile.")
    if args.fingerprint:
        log.warning("Ignoring --fingerprint: --scraper-api fetches through the Scraping Browser profile.")
    if args.dump_html:
        log.warning("--dump-html has nothing to save in --scraper-api mode: no page is opened.")
    client = TwoCaptchaClient(args.twocaptcha_key, api_base=args.captcha_api)
    engine = ScraperApiEngine(client, args.cdp_endpoint)
    args.dump_html = False
    return await page_flow.run(engine, args, urls=urls, discover_topic=discover_topic, proxy_pool=None,
                               client=None, started_at=started_at)
