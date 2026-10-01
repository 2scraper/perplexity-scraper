#!/usr/bin/env python3
"""output_writer.py — the row model, JSON/CSV writer, dedupe, exit codes,
and run metadata. Carries (almost) no site knowledge: everything here is
part of the family's shared output CONTRACT — diverging from it needs a
written reason, not a per-site tweak. Ported near-verbatim from
lidl-scraper's output_writer.py (CLAUDE.md §7: porting starts by copying
the family-shared modules from the newest sibling and diffing what
changed on purpose) — in turn ported from skyscanner-scraper's, in turn
from stockx-scraper's original (commit 00b5570). The exit codes,
STATUS_BY_EXIT map, and finish_run() outcome-precedence logic are
IDENTICAL to every prior family member on purpose, including the fix
from a 2026-09-15 audit that found `blocked`/`remote_api_error` were
being silently ignored whenever products were present or --allow-empty
was passed. Only the `Product` dataclass's site-specific tail (below the
family-common fields) differs per repo.
"""
from __future__ import annotations

import csv
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import List, Optional, Sequence

# --------------------------------------------------------------------------- #
# Exit codes — identical across playwright_scraper / selenium_scraper /
# puppeteer_scraper, and identical to every other 2scraper family repo. A
# caller (CI, a cron job, another program) must be able to tell these apart
# without parsing stdout.
# --------------------------------------------------------------------------- #
EXIT_OK = 0
EXIT_CRASH = 1
EXIT_BAD_USAGE = 2
EXIT_BLOCKED = 3
EXIT_ZERO_PRODUCTS = 4
EXIT_REMOTE_API_ERROR = 5
EXIT_PARTIAL = 6

STATUS_BY_EXIT = {
    EXIT_OK: "complete",
    EXIT_CRASH: "crashed",
    EXIT_BAD_USAGE: "bad_usage",
    EXIT_BLOCKED: "blocked",
    EXIT_ZERO_PRODUCTS: "empty",
    EXIT_REMOTE_API_ERROR: "remote_api_error",
    EXIT_PARTIAL: "partial",
}


# --------------------------------------------------------------------------- #
# Row model
# --------------------------------------------------------------------------- #
@dataclass
class Product:
    """Family-common fields first (identical name AND order to every other
    2scraper repo's row, in both JSON and CSV) so `diff_runs.py` and any
    other cross-repo tool keep working unmodified; perplexity.ai-specific
    (wiki article) fields at the end.

    This repo's target — Perplexity Pages (see README for why Pages and not
    live Q&A answers) — is a wiki-style article, not a commerce listing, so
    several family-common fields carry no real meaning here. Rather than
    invent a parallel schema and break every tool that assumes `Product`'s
    field order (CLAUDE.md §9), they are kept and explicitly documented as
    not applicable, exactly the way skyscanner-scraper repurposed `brand`
    for "operating airline" instead of leaving the family contract behind:

    - `sku`: `perplexity-{id}`, where `{id}` is the 22-character tail of
      the article's own `thread_url_slug` (confirmed live 2026-09-30: an
      old `/page/How-to-Generate-VzUTuvQVSIqru3QGvPihlg` link and its
      current canonical slug `perplexity-ai-pages-guide-gene-VzUTuvQVSIqru3QGvPihlg`
      share the id while the slug changed). Ids can contain `.` and `_`.
    - `category`: the Discover topic from the URL (`/discover/top/...` →
      `top`). A classic `/page/...` Page has none → `None`.
    - `brand` / `price` / `currency` / `price_source`: **not applicable**,
      always `None`. Kept so `diff_runs.py` and the family's row prefix
      stay unchanged (CLAUDE.md §9).
    - `image_url`: the first `featured_images[].image`, when there is one.

    Site-specific tail, every field read from the article's own
    `/rest/article/{slug}` JSON (see `page_parser.py`):

    - `author`: `author_username`.
    - `view_count` / `like_count` / `fork_count`: `social_info`.
    - `source_count` / `sources_json`: every distinct cited URL across all
      sections (`web_results` plus `article_info.cited_search_results`),
      as a JSON string of `{"url", "title"}` so a CSV row stays flat.
    - `section_count` / `word_count`: the number of sections (`entries`)
      and the words across their answers.
    - `slug`: the canonical `thread_url_slug`.
    - `summary`: `article_info.summary` (Discover articles; classic Pages
      have none).
    - `read_time_minutes`: `article_info.read_time`.
    - `published_at` / `updated_at`: `article_info.first_published` /
      `updated_datetime`.

    Removed 2026-09-30 after the first live capture:
    `follow_up_question_count`. Nothing in the article JSON, the HTML or
    the Discover feed carries it (measured on a 2024 Page and a 2026
    Discover article), and a column that is null on every row should not
    exist (CLAUDE.md §9).
    """

    # --- family-common ---
    sku: Optional[str]
    source: str
    category: Optional[str]
    title: Optional[str]
    brand: Optional[str]
    price: Optional[float]
    currency: Optional[str]
    price_source: Optional[str]
    product_url: Optional[str]
    image_url: Optional[str]
    scraped_at: str

    # --- perplexity.ai-specific (wiki article) ---
    author: Optional[str] = None
    view_count: Optional[int] = None
    like_count: Optional[int] = None
    fork_count: Optional[int] = None
    source_count: Optional[int] = None
    sources_json: Optional[str] = None
    section_count: Optional[int] = None
    word_count: Optional[int] = None
    slug: Optional[str] = None
    summary: Optional[str] = None
    read_time_minutes: Optional[int] = None
    published_at: Optional[str] = None
    updated_at: Optional[str] = None


PRODUCT_FIELD_NAMES: List[str] = [f.name for f in fields(Product)]


# --------------------------------------------------------------------------- #
# Dedupe / merge — "merge in page order, not arrival order": the caller
# passes pages already sorted by page number (or, for a concurrent run,
# sorted before calling this), never in whichever-finished-first order.
# --------------------------------------------------------------------------- #
def sku_key(p: Product) -> str:
    """The identity `merge_pages` dedupes on, and the same one an engine's
    scroll/pagination loop should track to decide "did this page add
    anything NEW" — never just "is this page non-empty" (a page repeating
    an already-seen item, e.g. past the real last batch of results, isn't
    empty but isn't new either)."""
    return p.sku or f"__no_sku__:{p.product_url}"


def merge_pages(pages: Sequence[Sequence[Product]]) -> List[Product]:
    seen: dict = {}
    order: List[str] = []
    for page in pages:
        for p in page:
            key = sku_key(p)
            if key not in seen:
                order.append(key)
            seen[key] = p  # last write for a given sku wins, in page order
    return [seen[k] for k in order]


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #
def write_json(products: Sequence[Product], out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump([asdict(p) for p in products], f, ensure_ascii=False, indent=2)


def write_csv(products: Sequence[Product], out_path: str) -> None:
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=PRODUCT_FIELD_NAMES)
        writer.writeheader()  # written even for zero rows — a consumer reads
        for p in products:    # an empty table, not a zero-byte file.
            writer.writerow(asdict(p))


def write_output(products: Sequence[Product], out_path: str, fmt: str) -> None:
    if fmt == "json":
        write_json(products, out_path)
    elif fmt == "csv":
        write_csv(products, out_path)
    else:
        raise ValueError(f"Unsupported format: {fmt!r} (expected 'json' or 'csv')")


# --------------------------------------------------------------------------- #
# Run metadata sidecar
# --------------------------------------------------------------------------- #
def meta_path_for(out_path: str) -> str:
    return f"{out_path}.meta.json"


def write_meta(
    out_path: str,
    *,
    status: str,
    stop_reason: str,
    engine: str,
    url: str,
    pages_requested: int,
    pages_completed: int,
    failed_pages: Optional[List[int]] = None,
    product_count: int,
    price_confirmed_pct: Optional[float] = None,
    started_at: float,
    finished_at: Optional[float] = None,
    extra: Optional[dict] = None,
) -> None:
    meta = {
        "status": status,
        "stop_reason": stop_reason,
        "engine": engine,
        "url": url,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "failed_pages": failed_pages or [],
        "product_count": product_count,
        "price_confirmed_pct": price_confirmed_pct,
        "started_at": started_at,
        "finished_at": finished_at or time.time(),
    }
    if extra:
        meta.update(extra)
    Path(meta_path_for(out_path)).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# finish_run — the single place every engine calls to decide exit code,
# whether to write output at all, and whether to write a sidecar. Keeping
# this in one shared function is what stops the three engines' exit-code
# mapping from drifting apart. Structurally identical to stockx-scraper's
# post-audit-fix finish_run() (commit 00b5570) — see that file's comment
# for the full incident writeup this precedence order fixes.
# --------------------------------------------------------------------------- #
def finish_run(
    *,
    products: List[Product],
    out_path: str,
    fmt: str,
    engine: str,
    url: str,
    pages_requested: int,
    pages_completed: int,
    failed_pages: Optional[List[int]],
    blocked: bool,
    remote_api_error: bool,
    allow_empty: bool,
    started_at: float,
    price_confirmed_pct: Optional[float] = None,
    extra_meta: Optional[dict] = None,
    rejected_rows: int = 0,
    max_results: Optional[int] = None,
    rate_limited: bool = False,
    total_results: Optional[int] = None,
    incomplete_reason: Optional[str] = None,
    capped: Optional[bool] = None,
) -> int:
    """Decide status/exit code, write output + sidecar (or neither), return
    the process exit code. NEVER writes a sidecar for a failed run, and
    NEVER overwrites a previous good output with an empty one unless the
    caller explicitly passed --allow-empty."""
    failed_pages = failed_pages or []
    partial = bool(failed_pages) or bool(incomplete_reason)
    zero_products = len(products) == 0

    # Outcome precedence — decided ONCE, independent of --allow-empty.
    # `--allow-empty` controls only whether a zero-product result gets
    # WRITTEN as a file (below); it must never launder a blocked or
    # remote-API-error run into a "complete" status just because the
    # caller also passed --allow-empty, and it must never do so just
    # because SOME batches did return results while the run was, in fact,
    # blocked partway through.
    #
    # Rows gathered by a run that did NOT finish cleanly are `partial` (6),
    # with the cause in `stop_reason` (CLAUDE.md §25: exit 5 and exit 3
    # promise no file; 6 means "some rows, incomplete"). Audit 2026-09-30:
    # this used to return 5 or 3 AND write the rows, so a consumer keyed on
    # the exit code threw away good data. `rejected_rows` counts records
    # the parser refused — the output is then incomplete too.
    if zero_products:
        if remote_api_error:
            status, exit_code = "remote_api_error", EXIT_REMOTE_API_ERROR
        elif blocked:
            status, exit_code = "blocked", EXIT_BLOCKED
        elif partial or rejected_rows:
            status, exit_code = "remote_api_error", EXIT_REMOTE_API_ERROR
        else:
            status, exit_code = "empty", EXIT_ZERO_PRODUCTS
        stop_reason = ("rate_limited" if rate_limited and blocked else incomplete_reason
                       or ("failed_pages" if failed_pages and not blocked and not remote_api_error else status))
    elif remote_api_error or blocked or partial or rejected_rows:
        status, exit_code = "partial", EXIT_PARTIAL
        # A throttle is not a block (CLAUDE.md §24): "blocked" sends the
        # reader to buy a proxy, "rate_limited" to slow down.
        stop_reason = ("remote_api_error" if remote_api_error else "rate_limited" if rate_limited and blocked
                       else "blocked" if blocked else incomplete_reason or ("failed_pages" if partial else "rejected_rows"))
    else:
        status, exit_code = "complete", EXIT_OK
        stop_reason = status

    # The "never overwrite good output with empty" rule: a zero-product
    # outcome (whatever its status above — blocked/remote_api_error/empty
    # all zero out `products`) writes NEITHER file NOR sidecar unless the
    # caller explicitly opted in with --allow-empty. NEVER write a sidecar
    # in the not-written case — a PREVIOUS good <out>.json is left in
    # place untouched, and a stale failure sidecar sitting right beside it
    # would contradict that good data rather than describe it. The
    # engine's own logs carry the diagnostic detail — that's what a
    # failed run's output is FOR, not this file.
    if zero_products and not allow_empty:
        return exit_code

    write_output(products, out_path, fmt)
    extra = dict(extra_meta or {})
    if rejected_rows:
        extra["rejected_rows"] = rejected_rows
    # What diff_runs needs to refuse a meaningless comparison: the cap the
    # run was given (a top-N selection, where "removed" means "fell out of
    # the top N", not "delisted") and a hash binding this sidecar to the
    # exact output file beside it.
    if max_results is not None:
        extra["max_results"] = max_results
        extra["capped"] = len(products) >= max_results if capped is None else capped
    if total_results is not None:
        extra["total_results"] = total_results  # what the site says exists, vs product_count collected
    extra["output_sha256"] = hashlib.sha256(Path(out_path).read_bytes()).hexdigest()
    write_meta(
        out_path, status=status, stop_reason=stop_reason, engine=engine, url=url,
        pages_requested=pages_requested, pages_completed=pages_completed,
        failed_pages=failed_pages, product_count=len(products),
        price_confirmed_pct=price_confirmed_pct, started_at=started_at,
        extra=extra or None,
    )
    return exit_code
