#!/usr/bin/env python3
"""diff_runs.py — diff two runs of this scraper by `sku`.

Refuses to compare two runs that are not both `status == "complete"` in
their `.meta.json` sidecar: a partial run's un-fetched pages would otherwise
read as delisted products, which is a false signal worse than no diff at
all.

What is compared, per article present in both runs:

- `changed`: the article itself changed -- any of `CONTENT_FIELDS`
  (title, author, summary, dates, section/word/source counts). These are
  what `--fail-on-change` acts on.
- `counters_changed`: only the view/like/fork counters moved. They move on
  almost every run, so they are reported but never fail `--fail-on-change`.

The family row's price/currency columns are always null on this site (an
article has no price), so they are not compared at all.

Refusals:

- Two runs must share their selection -- the sidecar `url` and
  `selection` (the URL list or Discover topic, and the limit). Otherwise
  the diff is refused.
- When either run was capped by `--max-results` (sidecar `capped`), an
  article missing from the new run is `left_selection` (it fell out of the
  top N), not `removed`.
- A sidecar carrying `output_sha256` must match the file beside it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Dict, List
from urllib.parse import parse_qsl, urlencode, urlparse

from output_writer import PRODUCT_FIELD_NAMES

# What makes an article "changed". Every name must be a real column: a name
# the row does not have would compare None against None forever and report
# nothing (checked at import below).
CONTENT_FIELDS = (
    "title", "author", "summary", "published_at", "updated_at",
    "section_count", "word_count", "source_count", "read_time_minutes",
    "slug", "image_url",
)
# Engagement counters: reported separately, never a reason to fail.
COUNTER_FIELDS = ("view_count", "like_count", "fork_count")

_unknown = [f for f in CONTENT_FIELDS + COUNTER_FIELDS if f not in PRODUCT_FIELD_NAMES]
if _unknown:  # pragma: no cover - a guard against a renamed column
    raise ImportError(f"diff_runs tracks fields the row does not have: {_unknown}")


def _load_meta(run_path: str) -> dict:
    meta_path = Path(f"{run_path}.meta.json")
    if not meta_path.exists():
        raise SystemExit(f"error: no sidecar found at {meta_path} — was this run produced by finish_run()?")
    return json.loads(meta_path.read_text(encoding="utf-8"))


def _load_rows(run_path: str) -> List[dict]:
    path = Path(run_path)
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    if path.suffix == ".csv":
        import csv
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    raise SystemExit(f"error: unsupported run file extension: {path.suffix}")


def _index_by_sku(rows: List[dict]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for row in rows:
        sku = row.get("sku")
        if sku:
            out[sku] = row
    return out


def normalize_scope(url: str) -> str:
    """host + path + sorted query — what makes two runs the same selection."""
    parts = urlparse(url or "")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return f"{parts.netloc.lower()}{parts.path.rstrip('/')}?{query}"


def diff(old_path: str, new_path: str, *, allow_different_scope: bool = False) -> dict:
    old_meta, new_meta = _load_meta(old_path), _load_meta(new_path)
    for label, meta in (("old", old_meta), ("new", new_meta)):
        if meta.get("status") != "complete":
            raise SystemExit(
                f"error: refusing to diff — {label} run status is "
                f"{meta.get('status')!r}, not 'complete'. A partial run's "
                f"un-fetched pages would read as delisted products."
            )
    for label, path, meta in (("old", old_path, old_meta), ("new", new_path, new_meta)):
        expected = meta.get("output_sha256")
        if expected and hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
            raise SystemExit(f"error: {label} run's sidecar does not describe {path} (output_sha256 mismatch).")
    if not allow_different_scope and normalize_scope(old_meta.get("url", "")) != normalize_scope(new_meta.get("url", "")):
        raise SystemExit(
            f"error: refusing to diff runs of different selections: {old_meta.get('url')!r} vs "
            f"{new_meta.get('url')!r}. Pass --allow-different-scope to compare them anyway."
        )
    if not allow_different_scope and (old_meta.get("sort") or None) != (new_meta.get("sort") or None):
        raise SystemExit(
            f"error: refusing to diff runs with different --sort ({old_meta.get('sort')!r} vs "
            f"{new_meta.get('sort')!r}) — a capped top-N under another ordering is another selection."
        )
    if not allow_different_scope and old_meta.get("selection") != new_meta.get("selection"):
        raise SystemExit("error: refusing to diff different input selections (or legacy metadata without selection).")
    capped = bool(old_meta.get("capped") or new_meta.get("capped"))

    old_rows, new_rows = _index_by_sku(_load_rows(old_path)), _index_by_sku(_load_rows(new_path))
    old_skus, new_skus = set(old_rows), set(new_rows)

    added = sorted(new_skus - old_skus)
    missing = sorted(old_skus - new_skus)
    removed, left_selection = ([], missing) if capped else (missing, [])
    changed, counters_changed = [], []

    for sku in sorted(old_skus & new_skus):
        o, n = old_rows[sku], new_rows[sku]
        content = _changes(o, n, CONTENT_FIELDS)
        if content:
            changed.append({"sku": sku, "title": n.get("title"), "fields": content})
        counters = _changes(o, n, COUNTER_FIELDS)
        if counters:
            counters_changed.append({"sku": sku, "title": n.get("title"), "fields": counters})

    return {
        "added": added, "removed": removed, "left_selection": left_selection,
        "changed": changed, "counters_changed": counters_changed,
        "old_count": len(old_rows), "new_count": len(new_rows), "capped": capped,
    }


def _norm(value):
    """CSV reads every cell back as text and a null as ''; compare on that."""
    return "" if value is None else str(value)


def _changes(old: dict, new: dict, names) -> Dict[str, dict]:
    return {name: {"old": old.get(name), "new": new.get(name)}
            for name in names if _norm(old.get(name)) != _norm(new.get(name))}


def main() -> int:
    p = argparse.ArgumentParser(description="Diff two perplexity-scraper runs by sku")
    p.add_argument("old_run")
    p.add_argument("new_run")
    p.add_argument("--fail-on-change", action="store_true",
                    help="Exit 1 if any article's content changed (counters_changed rows are ignored)")
    p.add_argument("--json", action="store_true", help="Print the diff as JSON instead of a summary")
    p.add_argument("--allow-different-scope", action="store_true",
                    help="Compare runs whose selections (URL list, Discover topic, limit) differ")
    args = p.parse_args()

    result = diff(args.old_run, args.new_run, allow_different_scope=args.allow_different_scope)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"{args.old_run} ({result['old_count']}) -> {args.new_run} ({result['new_count']})")
        print(f"  added:          {len(result['added'])}")
        print(f"  removed:        {len(result['removed'])}")
        if result["capped"]:
            print(f"  left_selection: {len(result['left_selection'])} (a capped top-N run: fell out of the selection, not delisted)")
        print(f"  changed:        {len(result['changed'])}")
        print(f"  counters_changed: {len(result['counters_changed'])} (views/likes/forks only; ignored by --fail-on-change)")

    if args.fail_on_change and result["changed"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
