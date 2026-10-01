#!/usr/bin/env python3
"""page_flow.py — what one fetched article URL MEANS, decided once for all
three engines (CLAUDE.md §1: three copies of this triage would drift, and
the drift would be silent — one engine reporting exit 3 where its twin
reports 0 on the same page).

perplexity.ai answers an article request these ways, all seen live on
2026-09-30:

  1. the app page, and `/rest/article/{ref}` returns the article JSON → a
     full row (`source_used="api"`);
  2. the app page, but the API call keeps failing → NO row. The rendered
     HTML is not evidence of WHICH article it is: a dead URL rendered the
     previously viewed article's headings in full, and the HTML carries
     no id to check against. A row with another article's title is worse
     than no row (CLAUDE.md §8, "fail loudly");
  3. the app page, and the API answers 400 → the article does not exist
     (`not_found`): no row, not a block, never retried harder;
  4. a Cloudflare managed challenge that never cleared (local Chromium,
     headless or headful, from a residential Mac) → `blocked`. Its HTML is
     never parsed: its own heading once came out as an article titled
     "Performing security verification";
  5. an HTTP >= 400 or an API 401/403/429 with nothing usable → `blocked`.

Pure: no driver, no JavaScript, no I/O — the engines pass in what they saw.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

import page_parser as pp
from captcha_solver import detect_from_html
from output_writer import Product

BLOCKING_API_STATUSES = (401, 403, 429)
NOT_FOUND_API_STATUSES = (400, 404)
# /rest/ answers 403 in two live situations: a fresh profile before the
# page's own bot check has issued cf_clearance (403 at ~3s, 200 at ~12s),
# and a burst — 13 articles at 0.5s apart, then 403 on every call until
# the profile was left alone for a few minutes. The engines retry the API
# call after each of these pauses; the long last one is for the burst.
API_RETRY_DELAYS_S = (3, 5, 8, 15)


def should_retry_api(status: int, error: Optional[str]) -> bool:
    """Worth another try after a pause: refused or unreadable, not "no such article"."""
    if status in NOT_FOUND_API_STATUSES or (status == 200 and not error):
        return False
    return status in BLOCKING_API_STATUSES or status == 0 or bool(error)


def is_challenge(html: str) -> bool:
    """A bot-challenge marker on a page that is NOT the app. The app shell
    itself may mention challenge vendors; a page built from the app's own
    assets is never the interstitial."""
    return detect_from_html(html or "", pp.BOT_CHALLENGE_MARKERS) and not pp.is_app_page(html or "")


@dataclass
class Outcome:
    product: Optional[Product]
    blocked: bool
    not_found: bool
    source_used: str
    warnings: List[str] = field(default_factory=list)


def decide(
    *, url: str, http_status: Optional[int], html: str, api_status: int,
    article_json: Any, api_error: Optional[str],
) -> Outcome:
    warnings: List[str] = []
    if api_status in NOT_FOUND_API_STATUSES:
        return Outcome(None, False, True, "none",
                       [f"{url}: the article API answered HTTP {api_status} — no such article (page_not_found)."])

    challenged = is_challenge(html)
    if challenged:
        return Outcome(None, True, False, "none",
                       [f"{url}: served a bot challenge that did not clear — blocked."])

    result = pp.safe_parse_page(html, url=url, article_json=None if api_error else article_json)
    product = result.products[0] if result.source_used == "api" else None
    if product is not None:
        return Outcome(product, False, False, "api", warnings)

    detail = f"article API HTTP {api_status or '-'}" + (f", {api_error}" if api_error else "")
    if (http_status is not None and http_status >= 400) or api_status in BLOCKING_API_STATUSES:
        warnings.append(f"{url}: {detail} — blocked, not empty.")
        return Outcome(None, True, False, "none", warnings)

    warnings.append(f"{url}: {detail} — no article read. Re-run with --dump-html to inspect the captured page.")
    return Outcome(None, False, False, "none", warnings)


# --------------------------------------------------------------------------- #
# The ONE fetch loop (CLAUDE.md §26). Until 2026-09-30 each engine carried
# its own copy of everything below. Each engine now passes an `Engine`
# (open a page session, sleep, solve a generic captcha) whose sessions
# provide NAMED operations — goto, content, fetch_json, wait, close — and
# no JavaScript crosses this boundary: each engine spells its own fetch()
# in its own driver's dialect.
# --------------------------------------------------------------------------- #
import logging as _logging
from pathlib import Path as _Path
from typing import Protocol as _Protocol

from output_writer import finish_run as _finish_run
from proxy_pool import is_proxy_dead_error as _is_proxy_dead_error, redact_credentials as _redact

_log = _logging.getLogger("page_flow")

CHALLENGE_WAIT_S = 15  # a Cloudflare managed challenge can clear by itself; give it this long


class PageSession(_Protocol):
    async def goto(self, url: str) -> Optional[int]: ...  # HTTP status or None; raises on failure
    async def content(self) -> str: ...
    async def fetch_json(self, url: str) -> tuple: ...  # (status, decoded JSON or None, error text)
    async def wait(self, seconds: float) -> None: ...
    async def close(self) -> None: ...


class Engine(_Protocol):
    name: str
    readiness_s: float

    async def open(self, proxy) -> PageSession: ...
    async def sleep(self, seconds: float) -> None: ...
    async def solve_captcha(self, session: PageSession, *, html: str, url: str) -> Optional[dict]: ...


class SolveBudget:
    """One cap on PAID captcha solves for the whole run (CLAUDE.md §23: a
    per-page limit nothing sums is a bill). `limit=0` means never pay."""

    def __init__(self, limit: int):
        self.limit = max(0, int(limit))
        self.spent = 0

    def remaining(self) -> int:
        return max(0, self.limit - self.spent)

    def spend(self) -> None:
        self.spent += 1


def resolve_urls(args) -> tuple:
    """(urls, skipped). `--url` wins over `--urls-file`. A robots.txt-
    disallowed path or a non-article URL is logged and skipped, never
    fetched."""
    if args.url:
        candidates = [args.url]
    elif args.urls_file:
        try:
            lines = _Path(args.urls_file).read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ValueError(f"could not read --urls-file {args.urls_file!r}: {exc}")
        candidates = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
    else:
        return [], 0
    urls, skipped = [], 0
    for url in candidates:
        if pp.is_disallowed_path(url):
            _log.warning("Skipping %s — its path is disallowed by perplexity.ai's robots.txt; this tool never requests a disallowed path.", url)
            skipped += 1
            continue
        if not pp.is_page_url(url):
            _log.warning("Skipping %s — not a perplexity.ai article URL (/page/... or /discover/{topic}/...).", url)
            skipped += 1
            continue
        urls.append(url)
    return urls, skipped


def _dump_path(out_path: str, index: int) -> str:
    return f"{_Path(out_path).with_suffix('')}_debug_{index}.html"


async def _solve_within_budget(engine: Engine, session: PageSession, args, *, html: str, url: str) -> Optional[dict]:
    budget = getattr(args, "_solve_budget", None)
    if budget is not None and budget.remaining() == 0:
        _log.warning("Captcha solving skipped: the run's solve budget is spent (--max-solves %d).", budget.limit)
        return None
    result = await engine.solve_captcha(session, html=html, url=url)
    if budget is not None and result and result.get("action") in ("solved", "warning_solver_error"):
        budget.spend()  # a task was created and billed, whatever came back
    return result


async def fetch_article(engine: Engine, args, url: str, index: int, proxy_pool, client) -> tuple:
    """One article URL: (product_or_none, blocked, nav_failed, not_found).
    Open the page (its cookies clear Cloudflare), wait out a challenge
    (bounded), solve one within budget, then read /rest/article/ with the
    page's own fetch(), retrying a refusal (API_RETRY_DELAYS_S)."""
    proxy = proxy_pool.next() if proxy_pool else None
    _log.info("Fetching %s (proxy: %s)", url, proxy.masked() if proxy else "(none — direct, or the --cdp-endpoint session's own exit)")
    try:
        session = await engine.open(proxy)
    except RuntimeError as exc:
        _log.error("Browser connection failed — treating this URL as failed, not a crash: %s", exc)
        return None, False, True, False
    try:
        last_error, status = None, None
        for attempt in range(args.retries + 1):
            try:
                status = await session.goto(url)
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 — every remote call must be bounded and reported
                last_error = _redact(str(exc)).splitlines()[0] if str(exc) else type(exc).__name__
                if proxy_pool is not None and proxy is not None and _is_proxy_dead_error(last_error):
                    proxy_pool.report_failure(proxy, dead=True)
                    _log.warning("Proxy reported dead: %s", last_error)
                else:
                    _log.warning("Navigation attempt %d/%d for %s failed: %s", attempt + 1, args.retries + 1, url, last_error)
                if attempt < args.retries:
                    await engine.sleep(args.retry_delay)
        if last_error is not None:
            _log.error("%s permanently failed to load: %s", url, last_error)
            return None, False, True, False

        html = await session.content()
        waited = 0.0
        while is_challenge(html) and waited < CHALLENGE_WAIT_S:
            await session.wait(1)
            waited += 1
            try:
                html = await session.content()
            except Exception:  # noqa: BLE001 — mid-navigation while the challenge redirects
                continue
        if waited and not is_challenge(html):
            _log.info("%s: Cloudflare challenge cleared by itself after %.0fs.", url, waited)
        if is_challenge(html) and args.solve_captcha != "off":
            result = await _solve_within_budget(engine, session, args, html=html, url=url)
            if result and result.get("action") == "solved":
                await session.wait(engine.readiness_s)
                html = await session.content()

        _topic, ref = pp.article_ref(url)
        api_status, article_json, api_error = (0, None, "skipped: page is a bot challenge")
        if not is_challenge(html):
            api_status, article_json, api_error = await session.fetch_json(pp.article_api_url(ref))
            for delay in API_RETRY_DELAYS_S:
                if not should_retry_api(api_status, api_error):
                    break
                _log.info("%s: article API HTTP %s — retrying in %ss (a fresh profile needs the page's own bot check first).", url, api_status or "-", delay)
                await engine.sleep(delay)
                api_status, article_json, api_error = await session.fetch_json(pp.article_api_url(ref))
        outcome = decide(url=url, http_status=status, html=html, api_status=api_status,
                         article_json=article_json, api_error=api_error)
        for message in outcome.warnings:
            _log.warning("%s", message)
        if proxy_pool is not None and proxy is not None:
            if outcome.blocked and status in (403, 429):
                proxy_pool.report_failure(proxy, dead=True)
            elif not outcome.blocked:
                proxy_pool.report_success(proxy)
        if args.dump_html:
            _Path(_dump_path(args.out, index)).write_text(html, encoding="utf-8")
        return outcome.product, outcome.blocked, False, outcome.not_found
    finally:
        await session.close()


async def collect_discover(engine: Engine, args, topic: str, limit: int, proxy_pool) -> tuple:
    """(urls, error) from the Discover feed, 20 per call by offset, until
    `limit` distinct URLs or the feed runs out. `error` only when even the
    first page could not be read."""
    proxy = proxy_pool.next() if proxy_pool else None
    try:
        session = await engine.open(proxy)
    except RuntimeError as exc:
        return [], f"browser connection failed: {exc}"
    urls: List[str] = []
    try:
        try:
            await session.goto(pp.DISCOVER_URL)
        except Exception as exc:  # noqa: BLE001
            return [], f"could not open {pp.DISCOVER_URL}: {_redact(str(exc)).splitlines()[0]}"
        offset = 0
        while len(urls) < limit:
            status, data, error = await session.fetch_json(pp.discover_feed_api_url(topic, offset=offset))
            if error or status != 200:
                if not urls:
                    return [], f"Discover feed HTTP {status or '-'}: {error or 'unexpected status'}"
                _log.warning("Discover feed page at offset %d failed (HTTP %s) — keeping the %d URLs collected.", offset, status or "-", len(urls))
                break
            page_urls, has_more = pp.parse_discover_feed(data, topic=topic)
            new = [u for u in page_urls if u not in urls]
            urls.extend(new)
            if not new or not has_more:
                break
            offset += len(page_urls)
            await engine.sleep(args.delay_between_pages)
        return urls[:limit], None
    finally:
        await session.close()


async def run(engine: Engine, args, *, urls: List[str], discover_topic: Optional[str], proxy_pool, client,
              started_at: float) -> int:
    """Discover (if asked), every URL in order, then the one finish_run."""
    remote_api_error = False
    if discover_topic:
        urls, error = await collect_discover(engine, args, discover_topic, args.max_results, proxy_pool)
        if error:
            _log.error("Discover feed unavailable — treating as remote_api_error: %s", error)
            remote_api_error = True
        elif not urls:
            _log.warning("Discover topic %r returned no articles.", discover_topic)
        else:
            _log.info("Discover topic %r: %d article URL(s) to fetch.", discover_topic, len(urls))
    products: List[Product] = []
    failed_pages: List[int] = []
    any_blocked = False
    completed = 0
    batch = [] if remote_api_error else urls[: args.max_results]
    for i, url in enumerate(batch, start=1):
        product, blocked, nav_failed, _not_found = await fetch_article(engine, args, url, i, proxy_pool, client)
        if nav_failed:
            failed_pages.append(i)
        else:
            completed += 1
            any_blocked = any_blocked or blocked
            if product is not None:
                products.append(product)
        if i < len(batch):
            await engine.sleep(args.delay_between_pages)
    return finish(args, products=products, blocked=any_blocked, remote_api_error=remote_api_error,
                  engine_name=engine.name, urls=urls, discover_topic=discover_topic, started_at=started_at,
                  pages_completed=completed, failed_pages=failed_pages)


def finish(args, *, products: List[Product], blocked: bool, remote_api_error: bool, engine_name: str,
           urls: List[str], discover_topic: Optional[str], started_at: float, pages_completed: int,
           failed_pages: List[int]) -> int:
    budget = getattr(args, "_solve_budget", None)
    label = f"{pp.DISCOVER_URL} (topic={discover_topic})" if discover_topic else (urls[0] if urls else "")
    return _finish_run(
        products=products, out_path=args.out, fmt=args.format, engine=engine_name, url=label,
        pages_requested=len(urls[: args.max_results]), pages_completed=pages_completed,
        failed_pages=failed_pages or None, blocked=blocked, remote_api_error=remote_api_error,
        allow_empty=args.allow_empty, started_at=started_at, price_confirmed_pct=None,
        max_results=args.max_results,
        extra_meta={"solves_spent": budget.spent if budget is not None else 0, "discover_topic": discover_topic},
    )


def validate_common(args, *, urls, skipped, print_err) -> Optional[int]:
    """The usage checks every engine makes before launching anything;
    returns an exit code to stop with, or None to go on."""
    from output_writer import EXIT_BAD_USAGE
    discover_topic = None if urls or args.url or args.urls_file else args.discover
    if not urls and not discover_topic:
        if skipped:
            print_err(f"Error: none of the URLs given is a fetchable article ({skipped} skipped) — nothing left to fetch")
        else:
            print_err("Error: provide --url, --urls-file or --discover")
        return EXIT_BAD_USAGE
    if args.format not in ("json", "csv"):
        print_err(f"Error: unsupported --format {args.format!r}")
        return EXIT_BAD_USAGE
    return None
