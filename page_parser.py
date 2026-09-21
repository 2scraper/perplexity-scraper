#!/usr/bin/env python3
"""page_parser.py — this IS the perplexity.ai site knowledge (the wiki-
article-family analog of lidl-scraper's lidl_parser.py, skyscanner-
scraper's flight_parser.py, and stockx-scraper's product_parser.py).

**Honesty note, read before trusting anything below** (CLAUDE.md §15: an
unverified site gets that stated plainly, not glossed over).

Written 2026-09-21, and **still no confirmed capture of a real, rendered
Perplexity Page** — every selector below remains a best-effort guess,
marked `# TODO: verify live`. Direct HTTP access to perplexity.ai is
blocked from every plain-`curl`-style shell available while building this
(the cloud sandbox's own network egress and the device-bridge VM on
Roman's machine both hit a proxy-level 403); only research through
WebFetch/WebSearch was possible for the article text above, and
Perplexity's own app is a client-rendered SPA — those tools only ever saw
an empty shell for actual `/page/...` URLs, never a real Page's rendered
HTML.

**UPDATE, same day, first live capture of the SITE ITSELF (not yet of
this repo's own scrapers) — a real, live incident, not a guess.** A
full browser-rendering tool (not a `curl`-style shell — see TESTING.md
for the distinction) reached `perplexity.ai` and got served a genuine
Cloudflare "managed challenge" interstitial (`cType: 'managed'`, Ray ID
`a3e80852cfb8ae37`) instead of any Page content, on **two separate,
freshly-opened attempts against two different `/page/...` URLs** — both
redirected to the bare origin and served the identical challenge page
("Один момент…" / "Just a moment..."), which rules out a one-off fluke or
a URL-specific block. This is now a confirmed, live, real finding — see
`BOT_CHALLENGE_MARKERS` below, `CHANGELOG.md`'s dated entry, and
`tests/fixtures/perplexity_cloudflare_block_real.html` for the captured
page. `captcha_solver.detect_from_html()` was run against this exact
captured HTML and correctly returns `True` (three markers hit:
`"cf-turnstile"`, `"challenges.cloudflare.com"`,
`"cdn-cgi/challenge-platform"`) — this repo's block detection would not
have silently misreported this run as `empty`.

**What this DOES confirm**: perplexity.ai fronts at least some requests
with a Cloudflare managed challenge, and this family's generic detector
already catches it (exit `3`, not `4`). **What this does NOT confirm**:
whether `playwright_scraper.py`'s own headless request (a different
client than the browser-rendering tool used here) gets the same
treatment, whether it ever clears on its own without solving anything (a
"managed" challenge sometimes passes silently for a client Cloudflare
trusts), or any selector below — this was a block page, not a results
page, so the actual parsing logic is exactly as unverified as before.
**The first real run of this repo's own engines against this site still
has to come from a human's own terminal** (outside any tool-mediated
shell), exactly as it did for lidl.com/skyscanner.com/stockx.com before
those files' selectors could be trusted — see `TESTING.md` step 2 for
what that run needs to check now that a block is a live, confirmed
possibility and not just a theoretical exit code.

What IS confirmed, from `robots.txt`, the sitemap, and third-party/
first-party writeups (see README for the full source list):

  - Perplexity Pages are real, public, wiki-style articles at
    `https://www.perplexity.ai/page/{slug}-{22-char-id}` — NOT blocked by
    `robots.txt` (only `/search*`, `/search/new`, and a few onboarding
    paths are disallowed; `/page/...` is not on that list).
  - A Page has: a title, section headings, images, body text, a sources/
    citations list ("links to the resources used in the preparation of
    the material" — Perplexity's own announcement blog's wording), a
    page-view count, a follow-up-question count, and author/creator
    attribution. An interactive "Ask AI" box is also present but is a
    LIVE feature, not static content — deliberately not scraped by this
    tool (see README's scope section).
  - **Confirmed architectural fact, not a guess**: Pages have NO site-
    search mechanism analogous to lidl/skyscanner/stockx's query-based
    listings. `robots.txt` explicitly disallows `/*?*q=` and `/search*`
    for every crawler, and no `?q=`-style endpoint against Perplexity
    itself surfaces Pages — they're reachable only via a specific URL
    (an external search engine's result, or a shared link). This is why
    this repo's engines take `--url` / `--urls-file` instead of the
    sibling repos' `--query` — a genuine, documented divergence
    (CLAUDE.md §1), not a silent one.

Everything else below (selectors, JSON-LD shape, OG-meta shape, the exact
DOM structure of the sources list and the view/follow-up counters) is an
UNCONFIRMED best-effort guess, laid out in priority order the same way
every sibling parser is — embedded-data-first, DOM fallback last — so a
future real capture can confirm or replace each path without restructuring
the file:

  1. `extract_json_ld()` — generic schema.org lookup (`Article` /
     `CreativeWork` / `WebPage`). Reused near-verbatim from lidl_parser.py
     (this part of that file carries no lidl-specific knowledge at all —
     it is a generic `<script type="application/ld+json">` reader).
     UNCONFIRMED whether perplexity.ai emits any JSON-LD on a Page at all.
  2. `extract_og_meta()` — Open Graph / Twitter Card `<meta>` tags
     (`og:title`, `og:description`, `og:image`, `article:author` if
     present, `<link rel="canonical">`). This is the one path with a
     genuine reason to expect it works even against a client-rendered
     SPA shell: OG tags exist specifically so link-preview bots (Slack,
     Twitter, iMessage) get a title/image without running JS, and a
     product built around one-click sharing (confirmed real Pages
     feature) has a concrete incentive to server-render them. Still
     UNCONFIRMED for THIS site specifically — no real capture exists.
  3. DOM fallback (`_parse_page_from_dom`) — best-effort selectors for
     the rendered article body: heading, byline, sources list, the two
     engagement counters. Marked `# TODO: verify live` throughout.

All paths feed the same `Product` shape from `output_writer.py`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, List, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from output_writer import Product

log = logging.getLogger("page_parser")

BASE_URL = "https://www.perplexity.ai"
SOURCE = "perplexity.ai"

MIN_CARD_MATCHES = 1  # per family invariant (CLAUDE.md §5) — but unlike a search-results
                       # grid, a Page URL is either the one article or nothing; there is no
                       # "did enough cards render" ambiguity to guard against here, so this
                       # constant exists for signature parity with the engines' readiness-wait
                       # helper, not because more than one match is ever expected on this site.

# REAL, live-captured incident (2026-09-21, via a browser-rendering tool —
# see module docstring and TESTING.md): a fresh visit to a real
# `/page/{slug}-{id}` URL got served Cloudflare's own "managed challenge"
# interstitial (`cType: 'managed'`, Ray ID `a3e80852cfb8ae37`) instead of
# any Page content — confirmed on two separate attempts against two
# different Page URLs, not a one-off. `captcha_solver.
# GENERIC_BOT_CHALLENGE_MARKERS` already catches this via its generic
# `"cf-turnstile"` / `"challenges.cloudflare.com"` /
# `"cdn-cgi/challenge-platform"` strings (confirmed: `detect_from_html()`
# on the actual captured HTML returns `True`) — the markers below are
# added anyway, as durable, site-specific corroboration of the SAME
# incident, not a replacement for the generic check, mirroring
# skyscanner-scraper's PerimeterX precedent:
#   - `cf-chl-widget` — the id prefix Cloudflare's own challenge form uses
#     for its hidden Turnstile response field on THIS site's challenge
#     page (`id="cf-chl-widget-qblbv_response"`).
#   - `_cf_chl_opt` — the inline JS object Cloudflare's challenge-platform
#     script sets on the page (`window._cf_chl_opt = {cType: 'managed', ...}`).
# What this incident does NOT confirm: any selector elsewhere in this file
# (`# TODO: verify live`) — this was a block page, not a results page.
# `captcha_solver.py`'s own `_UNSUPPORTED_VENDOR_MARKERS` already lists a
# bare Cloudflare managed challenge as a vendor with no automated solve
# path (pending confirmation, per that file's comment) — this incident is
# real-world corroboration of exactly that case, not yet acted on here
# since `captcha_solver.py` is a family-shared module (CLAUDE.md §7) and a
# cross-cutting change to it deserves a conscious, family-wide decision
# rather than a one-repo edit.
BOT_CHALLENGE_MARKERS: tuple = (
    "cf-chl-widget",
    "_cf_chl_opt",
)

# Confirmed real from robots.txt: these path prefixes are explicitly
# disallowed for every crawler. Not used to block a request this tool
# makes (a human-directed --url is not a crawl), but `is_disallowed_path()`
# below lets the CLI warn a caller who points --url at one of these rather
# than silently trying and failing.
_DISALLOWED_PATH_PREFIXES = (
    "/search",           # covers /search, /search/new, /search?*/
    "/marketing/prerelease/",
    "/onboarding/",
    "/join/",
)

_PAGE_ID_RE = re.compile(r"^(?P<slug>.*)-(?P<id>[A-Za-z0-9_-]{18,24})$")


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #
def is_page_url(url: str) -> bool:
    return urlparse(url).path.startswith("/page/")


def is_disallowed_path(url: str) -> bool:
    """True if `url`'s path matches a robots.txt-disallowed prefix — used
    by the CLI to refuse (EXIT_BAD_USAGE) a --url/--urls-file entry that
    isn't actually a Page, rather than silently sending a request this
    tool has no business making."""
    path = urlparse(url).path
    return any(path.startswith(prefix) for prefix in _DISALLOWED_PATH_PREFIXES)


def parse_page_ref(url: str) -> tuple:
    """Splits a Page URL's `/page/{slug}-{id}` path segment into
    `(slug, page_id)`. The id is confirmed real-shaped (22 characters,
    mixed-case alphanumeric plus `_`/`-`, from the one real example URL
    found via WebSearch during research — see README) but the exact
    length is UNCONFIRMED as a hard invariant, so the regex accepts a
    range (18-24) rather than an exact count. Returns `(None, None)` if
    the path doesn't look like a Page URL at all."""
    path = urlparse(url).path
    if not path.startswith("/page/"):
        return None, None
    segment = path[len("/page/"):].strip("/")
    m = _PAGE_ID_RE.match(segment)
    if not m:
        return segment or None, None
    return m.group("slug"), m.group("id")


# --------------------------------------------------------------------------- #
# sku — a Page's own id is the stable part of its URL; an author can edit
# the slug (title-derived, so a title edit could change it) without
# changing the id, exactly the reasoning lidl-scraper's make_sku() uses for
# preferring a site-native id over a URL fingerprint. Falls back to a
# deterministic fingerprint of the full URL (NEVER random/run-scoped) only
# when no id-shaped segment could be split out, so diff_runs.py still sees
# a stable sku across two runs whenever possible.
# --------------------------------------------------------------------------- #
def make_sku(*, page_id: Optional[str], url: Optional[str]) -> str:
    if page_id:
        return f"perplexity-{page_id}"
    basis = url or ""
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]
    return f"perplexity-url-{digest}"


# --------------------------------------------------------------------------- #
# Path 1: generic schema.org JSON-LD lookup — reused near-verbatim from
# lidl_parser.py's extract_json_ld(); this function itself carries no
# site-specific knowledge, only the node-shape functions below it do.
# --------------------------------------------------------------------------- #
def extract_json_ld(html: str) -> List[dict]:
    """Returns every parseable `application/ld+json` blob on the page, with
    `@graph` arrays flattened into the top-level list."""
    soup = BeautifulSoup(html, "html.parser")
    blobs: List[dict] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        if not tag.string:
            continue
        try:
            data = json.loads(tag.string)
        except (ValueError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("@graph"), list):
                blobs.extend(g for g in item["@graph"] if isinstance(g, dict))
            else:
                blobs.append(item)
    return blobs


def _schema_type(node: dict) -> str:
    t = node.get("@type")
    if isinstance(t, list):
        return "|".join(str(x) for x in t)
    return str(t or "")


def _find_article_node(blobs: List[dict]) -> Optional[dict]:
    """# TODO: verify live — UNCONFIRMED whether perplexity.ai emits any
    JSON-LD on a Page at all, let alone which schema.org type. Tries the
    obvious content-page candidates in order of specificity."""
    for wanted in ("Article", "CreativeWork", "WebPage"):
        for node in blobs:
            if wanted in _schema_type(node):
                return node
    return None


def _json_ld_node_to_product(node: dict, *, source_url: str) -> Optional[Product]:
    title = node.get("headline") or node.get("name")
    if not title:
        return None  # nothing usable — skip rather than fabricate a row

    author = node.get("author")
    if isinstance(author, dict):
        author = author.get("name")
    elif isinstance(author, list):
        author = next((a.get("name") for a in author if isinstance(a, dict) and a.get("name")), None)

    image = node.get("image")
    if isinstance(image, list):
        image = next((item for item in image if isinstance(item, (str, dict))), None)
    if isinstance(image, dict):
        image = image.get("url") or image.get("contentUrl")

    url = node.get("url") or source_url
    slug, page_id = parse_page_ref(url)

    return Product(
        sku=make_sku(page_id=page_id, url=url),
        source=SOURCE,
        category=None,       # not applicable — see output_writer.Product docstring
        title=title,
        brand=None,           # not applicable — see output_writer.Product docstring
        price=None,           # not applicable — see output_writer.Product docstring
        currency=None,        # not applicable — see output_writer.Product docstring
        price_source=None,    # not applicable — see output_writer.Product docstring
        product_url=url,
        image_url=image,
        scraped_at=_now_iso(),
        author=author,
        view_count=None,                    # TODO: verify live — not present in schema.org Article
        follow_up_question_count=None,      # TODO: verify live — Perplexity-specific, not standard schema.org
        source_count=None,                  # TODO: verify live
        sources_json=None,                  # TODO: verify live
        section_count=None,                 # TODO: verify live
        word_count=_word_count(node.get("articleBody")) if isinstance(node.get("articleBody"), str) else None,
        slug=slug,
        published_at=node.get("datePublished") or node.get("dateModified"),
    )


# --------------------------------------------------------------------------- #
# Path 2: Open Graph / Twitter Card meta tags — see module docstring for
# why this has a genuine (if unconfirmed) reason to work even against a
# client-rendered shell.
# --------------------------------------------------------------------------- #
def extract_og_meta(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    out: dict = {}
    for prop, key in (
        ("og:title", "title"),
        ("og:description", "description"),
        ("og:image", "image"),
        ("og:url", "url"),
        ("article:author", "author"),
        ("article:published_time", "published_at"),
    ):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            out[key] = tag["content"].strip()
    if "title" not in out:
        tag = soup.find("meta", attrs={"name": "twitter:title"})
        if tag and tag.get("content"):
            out["title"] = tag["content"].strip()
    canonical = soup.find("link", rel="canonical")
    if canonical and canonical.get("href"):
        out.setdefault("url", canonical["href"].strip())
    return out


def _og_meta_to_product(meta: dict, *, source_url: str) -> Optional[Product]:
    title = meta.get("title")
    if not title:
        return None  # no usable title anywhere — skip rather than fabricate a row

    url = meta.get("url") or source_url
    slug, page_id = parse_page_ref(url)

    return Product(
        sku=make_sku(page_id=page_id, url=url),
        source=SOURCE,
        category=None,
        title=title,
        brand=None,
        price=None,
        currency=None,
        price_source=None,
        product_url=url,
        image_url=meta.get("image"),
        scraped_at=_now_iso(),
        author=meta.get("author"),          # TODO: verify live — article:author is UNCONFIRMED to be set on a real Page
        view_count=None,                    # TODO: verify live — meta tags don't carry engagement counters
        follow_up_question_count=None,      # TODO: verify live
        source_count=None,                  # TODO: verify live
        sources_json=None,                  # TODO: verify live
        section_count=None,                 # TODO: verify live
        word_count=None,
        slug=slug,
        published_at=meta.get("published_at"),
    )


# --------------------------------------------------------------------------- #
# Path 3: DOM fallback — rendered article body. Every selector here is a
# best-effort guess (# TODO: verify live) since no real capture of a
# rendered Page exists yet. Several candidate selectors are tried per
# field, same defensive pattern as lidl_parser.py's own DOM fallback,
# precisely because the real markup is unknown.
# --------------------------------------------------------------------------- #
_TITLE_SELECTORS = ("h1", '[data-testid="page-title"]', "article h1", ".page-title")
_AUTHOR_SELECTORS = (
    '[data-testid="page-author"]', '[class*="author" i]', '[class*="byline" i]', ".creator-name",
)
_SOURCES_CONTAINER_SELECTORS = (
    '[data-testid="sources-list"]', '[class*="sources" i]', '[class*="citations" i]', "#sources",
)
_VIEW_COUNT_SELECTORS = ('[data-testid="view-count"]', '[aria-label*="view" i]', '[class*="view-count" i]')
_FOLLOWUP_COUNT_SELECTORS = (
    '[data-testid="followup-count"]', '[aria-label*="question" i]', '[class*="followup" i]',
)
_SECTION_HEADING_SELECTORS = ("article h2", "article h3", ".page-content h2", ".page-content h3")
_BODY_SELECTORS = ("article", '[data-testid="page-content"]', ".page-content", "main")
_IMAGE_SELECTORS = ("article img", '[data-testid="page-content"] img', "main img")

_COUNT_RE = re.compile(r"([\d,.]+)\s*([kKmM])?")


def _parse_count(text: Optional[str]) -> Optional[int]:
    """# TODO: verify live — UNCONFIRMED whether the real UI shows a raw
    number or an abbreviated one ("1.2k views"); handles both so this
    doesn't silently return None the moment a real capture shows the
    abbreviated form."""
    if not text:
        return None
    m = _COUNT_RE.search(text.replace(" ", ""))
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    suffix = (m.group(2) or "").lower()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return int(value)


def count_result_cards(html: str) -> int:
    """Used by captcha_solver.solve_when_blocked — cheap presence check, no
    readiness wait. Unlike a search-results grid, a Page URL has at most
    ONE "card" (the article itself), so this returns 1 if a title-shaped
    heading rendered, 0 otherwise — enough for solve_when_blocked's own
    `count_product_links(html) > 0` skip-if-already-rendered check."""
    soup = BeautifulSoup(html, "html.parser")
    return 1 if _first_text(soup, _TITLE_SELECTORS) else 0


def _first_text(soup_or_tag, selectors) -> Optional[str]:
    for sel in selectors:
        found = soup_or_tag.select_one(sel)
        if found:
            text = found.get_text(strip=True)
            if text:
                return text
    return None


def _word_count(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    words = text.split()
    return len(words) or None


def _extract_sources(soup: BeautifulSoup) -> List[dict]:
    """# TODO: verify live — best-effort: looks for a container matching
    one of `_SOURCES_CONTAINER_SELECTORS`, then every link inside it.
    Returns a list of `{"url": ..., "title": ...}` dicts, JSON-encoded by
    the caller into `Product.sources_json` (see output_writer.py's
    docstring for why that field is a string, not a native list)."""
    for sel in _SOURCES_CONTAINER_SELECTORS:
        container = soup.select_one(sel)
        if container:
            sources = []
            for a in container.select("a[href]"):
                href = a.get("href")
                if not href:
                    continue
                sources.append({"url": href, "title": a.get_text(strip=True) or None})
            if sources:
                return sources
    return []


def _parse_page_from_dom(html: str, *, source_url: str) -> Optional[Product]:
    soup = BeautifulSoup(html, "html.parser")

    title = _first_text(soup, _TITLE_SELECTORS)
    if not title:
        return None  # nothing usable rendered — skip rather than fabricate a row

    author = _first_text(soup, _AUTHOR_SELECTORS)

    view_count = _parse_count(_first_text(soup, _VIEW_COUNT_SELECTORS))
    follow_up_question_count = _parse_count(_first_text(soup, _FOLLOWUP_COUNT_SELECTORS))

    sources = _extract_sources(soup)

    section_count = 0
    for sel in _SECTION_HEADING_SELECTORS:
        matches = soup.select(sel)
        if matches:
            section_count = len(matches)
            break

    body_text = None
    for sel in _BODY_SELECTORS:
        body = soup.select_one(sel)
        if body:
            body_text = body.get_text(" ", strip=True)
            break

    image = None
    for sel in _IMAGE_SELECTORS:
        img = soup.select_one(sel)
        if img and (img.get("src") or img.get("data-src")):
            image = img.get("src") or img.get("data-src")
            break

    slug, page_id = parse_page_ref(source_url)

    return Product(
        sku=make_sku(page_id=page_id, url=source_url),
        source=SOURCE,
        category=None,
        title=title,
        brand=None,
        price=None,
        currency=None,
        price_source=None,
        product_url=source_url,
        image_url=image,
        scraped_at=_now_iso(),
        author=author,
        view_count=view_count,
        follow_up_question_count=follow_up_question_count,
        source_count=len(sources) or None,
        sources_json=json.dumps(sources, ensure_ascii=False) if sources else None,
        section_count=section_count or None,
        word_count=_word_count(body_text),
        slug=slug,
        published_at=None,  # TODO: verify live — no confirmed DOM selector for a publish date
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
@dataclass
class PageResult:
    products: List[Product]
    source_used: str  # "json_ld" | "og_meta" | "dom" | "none"


def parse_page(html: str, *, url: str) -> PageResult:
    """One Page URL yields zero or one Product — `products` is still a
    list (never a bare `Optional[Product]`) purely so the engines' shared
    `merge_pages()`/round-loop code (written once, for all four family
    repos) doesn't need a special case for this repo's 1-or-0 cardinality
    versus the sibling repos' many-per-round cardinality."""
    json_ld_blobs = extract_json_ld(html)
    article_node = _find_article_node(json_ld_blobs)
    if article_node:
        product = _json_ld_node_to_product(article_node, source_url=url)
        if product is not None:
            return PageResult(products=[product], source_used="json_ld")

    og_meta = extract_og_meta(html)
    if og_meta:
        product = _og_meta_to_product(og_meta, source_url=url)
        if product is not None:
            return PageResult(products=[product], source_used="og_meta")

    dom_product = _parse_page_from_dom(html, source_url=url)
    if dom_product is not None:
        return PageResult(products=[dom_product], source_used="dom")

    return PageResult(products=[], source_used="none")


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_parse_page(html: str, **kwargs) -> PageResult:
    """Public engine entry point. Wraps `parse_page` so an unexpected
    exception INSIDE parsing degrades that ONE url to "found nothing here"
    instead of propagating out of an engine's --urls-file loop and
    crashing the whole run — which would discard every Page already
    collected from earlier URLs in the same batch. Same family-wide
    invariant (CLAUDE.md §6/§10) as lidl_parser.safe_parse_search_results,
    skyscanner-scraper's flight_parser.safe_parse_search_results, and
    stockx-scraper's per-engine safe_parse closures: one bad page is a
    reason to log loudly and move on, not to lose everything gathered so
    far. All three engines call this instead of `parse_page` directly.
    """
    try:
        return parse_page(html, **kwargs)
    except Exception as exc:  # noqa: BLE001 — see docstring above
        log.error("A page's HTML failed to parse — treating it as empty, not crashing: %s", exc)
        return PageResult(products=[], source_used="none")
