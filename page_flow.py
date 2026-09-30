#!/usr/bin/env python3
"""page_flow.py — what one fetched article URL MEANS, decided once for all
three engines (CLAUDE.md §1: three copies of this triage would drift, and
the drift would be silent — one engine reporting exit 3 where its twin
reports 0 on the same page).

perplexity.ai answers an article request five ways, all seen live on
2026-09-30:

  1. the app page, and `/rest/article/{ref}` returns the article JSON → a
     full row (`source_used="api"`);
  2. the app page, but the API call failed → a thin row from the rendered
     HTML (`"dom"`), logged as degraded;
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

    if api_error:
        warnings.append(f"{url}: article API HTTP {api_status or '-'}, {api_error} — falling back to the rendered HTML.")
    result = pp.safe_parse_page(html, url=url, article_json=None if api_error else article_json)
    product = result.products[0] if result.products else None

    if product is not None:
        if result.source_used != "api":
            warnings.append(f"{url}: parsed from the rendered HTML only (no counters, no sources).")
        return Outcome(product, False, False, result.source_used, warnings)

    if (http_status is not None and http_status >= 400) or api_status in BLOCKING_API_STATUSES:
        warnings.append(f"{url}: HTTP {http_status or '-'} / article API {api_status or '-'} with no content — blocked, not empty.")
        return Outcome(None, True, False, "none", warnings)

    warnings.append(f"{url}: nothing recognised. Re-run with --dump-html to inspect the captured page.")
    return Outcome(None, False, False, "none", warnings)
