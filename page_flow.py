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
