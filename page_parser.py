#!/usr/bin/env python3
"""page_parser.py — this IS the perplexity.ai site knowledge.

**Rewritten 2026-09-30 from the first live capture of real articles**
(through a US Scraping Browser API profile over CDP: HTTP 200, no
Cloudflare challenge on any of the requests below). Everything this file
extracts from is CONFIRMED against that capture; the fixtures in
`tests/fixtures/*_live_20260930.json` are the unedited API responses.

What the site actually is today:

  - Two URL kinds serve the same article object:
      * classic Pages, `/page/{slug}-{id}`. Creating new Pages is currently
        retired by Perplexity, but existing links still resolve: the
        client rewrites `/page/How-to-Generate-VzUTuvQVSIqru3QGvPihlg` to
        `/page/{backend_uuid}`;
      * Discover articles, `/discover/{topic}/{slug}-{id}` — where
        Perplexity publishes new articles now (`/discover` links only to
        these).
    `{id}` is a 22-character token that may contain `.`, `_` and `-`
    (`.FiJwwm9STi9_gZ5rgyhXQ`, `P.Mg27lmRU21zVNesSK35g` — seen live).
  - **The page's own data source is `GET /rest/article/{ref}`**, where
    `{ref}` is the slug-with-id OR the backend uuid. It returns
    `{"status": "success", "entries": [...]}`, one entry per article
    section, the first carrying the article-level fields
    (`thread_url_slug`, `author_username`, `social_info`,
    `featured_images`, `article_info.{title, summary, read_time,
    first_published}`). Each entry's `text` is itself a JSON string with
    `answer` (the section's markdown) and `web_results` (its sources).
    An unknown ref answers HTTP 400.
  - The server-rendered HTML carries NO article data worth trusting: no
    JSON-LD, and the Open Graph tags are the site-wide defaults on every
    page (`og:title` "Perplexity", `og:url` the origin). A parser that
    believed them would emit the same "Perplexity" row for every URL, so
    nothing is read from them.
  - `GET /rest/discover/feed?limit=N&offset=K&topic=top` is the Discover
    listing (20 per call, paged by `offset`). Other topic slugs from
    `/rest/discover/topics` (tech, finance, arts, sports, entertainment)
    returned zero items for an anonymous visitor on 2026-09-30.

Extraction: `parse_article_json()` on the `/rest/article/` payload, and
nothing else. The rendered HTML is NOT a fallback, for a measured reason:
a dead URL (the API answered 400) rendered the previously viewed
article's title and sections in full, and no article id appears anywhere
in the HTML to tell the two apart. A row built from it could carry
another article's title under this URL's sku. The HTML is only used to
tell "the app rendered" from "a challenge page" (`is_app_page`,
`count_result_cards`).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, List, Optional
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup

from output_writer import Product

log = logging.getLogger("page_parser")

BASE_URL = "https://www.perplexity.ai"
SOURCE = "perplexity.ai"
DISCOVER_URL = f"{BASE_URL}/discover"

# The app's own query suffix on every /rest/ call, copied from the live
# requests. The endpoints answered identically without it in testing, but
# matching the real client is the cheaper assumption.
_REST_SUFFIX = "version=2.18&source=default"

MIN_CARD_MATCHES = 1  # one URL is one article (CLAUDE.md §5 names the constant; there is no grid)

# Cloudflare's managed challenge, captured live 2026-09-21
# (tests/fixtures/perplexity_cloudflare_block_real.html). The generic
# captcha_solver markers catch it too; these two are the site-specific
# corroboration.
BOT_CHALLENGE_MARKERS: tuple = (
    "cf-chl-widget",
    "_cf_chl_opt",
)

# robots.txt disallows these for every crawler; the CLI refuses them.
_DISALLOWED_PATH_PREFIXES = (
    "/search",
    "/marketing/prerelease/",
    "/onboarding/",
    "/join/",
)

ID_LENGTH = 22
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_ID_CHARS_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# A page the app actually served is built from its own asset host; a
# Cloudflare interstitial is not (live 2026-09-30: 195 references on each
# real article, 0 on both captured challenge pages). The DOM fallback
# refuses to read any page without it — a local headless run otherwise
# turned the challenge's own "Performing security verification" heading
# into an article title and reported success.
APP_ASSET_MARKER = "pplx-next-static-public"
_MIN_APP_ASSET_HITS = 3


def is_app_page(html: str) -> bool:
    return (html or "").count(APP_ASSET_MARKER) >= _MIN_APP_ASSET_HITS


# Headings the site renders around every article, never article content.
_CHROME_HEADINGS = {"cookie policy"}


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #
def is_page_url(url: str) -> bool:
    """A URL this scraper can turn into an article: `/page/...` or
    `/discover/{topic}/...` on perplexity.ai."""
    return article_ref(url)[1] is not None


def is_disallowed_path(url: str) -> bool:
    path = urlparse(url).path
    return any(path.startswith(prefix) for prefix in _DISALLOWED_PATH_PREFIXES)


def split_slug_id(segment: str) -> tuple:
    """`some-title-heYaECNnQuaM0AZ0QSWjaw` → (`some-title`, `heYaECNnQuaM0AZ0QSWjaw`).
    The id is the last 22 characters after a `-`; it can itself contain
    `-`, `.` and `_`, so it is cut by length, never by splitting on `-`."""
    segment = (segment or "").strip("/")
    if len(segment) > ID_LENGTH and segment[-ID_LENGTH - 1] == "-":
        ident = segment[-ID_LENGTH:]
        if _ID_CHARS_RE.match(ident):
            return segment[: -ID_LENGTH - 1], ident
    return segment or None, None


def article_ref(url: str) -> tuple:
    """(topic, ref) for an article URL, or (None, None). `ref` is what
    `/rest/article/{ref}` accepts: the full slug-with-id, or a backend
    uuid (what an old `/page/` link is rewritten to)."""
    parts = urlparse(url)
    if parts.netloc and not parts.netloc.endswith("perplexity.ai"):
        return None, None
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) == 2 and segs[0] == "page":
        return None, segs[1]
    if len(segs) == 3 and segs[0] == "discover":
        return segs[1], segs[2]
    return None, None


def parse_page_ref(url: str) -> tuple:
    """(slug, id) from an article URL; a uuid ref has no id."""
    _topic, ref = article_ref(url)
    if not ref or _UUID_RE.match(ref):
        return None, None
    return split_slug_id(ref)


def article_api_url(ref: str) -> str:
    return f"{BASE_URL}/rest/article/{quote(ref, safe='._-')}?{_REST_SUFFIX}"


def discover_feed_api_url(topic: str, *, offset: int, limit: int = 20) -> str:
    return f"{BASE_URL}/rest/discover/feed?limit={limit}&offset={offset}&topic={quote(topic)}&{_REST_SUFFIX}"


def discover_article_url(topic: str, slug: str) -> str:
    return f"{BASE_URL}/discover/{topic}/{slug}"


# --------------------------------------------------------------------------- #
# sku — the 22-character id, which survives a slug change (see Product).
# A deterministic URL fingerprint is the last resort, never a random one.
# --------------------------------------------------------------------------- #
def make_sku(*, page_id: Optional[str], url: Optional[str]) -> str:
    if page_id:
        return f"perplexity-{page_id}"
    digest = hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:16]
    return f"perplexity-url-{digest}"


# --------------------------------------------------------------------------- #
# The /rest/article/ payload
# --------------------------------------------------------------------------- #
def _entry_text(entry: dict) -> dict:
    raw = entry.get("text")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _collect_sources(entries: List[dict]) -> List[dict]:
    seen, sources = set(), []

    def add(result: Any) -> None:
        if not isinstance(result, dict):
            return
        url = result.get("url")
        if not isinstance(url, str) or not url.startswith("http") or url in seen:
            return
        seen.add(url)
        sources.append({"url": url, "title": result.get("name") or None})

    for entry in entries:
        for result in _entry_text(entry).get("web_results") or []:
            add(result)
        for result in (entry.get("article_info") or {}).get("cited_search_results") or []:
            add(result)
    return sources


def _word_count(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    return len(text.split()) or None


def parse_article_json(data: Any, *, url: str) -> Optional[Product]:
    """One Product from a `/rest/article/` payload, or None when the
    payload is not a successful article (a 400's `{"detail": ...}`, an
    empty `entries`, a missing title)."""
    if not isinstance(data, dict) or data.get("status") != "success":
        return None
    entries = [e for e in data.get("entries") or [] if isinstance(e, dict)]
    if not entries:
        return None
    head = entries[0]
    info = head.get("article_info") or {}
    title = info.get("title") or head.get("thread_title")
    if not title:
        return None

    canonical_slug = head.get("thread_url_slug")
    slug, page_id = split_slug_id(canonical_slug or "")
    if page_id is None:
        slug, page_id = parse_page_ref(url)
    topic, _ref = article_ref(url)
    if canonical_slug:
        product_url = discover_article_url(topic, canonical_slug) if topic else f"{BASE_URL}/page/{canonical_slug}"
    else:
        product_url = url

    image = None
    for img in head.get("featured_images") or []:
        if isinstance(img, dict) and isinstance(img.get("image"), str):
            image = img["image"]
            break

    social = head.get("social_info") or {}
    sources = _collect_sources(entries)
    words = sum(_word_count(_entry_text(e).get("answer")) or 0 for e in entries)

    return Product(
        sku=make_sku(page_id=page_id, url=product_url),
        source=SOURCE,
        category=topic,
        title=title,
        brand=None,
        price=None,
        currency=None,
        price_source=None,
        product_url=product_url,
        image_url=image,
        scraped_at=_now_iso(),
        author=head.get("author_username"),
        view_count=_as_int(social.get("view_count")),
        like_count=_as_int(social.get("like_count")),
        fork_count=_as_int(social.get("fork_count")),
        source_count=len(sources),
        sources_json=json.dumps(sources, ensure_ascii=False),
        section_count=len(entries),
        word_count=words or None,
        slug=canonical_slug or (f"{slug}-{page_id}" if slug and page_id else slug),
        summary=info.get("summary") or None,
        read_time_minutes=_as_int(info.get("read_time")),
        published_at=info.get("first_published") or None,
        updated_at=head.get("updated_datetime") or None,
    )


def parse_discover_feed(data: Any, *, topic: str) -> tuple:
    """(article URLs, has_more) from one `/rest/discover/feed` page."""
    if not isinstance(data, dict):
        return [], False
    urls = []
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if isinstance(slug, str) and slug:
            urls.append(discover_article_url(topic, slug))
    return urls, bool(data.get("next_token"))


# --------------------------------------------------------------------------- #
# Rendered HTML: only "did the app render" — never a source of row data
# --------------------------------------------------------------------------- #
def _headings(soup: BeautifulSoup) -> List[str]:
    heads = []
    for h in soup.select("h2"):
        text = h.get_text(" ", strip=True)
        if text and text.lower() not in _CHROME_HEADINGS:
            heads.append(text)
    return heads


def count_result_cards(html: str) -> int:
    """1 if an article heading rendered, else 0 — what
    captcha_solver.solve_when_blocked uses to skip a solve on a page whose
    content is already there."""
    if not is_app_page(html):
        return 0
    return 1 if _headings(BeautifulSoup(html, "html.parser")) else 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
@dataclass
class PageResult:
    products: List[Product]
    source_used: str  # "api" | "none"


def parse_page(html: str, *, url: str, article_json: Any = None) -> PageResult:
    """Zero or one Product for one URL, from the engine's decoded
    `/rest/article/` payload. `html` is accepted for signature parity and
    deliberately not parsed — see the module docstring."""
    if article_json is not None:
        product = parse_article_json(article_json, url=url)
        if product is not None:
            return PageResult(products=[product], source_used="api")
    return PageResult(products=[], source_used="none")


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_parse_page(html: str, **kwargs) -> PageResult:
    """Engine entry point: a parse exception degrades this ONE url to
    "nothing found" instead of crashing the batch (CLAUDE.md §6/§10)."""
    try:
        return parse_page(html, **kwargs)
    except Exception as exc:  # noqa: BLE001
        log.error("A page failed to parse — treating it as empty, not crashing: %s", exc)
        return PageResult(products=[], source_used="none")
