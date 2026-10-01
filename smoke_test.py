#!/usr/bin/env python3
"""smoke_test.py — one file of plain functions with inline/synthetic-
fixture checks. No pytest, no conftest. `tests/test_smoke.py` wraps this as
a single pytest entry point so `pytest` also works, without a second copy
of the checks.

**Honesty note, read before trusting a green run** (same caveat as every
other family member's smoke_test.py): EVERY HTML fixture below is
SYNTHETIC — hand-written to exercise the parsing code paths, not a real
capture of perplexity.ai. Unlike lidl-scraper (which has one real-capture
fixture, `tests/fixtures/lidl_search_real.html`), this repo has NO
real-capture fixture at all yet — no live browser session against
perplexity.ai has been possible from this environment (see
`page_parser.py`'s module docstring for why). A green run here proves the
architecture (exit codes, dedupe, precedence, credential redaction, CLI
validation, engines importing cleanly, the three parsing paths each doing
something sane on the SHAPE of input they're meant for) is sound. It does
NOT prove `page_parser.py`'s selectors match a single byte of what the
real site actually renders — that can only be confirmed by a real capture,
exactly the gap lidl-scraper had before its own first live run (see that
repo's CHANGELOG for what changed once one arrived).

Run directly: `python3 smoke_test.py`
"""
from __future__ import annotations

import asyncio
import inspect as _inspect
import json
import tempfile
from pathlib import Path

import captcha_solver
import diff_runs
import env_config
import output_writer
import page_flow
import page_parser as pp
import proxy_pool
import puppeteer_scraper
import scraper_api_client
import selenium_scraper

try:
    import playwright_scraper
except Exception as exc:  # pragma: no cover — this import itself must never fail
    raise AssertionError(f"playwright_scraper must import cleanly even without playwright installed: {exc}") from exc

ROOT = Path(__file__).parent

RESULTS = []  # (name, ok, detail)


def check(name):
    """Runs the decorated function IMMEDIATELY (at module-load time) and
    records the outcome — same pattern as every other family member's
    smoke_test.py; every check function is named `_` because only RESULTS
    is ever read, nothing looks a check up by name."""
    def decorator(fn):
        try:
            fn()
            RESULTS.append((name, True, ""))
        except AssertionError as exc:
            RESULTS.append((name, False, str(exc)))
        except Exception as exc:  # a check that crashes is still a failure, not an uncaught traceback
            RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
        except SystemExit as exc:  # a CLI helper exiting inside a check would otherwise end the whole suite silently
            RESULTS.append((name, False, f"SystemExit: {exc}"))
        return fn
    return decorator


def asyncio_run_maybe(mod, args):
    """playwright_scraper.run()/puppeteer_scraper.run() are coroutines;
    selenium_scraper.run() is plain sync."""
    result = mod.run(args)
    if _inspect.iscoroutine(result):
        return asyncio.run(result)
    return result


# --------------------------------------------------------------------------- #
# Engine import/CLI hygiene (CLAUDE.md §6)
# --------------------------------------------------------------------------- #
@check("engines import cleanly regardless of installed drivers")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        assert hasattr(mod, "build_arg_parser")
        assert hasattr(mod, "run")


@check("each engine imports its driver at MODULE level, guarded by try/except ImportError")
def _():
    for path in ("playwright_scraper.py", "selenium_scraper.py", "puppeteer_scraper.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "except ImportError as _IMPORT_ERROR" in src, f"{path}: missing guarded driver import"


@check("no forbidden overclaiming wording in any shipped .py/.md/.yml file")
def _():
    # Built from pieces so this file can be scanned too (CLAUDE.md §22: the
    # check used to exempt its own file, where the phrases sat verbatim).
    anti = "anti" + "detect"
    banned = (
        "cloud" + " browser", anti + " browser", "2scraper " + anti + " browser",
        "gate." + "2prx.com", "--" + anti, anti + "_local_api",
    )
    exempt_names = {"CLAUDE.md"}
    venvs = {p.parent for p in ROOT.rglob("pyvenv.cfg")}
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in (".py", ".md", ".html", ".toml", ".cfg", ".yml", ".yaml"):
            continue
        if path.name in exempt_names or path.name.startswith("2scraper"):
            continue
        if ".git" in path.parts or "__pycache__" in path.parts or any(v in path.parents for v in venvs):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for phrase in banned:
            assert phrase not in text, f"{path.relative_to(ROOT)}: contains banned phrase {phrase!r}"


@check("all three engines expose the identical --flag set (CLAUDE.md §4)")
def _():
    def flag_set(mod):
        return {opt for a in mod.build_arg_parser()._actions for opt in a.option_strings if opt.startswith("--")}

    pw, se, pu = flag_set(playwright_scraper), flag_set(selenium_scraper), flag_set(puppeteer_scraper)
    all_engines = pw | se | pu
    for name, flags in (("playwright_scraper", pw), ("selenium_scraper", se), ("puppeteer_scraper", pu)):
        missing = all_engines - flags
        assert not missing, f"{name} is missing {sorted(missing)} that (an)other engine(s) define — flag sets have drifted apart"


@check("all three engines share the same default output filename stem")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        assert mod._default_out("json") == "perplexity_results.json"


@check("all engines take --url/--urls-file, NOT --query/--category (CLAUDE.md §1 divergence)")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        flags = {opt for a in mod.build_arg_parser()._actions for opt in a.option_strings}
        assert "--url" in flags and "--urls-file" in flags
        assert "--query" not in flags and "--category" not in flags


@check("every top-level module is in the Dockerfile COPY and pyproject py-modules (a module left out breaks the image on every run — CLAUDE.md §16)")
def _():
    import re as _re
    if not (ROOT / "Dockerfile").exists() and not (ROOT / "pyproject.toml").exists():
        return  # the Docker image's own copy of this suite ships neither (CLAUDE.md §22)
    modules = sorted(pth.stem for pth in ROOT.glob("*.py"))
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    listed = set(_re.findall(r'"([a-z_]+)"', pyproject.split("py-modules", 1)[1].split("]", 1)[0]))
    for mod in modules:
        assert f"{mod}.py" in docker, f"{mod}.py missing from the Dockerfile COPY"
        assert mod in listed, f"{mod} missing from pyproject py-modules"


@check("engines never request a robots.txt-disallowed path — _resolve_urls filters it out")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        urls, skipped = mod._resolve_urls(mod.build_arg_parser().parse_args(["--url", "https://www.perplexity.ai/search?q=milk"]))
        assert urls == [], f"{mod.__name__}: a disallowed path must never be attempted"
        assert skipped == 1


@check("BOT_CHALLENGE_MARKERS matches the real, captured Cloudflare managed-challenge incident (2026-09-21), not a guess")
def _():
    # Updated once a real perplexity.ai block page existed to check against
    # (see page_parser.py's module docstring and BOT_CHALLENGE_MARKERS
    # comment for the full incident) — before that, this asserted the tuple
    # was EMPTY, since no site-specific marker could be honestly claimed as
    # verified yet. Every marker here must appear in the actual captured
    # fixture, not just be plausible-sounding.
    real_block_page = (ROOT / "tests" / "fixtures" / "perplexity_cloudflare_block_real.html").read_text(encoding="utf-8")
    assert pp.BOT_CHALLENGE_MARKERS, "the real incident below should have left at least one marker"
    for marker in pp.BOT_CHALLENGE_MARKERS:
        assert marker.lower() in real_block_page.lower(), f"{marker!r} does not match the actual captured incident"
    assert len(captcha_solver.GENERIC_BOT_CHALLENGE_MARKERS) > 0
    # The generic detector alone already caught this exact page (via its
    # own "cf-turnstile"/"challenges.cloudflare.com"/"cdn-cgi/challenge-
    # platform" strings) — the site-specific markers above are
    # corroboration, not the only thing standing between this repo and a
    # misreported "empty" run.
    assert captcha_solver.detect_from_html(real_block_page), (
        "detect_from_html must flag the real captured Cloudflare challenge as a block"
    )


# --------------------------------------------------------------------------- #
# output_writer — exit codes / precedence / dedupe (CLAUDE.md §9)
# --------------------------------------------------------------------------- #
@check("exit codes and STATUS_BY_EXIT match the family contract exactly")
def _():
    expected = {0: "complete", 1: "crashed", 2: "bad_usage", 3: "blocked", 4: "empty", 5: "remote_api_error", 6: "partial"}
    assert output_writer.STATUS_BY_EXIT == expected


def _mk_product(sku, price=None, **kw):
    defaults = dict(
        sku=sku, source="perplexity.ai", category=None, title="An Example Page",
        brand=None, price=price, currency=None, price_source=None,
        product_url=f"https://www.perplexity.ai/page/an-example-page-{sku}",
        image_url=None, scraped_at="2026-09-21T00:00:00Z",
        author="Henry", view_count=100, like_count=5,
    )
    defaults.update(kw)
    return output_writer.Product(**defaults)


@check("finish_run: rows gathered by a run that did not finish are PARTIAL (6) with the cause in stop_reason — never 5/3 with a file (CLAUDE.md §25; audit 2026-09-30 got exit 5 AND a written file). Rewrites the old pinned 'exit 5 with products' position deliberately.")
def _():
    cases = (
        (dict(blocked=True, remote_api_error=True), "remote_api_error"),
        (dict(blocked=False, remote_api_error=True), "remote_api_error"),
        (dict(blocked=True, remote_api_error=False), "blocked"),
        (dict(blocked=False, remote_api_error=False, failed_pages=[3]), "failed_pages"),
        (dict(blocked=False, remote_api_error=False, rejected_rows=2), "rejected_rows"),
    )
    for kw, reason in cases:
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "out.json")
            kw = {"failed_pages": None, **kw}
            code = output_writer.finish_run(
                products=[_mk_product("1")], out_path=out, fmt="json", engine="test", url="u",
                pages_requested=3, pages_completed=2, allow_empty=False, started_at=0.0, **kw,
            )
            assert code == output_writer.EXIT_PARTIAL, (kw, code)
            assert Path(out).exists(), "already-collected products must still be written out"
            meta = json.loads(Path(f"{out}.meta.json").read_text())
            assert meta["status"] == "partial" and meta["stop_reason"] == reason, (kw, meta)
            if reason == "rejected_rows":
                assert meta["rejected_rows"] == 2
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "z.json")
        code = output_writer.finish_run(
            products=[], out_path=out, fmt="json", engine="test", url="u", pages_requested=1, pages_completed=0,
            failed_pages=None, blocked=False, remote_api_error=True, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_REMOTE_API_ERROR and not Path(out).exists(), "5 promises no file"

@check("finish_run precedence: blocked+zero-products respects --allow-empty for WHETHER to write, never for the STATUS")
def _():
    with tempfile.TemporaryDirectory() as td:
        out_a = str(Path(td) / "a.json")
        code = output_writer.finish_run(
            products=[], out_path=out_a, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=[2],
            blocked=True, remote_api_error=False, allow_empty=True, started_at=0.0,
        )
        assert code == output_writer.EXIT_BLOCKED
        assert Path(out_a).exists(), "--allow-empty means a zero-product outcome DOES get written"
        meta = json.loads(Path(f"{out_a}.meta.json").read_text())
        assert meta["status"] == "blocked", "--allow-empty must never launder this into 'complete'"

        out_b = str(Path(td) / "b.json")
        code = output_writer.finish_run(
            products=[], out_path=out_b, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=[2],
            blocked=True, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_BLOCKED
        assert not Path(out_b).exists(), "without --allow-empty, a zero-product outcome writes nothing"


@check("finish_run: zero products without --allow-empty writes neither file nor sidecar")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=None,
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_ZERO_PRODUCTS
        assert not Path(out).exists()
        assert not Path(f"{out}.meta.json").exists()


@check("finish_run: partial (failed pages, some products) writes output and reports EXIT_PARTIAL")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[_mk_product("a")], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=2, pages_completed=1, failed_pages=[2],
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_PARTIAL
        assert Path(out).exists()
        meta = json.loads(Path(f"{out}.meta.json").read_text())
        assert meta["status"] == "partial"


@check("finish_run: a clean run with products writes output and reports EXIT_OK/complete")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[_mk_product("a"), _mk_product("b")], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=None,
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_OK
        data = json.loads(Path(out).read_text())
        assert len(data) == 2


@check("Product field order: family-common fields first, perplexity-specific fields after")
def _():
    expected_head = [
        "sku", "source", "category", "title", "brand", "price", "currency",
        "price_source", "product_url", "image_url", "scraped_at",
    ]
    assert output_writer.PRODUCT_FIELD_NAMES[: len(expected_head)] == expected_head
    tail = output_writer.PRODUCT_FIELD_NAMES[len(expected_head):]
    for name in ("author", "view_count", "like_count", "fork_count", "source_count", "sources_json",
                 "section_count", "word_count", "slug", "summary", "read_time_minutes", "published_at", "updated_at"):
        assert name in tail, f"{name} missing from Product's site-specific tail"
    assert "follow_up_question_count" not in tail, "removed 2026-09-30: null on every row (CLAUDE.md §9)"


@check("category/brand/price/currency/price_source are always None — not applicable to a Page")
def _():
    p = _mk_product("a")
    assert p.category is None and p.brand is None
    assert p.price is None and p.currency is None and p.price_source is None


@check("merge_pages dedupes by sku, last-write-wins, in fetch order not arrival order")
def _():
    batch1 = [_mk_product("a", title="A v1"), _mk_product("b", title="B")]
    batch2 = [_mk_product("a", title="A v2"), _mk_product("c", title="C")]  # "a" edited between runs
    merged = output_writer.merge_pages([batch1, batch2])
    skus = [p.sku for p in merged]
    assert skus == ["a", "b", "c"], f"expected batch-order with new items appended, got {skus}"
    a = next(p for p in merged if p.sku == "a")
    assert a.title == "A v2", "later batch's value must win for a repeated sku"


@check("write_csv writes a header even for zero rows")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.csv")
        output_writer.write_csv([], out)
        text = Path(out).read_text()
        assert text.strip() != ""
        assert "sku" in text.splitlines()[0]


# --------------------------------------------------------------------------- #
# proxy_pool — parsing, redaction, dead-marking (family-shared, no site knowledge)
# --------------------------------------------------------------------------- #
@check("proxy_pool rejects a malformed proxy string with ProxyParseError")
def _():
    try:
        proxy_pool.load_proxies("not a proxy!!", None)
        raise AssertionError("expected ProxyParseError")
    except proxy_pool.ProxyParseError:
        pass


@check("proxy_pool parses a credentialed proxy and masks it in logs")
def _():
    proxies = proxy_pool.load_proxies("http://user:secretpass@host.example:8080", None)
    assert len(proxies) == 1
    p = proxies[0]
    assert p.has_auth
    masked = p.masked()
    assert "secretpass" not in masked
    assert "host.example" in masked


@check("proxy_pool.redact_credentials strips login:password out of an arbitrary string")
def _():
    raw = "connect failed: ws://myuser:mysecret@cb.2captcha.com:9222 (5 attempts)"
    redacted = proxy_pool.redact_credentials(raw)
    assert "mysecret" not in redacted
    assert "myuser" not in redacted


# --------------------------------------------------------------------------- #
# captcha_solver — generic + widget-specific detection (family-shared)
# --------------------------------------------------------------------------- #
@check("captcha_solver.detect_from_html finds generic bot-challenge markers")
def _():
    assert captcha_solver.detect_from_html("<html>please complete the g-recaptcha below</html>")
    assert not captcha_solver.detect_from_html("<html><body>ordinary page, no widgets</body></html>")


@check("captcha_solver.identify_widget extracts a Turnstile sitekey")
def _():
    html = '<div class="cf-turnstile" data-sitekey="0x4AAA_example"></div>'
    signal = captcha_solver.identify_widget(html)
    assert signal is not None
    assert signal.captcha_type == captcha_solver.CaptchaType.CLOUDFLARE_TURNSTILE
    assert signal.sitekey == "0x4AAA_example"


# --------------------------------------------------------------------------- #
# env_config — PERPLEXITY_* keys, placeholder detection, precedence
# --------------------------------------------------------------------------- #
@check("env_config.ENV_KEYS matches .env.example exactly, in both directions")
def _():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    documented = {line.split("=", 1)[0] for line in example.splitlines() if "=" in line and not line.startswith("#")}
    assert documented == set(env_config.ENV_KEYS), (documented, set(env_config.ENV_KEYS))


@check("env_config uses PERPLEXITY_ prefixed keys, not a leftover LIDL_/other-site name")
def _():
    for key in env_config.ENV_KEYS:
        assert key == "TWOCAPTCHA_KEY" or key.startswith("PERPLEXITY_"), f"unexpected env key {key!r}"


@check("env_config._is_placeholder treats a braced {...} fragment as unset")
def _():
    assert env_config._is_placeholder("")
    assert env_config._is_placeholder(None)
    assert env_config._is_placeholder("{login}-zone-scraping_browser:{password}@cb.2captcha.com")
    assert not env_config._is_placeholder("a-real-looking-value-123")


@check("env_config.apply_env never overrides an explicitly-set CLI flag")
def _():
    import os as _os
    ns = __import__("argparse").Namespace(proxy="http://explicit:pass@host:1")
    _os.environ["PERPLEXITY_PROXY"] = "http://from-env:pass@host:2"
    try:
        env_config.apply_env(ns, dotenv_path="/nonexistent/.env")
        assert ns.proxy == "http://explicit:pass@host:1"
    finally:
        del _os.environ["PERPLEXITY_PROXY"]


# --------------------------------------------------------------------------- #
# page_parser — URL/id splitting, sku, robots-disallow check, the three
# parsing paths (JSON-LD / OG meta / DOM), priority order
# --------------------------------------------------------------------------- #
_FIX = ROOT / "tests" / "fixtures"
_DISCOVER_URL = "https://www.perplexity.ai/discover/top/openai-unveils-dots-an-always-heYaECNnQuaM0AZ0QSWjaw"
_OLD_PAGE_URL = "https://www.perplexity.ai/page/How-to-Generate-VzUTuvQVSIqru3QGvPihlg"


def _fixture_json(name):
    return json.loads((_FIX / name).read_text(encoding="utf-8"))


@check("URL handling against REAL live URLs: /page/{slug}-{id}, /page/{uuid}, /discover/{topic}/{slug}-{id}, ids containing '.' and '_' (seen live), and non-article URLs refused")
def _():
    assert pp.parse_page_ref(_OLD_PAGE_URL) == ("How-to-Generate", "VzUTuvQVSIqru3QGvPihlg")
    assert pp.article_ref(_DISCOVER_URL) == ("top", "openai-unveils-dots-an-always-heYaECNnQuaM0AZ0QSWjaw")
    dotted = "https://www.perplexity.ai/discover/top/us-completes-troop-withdrawal-.FiJwwm9STi9_gZ5rgyhXQ"
    assert pp.parse_page_ref(dotted) == ("us-completes-troop-withdrawal", ".FiJwwm9STi9_gZ5rgyhXQ")
    assert pp.parse_page_ref("https://www.perplexity.ai/discover/top/judge-orders-nyc-to-scrap-pied-URe1Q_iURaCkk.hSaMoWIQ")[1] == "URe1Q_iURaCkk.hSaMoWIQ"
    uuid_url = "https://www.perplexity.ai/page/573513ba-f415-488a-abbb-7406bcf8a196"
    assert pp.article_ref(uuid_url) == (None, "573513ba-f415-488a-abbb-7406bcf8a196")
    assert pp.parse_page_ref(uuid_url) == (None, None), "a uuid ref carries no id — the sku must come from the API"
    for url in (_OLD_PAGE_URL, _DISCOVER_URL, uuid_url):
        assert pp.is_page_url(url), url
    for url in ("https://www.perplexity.ai/discover", "https://www.perplexity.ai/search?q=x",
                "https://example.com/page/a-AAAAAAAAAAAAAAAAAAAAAA", "https://www.perplexity.ai/hub/blog/x"):
        assert not pp.is_page_url(url), url
    assert pp.article_api_url("P.Mg27lmRU21zVNesSK35g").startswith("https://www.perplexity.ai/rest/article/P.Mg27lmRU21zVNesSK35g?")


@check("is_disallowed_path matches robots.txt's own disallowed prefixes, and lets /page/ and /discover/ through")
def _():
    assert pp.is_disallowed_path("https://www.perplexity.ai/search?q=foo")
    assert pp.is_disallowed_path("https://www.perplexity.ai/search/new")
    assert pp.is_disallowed_path("https://www.perplexity.ai/onboarding/step1")
    assert not pp.is_disallowed_path(_OLD_PAGE_URL)
    assert not pp.is_disallowed_path(_DISCOVER_URL)


@check("make_sku prefers the article's own id over a URL fingerprint, and is deterministic")
def _():
    a = pp.make_sku(page_id="AbCdEfGhIjKlMnOpQrStUv", url="https://www.perplexity.ai/page/x-AbCdEfGhIjKlMnOpQrStUv")
    b = pp.make_sku(page_id="AbCdEfGhIjKlMnOpQrStUv", url="https://www.perplexity.ai/page/y-AbCdEfGhIjKlMnOpQrStUv")
    assert a == b == "perplexity-AbCdEfGhIjKlMnOpQrStUv"
    c1 = pp.make_sku(page_id=None, url="https://www.perplexity.ai/page/some-page")
    assert c1 == pp.make_sku(page_id=None, url="https://www.perplexity.ai/page/some-page")
    assert c1 != pp.make_sku(page_id=None, url="https://www.perplexity.ai/page/other-page")


@check("parse_article_json on the REAL live Discover article payload (2026-09-30): every field, sources deduped across sections")
def _():
    p = pp.parse_article_json(_fixture_json("perplexity_article_discover_live_20260930.json"), url=_DISCOVER_URL)
    assert p is not None
    assert p.sku == "perplexity-heYaECNnQuaM0AZ0QSWjaw"
    assert p.category == "top"
    assert p.title == "OpenAI unveils Dots, an always-on AI agent, at DevDay 2026"
    assert p.product_url == _DISCOVER_URL
    assert p.author == "pagesandbits"
    assert p.image_url and p.image_url.startswith("https://pplx-res.cloudinary.com/")
    assert (p.view_count, p.like_count, p.fork_count) == (0, 0, 0)
    assert p.section_count == 4 and p.word_count and p.word_count > 300
    sources = json.loads(p.sources_json)
    assert p.source_count == len(sources) == len({s["url"] for s in sources}) == 6
    assert sources[0] == {"url": "https://openai.com/index/introducing-dots/", "title": "Introducing dots"}
    assert p.summary.startswith("Powered by GPT-6 Astra")
    assert p.read_time_minutes == 3
    assert p.published_at == "2026-09-29T17:14:10.246772+00:00"
    assert p.updated_at == "2026-09-29T17:15:35.292639"
    assert p.price is None and p.brand is None


@check("parse_article_json on the REAL live classic-Page payload: an old /page/ slug maps to the CANONICAL slug and keeps the same id-based sku")
def _():
    p = pp.parse_article_json(_fixture_json("perplexity_article_page_live_20260930.json"), url=_OLD_PAGE_URL)
    assert p.sku == "perplexity-VzUTuvQVSIqru3QGvPihlg", "the id survived a slug change live — the sku must too"
    assert p.slug == "perplexity-ai-pages-guide-gene-VzUTuvQVSIqru3QGvPihlg"
    assert p.product_url == "https://www.perplexity.ai/page/perplexity-ai-pages-guide-gene-VzUTuvQVSIqru3QGvPihlg"
    assert p.category is None, "a classic Page has no Discover topic"
    assert (p.view_count, p.fork_count) == (2280, 30)
    assert p.section_count == 14 and p.source_count == 38
    assert p.summary is None and p.read_time_minutes == 8
    same = pp.parse_article_json(_fixture_json("perplexity_article_page_live_20260930.json"),
                                 url="https://www.perplexity.ai/page/573513ba-f415-488a-abbb-7406bcf8a196")
    assert same.sku == p.sku, "the uuid form of the same article must dedupe with the slug form"


@check("parse_article_json refuses anything that is not a successful article (the live 400 body, empty entries, no title, junk)")
def _():
    for bad in ({"detail": "Invalid thread url slug"}, {"status": "success", "entries": []},
                {"status": "success", "entries": [{"text": "{}"}]}, [], None, "x", {"status": "failed"}):
        assert pp.parse_article_json(bad, url=_DISCOVER_URL) is None, bad


@check("parse_discover_feed on the REAL live feed payload: article URLs in feed order, topic kept, next_token means more")
def _():
    urls, more = pp.parse_discover_feed(_fixture_json("perplexity_discover_feed_live_20260930.json"), topic="top")
    assert urls[0] == "https://www.perplexity.ai/discover/top/us-completes-troop-withdrawal-.FiJwwm9STi9_gZ5rgyhXQ"
    assert len(urls) == 3 and more is True
    assert all(pp.is_page_url(u) for u in urls)
    assert pp.parse_discover_feed({"items": [], "next_token": None}, topic="top") == ([], False)
    assert "offset=40" in pp.discover_feed_api_url("top", offset=40)


_APP_SHELL = "<script src='https://pplx-next-static-public.perplexity.ai/_next/a.js'></script>" * 4
_LIVE_OG_DEFAULTS = """<meta property="og:title" content="Perplexity">
<meta property="og:url" content="https://www.perplexity.ai/">
<meta property="og:image" content="https://ppl-ai-public.s3.amazonaws.com/static/img/pplx-default-preview.png">"""


@check("the rendered HTML never becomes a row, even a well-formed article page — live, a DEAD URL rendered the previously viewed article's full headings, with no id in the HTML to tell them apart")
def _():
    html = f"<html><head>{_LIVE_OG_DEFAULTS}</head><body>{_APP_SHELL}<h2>OpenAI unveils Dots</h2><h2>What Dots Can Do</h2></body></html>"
    res = pp.parse_page(html, url="https://www.perplexity.ai/discover/top/does-not-exist-AAAAAAAAAAAAAAAAAAAAAA")
    assert res.products == [] and res.source_used == "none"
    assert pp.count_result_cards(html) == 1, "the page DID render — that is all the HTML may say"
    assert not hasattr(pp, "_parse_page_from_dom") and not hasattr(pp, "extract_og_meta")


@check("the DOM fallback refuses a Cloudflare interstitial: the REAL captured challenge page yields no row (live, a local run once reported 'Performing security verification' as an article)")
def _():
    block = (_FIX / "perplexity_cloudflare_block_real.html").read_text(encoding="utf-8")
    fake_heading = block.replace("</body>", "<h2>Performing security verification</h2></body>")
    res = pp.parse_page(fake_heading, url=_OLD_PAGE_URL)
    assert res.products == [] and res.source_used == "none"
    assert pp.count_result_cards(fake_heading) == 0
    assert not pp.is_app_page(block)


@check("parse_page returns a row only from a successful API payload")
def _():
    data = _fixture_json("perplexity_article_discover_live_20260930.json")
    html = f"<html><body>{_APP_SHELL}<h2>DOM Title</h2></body></html>"
    assert pp.parse_page(html, url=_DISCOVER_URL, article_json=data).source_used == "api"
    assert pp.parse_page(html, url=_DISCOVER_URL, article_json={"detail": "x"}).source_used == "none"
    assert pp.parse_page(html, url=_DISCOVER_URL).source_used == "none"


@check("safe_parse_page degrades a bad page instead of crashing the whole batch")
def _():
    original = pp.parse_article_json
    pp.parse_article_json = lambda *a, **kw: (_ for _ in ()).throw(ValueError("simulated parser defect"))
    try:
        res = pp.safe_parse_page("<html></html>", url=_DISCOVER_URL, article_json={"status": "success"})
    finally:
        pp.parse_article_json = original
    assert res.products == [] and res.source_used == "none"


@check("page_flow.decide: the live outcomes — api row; API refused = NO row (blocked on 401/403/429); 400 = not_found (not blocked); challenge = blocked (never parsed); and the API-retry policy")
def _():
    data = _fixture_json("perplexity_article_discover_live_20260930.json")
    app = f"<html><body>{_APP_SHELL}<h2>DOM Title</h2></body></html>"
    block = (_FIX / "perplexity_cloudflare_block_real.html").read_text(encoding="utf-8")

    o = page_flow.decide(url=_DISCOVER_URL, http_status=200, html=app, api_status=200, article_json=data, api_error=None)
    assert o.product and o.source_used == "api" and not o.blocked and not o.warnings

    o = page_flow.decide(url=_DISCOVER_URL, http_status=200, html=app, api_status=403, article_json=None, api_error="not JSON")
    assert o.product is None and o.blocked, "a rendered page with a refused API is not a row"

    assert page_flow.should_retry_api(403, "not JSON") and page_flow.should_retry_api(0, "fetch failed")
    assert not page_flow.should_retry_api(400, None) and not page_flow.should_retry_api(200, None)
    assert sum(page_flow.API_RETRY_DELAYS_S) >= 12, "live: 403 at ~3s, 200 only at ~12s on a fresh profile"

    o = page_flow.decide(url=_DISCOVER_URL, http_status=200, html=app, api_status=400, article_json={"detail": "x"}, api_error=None)
    assert o.not_found and not o.blocked and o.product is None

    o = page_flow.decide(url=_DISCOVER_URL, http_status=403, html=block, api_status=0, article_json=None, api_error="skipped")
    assert o.blocked and o.product is None

    o = page_flow.decide(url=_DISCOVER_URL, http_status=403, html="<html></html>", api_status=403, article_json=None, api_error="not JSON")
    assert o.blocked and o.product is None

    o = page_flow.decide(url=_DISCOVER_URL, http_status=200, html="<html></html>", api_status=500, article_json=None, api_error="HTTP 500")
    assert not o.blocked and o.product is None, "nothing recognised is not a block"
    assert page_flow.is_challenge(block) and not page_flow.is_challenge(app + "cf-turnstile")


# --------------------------------------------------------------------------- #
# CLI validation — bad usage never crashes, never writes output
# --------------------------------------------------------------------------- #
@check("each engine: same --discover flag, and a non-article URL is skipped (never fetched), leaving EXIT_BAD_USAGE when nothing is left")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        args = mod.build_arg_parser().parse_args(["--discover", "top", "--max-results", "5"])
        assert args.discover == "top" and args.max_results == 5
        urls, skipped = mod._resolve_urls(mod.build_arg_parser().parse_args(["--url", "https://www.perplexity.ai/hub/blog/x"]))
        assert urls == [] and skipped == 1, mod.__name__
        with tempfile.TemporaryDirectory() as td:
            args = mod.build_arg_parser().parse_args(["--url", "https://www.perplexity.ai/hub/blog/x", "--out", str(Path(td) / "o.json")])
            assert asyncio_run_maybe(mod, args) == output_writer.EXIT_BAD_USAGE, mod.__name__


@check("each engine: no --url/--urls-file is EXIT_BAD_USAGE, not a crash")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "out.json")
            args = mod.build_arg_parser().parse_args(["--out", out])
            code = asyncio_run_maybe(mod, args)
            assert code == output_writer.EXIT_BAD_USAGE, f"{mod.__name__}: expected EXIT_BAD_USAGE, got {code}"
            assert not Path(out).exists()


@check("each engine: a --url disallowed by robots.txt is EXIT_BAD_USAGE (nothing left to fetch)")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "out.json")
            args = mod.build_arg_parser().parse_args(["--url", "https://www.perplexity.ai/search?q=milk", "--out", out])
            code = asyncio_run_maybe(mod, args)
            assert code == output_writer.EXIT_BAD_USAGE, f"{mod.__name__}: expected EXIT_BAD_USAGE, got {code}"
            assert not Path(out).exists()


@check("each engine: a malformed --proxy is EXIT_BAD_USAGE, not a crash, and writes nothing")
def _():
    # Best-effort, not a requirement that a driver be installed: CLAUDE.md
    # §6 requires smoke_test.py to run cleanly with ZERO engine drivers
    # present (that's exactly what the "offline checks" CI job installs —
    # requirements.txt only, per §16's dependency split), so this must not
    # demand `exercised > 0`. "This particular engine wasn't skipped" is
    # already asserted independently, per engine, by tests.yml's own
    # engine-smoke jobs — that's the right layer for it, not this
    # driver-agnostic offline check (see lidl-scraper's own CI incident
    # writeup for why: an environment that happens to have a driver
    # installed must never be required for this file to pass).
    _IMPORT_ERROR_ATTR = {
        "playwright_scraper": "_PLAYWRIGHT_IMPORT_ERROR",
        "selenium_scraper": "_SELENIUM_IMPORT_ERROR",
        "puppeteer_scraper": "_PYPPETEER_IMPORT_ERROR",
    }
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        if getattr(mod, _IMPORT_ERROR_ATTR[mod.__name__], None) is not None:
            continue
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "out.json")
            args = mod.build_arg_parser().parse_args([
                "--url", "https://www.perplexity.ai/page/x-AbCdEfGhIjKlMnOpQrStUv",
                "--proxy", "not a proxy!!", "--out", out,
            ])
            code = asyncio_run_maybe(mod, args)
            assert code == output_writer.EXIT_BAD_USAGE, (
                f"{mod.__name__}: a malformed --proxy must exit {output_writer.EXIT_BAD_USAGE} "
                f"(bad usage), got {code}"
            )
            assert not Path(out).exists(), f"{mod.__name__}: a bad-usage run must never write output"


@check("selenium_scraper refuses a credentialed --cdp-endpoint with EXIT_BAD_USAGE")
def _():
    args = selenium_scraper.build_arg_parser().parse_args([
        "--url", "https://www.perplexity.ai/page/x-AbCdEfGhIjKlMnOpQrStUv",
        "--cdp-endpoint", "ws://user:pass@cb.2captcha.com:9222",
    ])
    code = selenium_scraper.run(args)
    assert code == output_writer.EXIT_BAD_USAGE


@check("each engine rejects --max-results 0 at the argparse level")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        try:
            mod.build_arg_parser().parse_args([
                "--url", "https://www.perplexity.ai/page/x-AbCdEfGhIjKlMnOpQrStUv", "--max-results", "0",
            ])
            raise AssertionError(f"{mod.__name__}: expected argparse to reject --max-results 0")
        except SystemExit:
            pass


# --------------------------------------------------------------------------- #
# diff_runs / scraper_api_client — sanity only (family-shared, no site knowledge)
# --------------------------------------------------------------------------- #
@check("diff_runs reports added/removed/changed between two real finish_run() outputs")
def _():
    with tempfile.TemporaryDirectory() as td:
        old_out, new_out = str(Path(td) / "old.json"), str(Path(td) / "new.json")
        output_writer.finish_run(
            products=[_mk_product("a", title="A v1"), _mk_product("b", title="B")],
            out_path=old_out, fmt="json", engine="test", url="u", pages_requested=1, pages_completed=1,
            failed_pages=None, blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        output_writer.finish_run(
            products=[_mk_product("a", title="A v1"), _mk_product("c", title="C")],
            out_path=new_out, fmt="json", engine="test", url="u", pages_requested=1, pages_completed=1,
            failed_pages=None, blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        result = diff_runs.diff(old_out, new_out)
        assert result["added"] == ["c"]
        assert result["removed"] == ["b"]


@check("diff_runs refuses to compare a non-'complete' run")
def _():
    with tempfile.TemporaryDirectory() as td:
        old_out, new_out = str(Path(td) / "old.json"), str(Path(td) / "new.json")
        output_writer.finish_run(
            products=[_mk_product("a")], out_path=old_out, fmt="json", engine="test", url="u",
            pages_requested=2, pages_completed=1, failed_pages=[2],
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        output_writer.finish_run(
            products=[_mk_product("a")], out_path=new_out, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=None,
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        try:
            diff_runs.diff(old_out, new_out)
            raise AssertionError("expected a refusal — old run is 'partial', not 'complete'")
        except SystemExit:
            pass


@check("scraper_api_client.TwoCaptchaClient._require_key rejects a missing/empty key")
def _():
    client = scraper_api_client.TwoCaptchaClient("")
    try:
        client._require_key()
        raise AssertionError("expected TwoCaptchaAuthError")
    except scraper_api_client.TwoCaptchaAuthError:
        pass


@check("scraper_api_client honors --captcha-api override, not the module-level API_BASE")
def _():
    client = scraper_api_client.TwoCaptchaClient("fakekey", api_base="https://mock.example.test")
    assert client.api_base == "https://mock.example.test"
    assert client.api_base != scraper_api_client.API_BASE


def _diff_run(td, name, rows, url, **kw):
    out = str(Path(td) / name)
    kw.setdefault("allow_empty", False)
    output_writer.finish_run(products=rows, out_path=out, fmt="json", engine="t", url=url, pages_requested=1,
                             pages_completed=1, failed_pages=None, blocked=False, remote_api_error=False,
                             started_at=0.0, **kw)
    return out


@check("diff_runs refuses different selections, never calls a currency switch a price change (even at the same number), reads a capped top-N's missing SKU as left_selection, and rejects a sidecar that does not describe its file (audit 2026-09-30)")
def _():
    dress, jeans = "https://us.shein.com/pdsearch/dress/", "https://us.shein.com/pdsearch/jeans/"
    with tempfile.TemporaryDirectory() as td:
        a = _diff_run(td, "a.json", [_mk_product("s1", 9.93, currency="USD")], dress)
        b = _diff_run(td, "b.json", [_mk_product("s1", 19.93, currency="EUR")], jeans)
        try:
            diff_runs.diff(a, b)
            raise AssertionError("different selections must be refused")
        except SystemExit as exc:
            assert "different selections" in str(exc)
        r = diff_runs.diff(a, b, allow_different_scope=True)
        assert not r["changed"] and len(r["currency_changed"]) == 1

        c = _diff_run(td, "c.json", [_mk_product("s1", 10.0, currency="USD")], dress)
        e = _diff_run(td, "e.json", [_mk_product("s1", 10.0, currency="EUR")], dress + "?")
        r = diff_runs.diff(c, e)
        assert r["currency_changed"] and not r["changed"], "same number, other currency must still be reported"

        f = _diff_run(td, "f.json", [_mk_product("s1"), _mk_product("s2")], dress, max_results=2)
        g = _diff_run(td, "g.json", [_mk_product("s1"), _mk_product("s3")], dress, max_results=2)
        r = diff_runs.diff(f, g)
        assert r["capped"] and r["left_selection"] == ["s2"] and r["removed"] == [] and r["added"] == ["s3"]
        h = _diff_run(td, "h.json", [_mk_product("s1"), _mk_product("s2")], dress, max_results=50)
        i = _diff_run(td, "i.json", [_mk_product("s1")], dress, max_results=50)
        r = diff_runs.diff(h, i)
        assert r["removed"] == ["s2"] and not r["capped"], "an uncapped run's missing SKU really is removed"

        Path(g).write_text("[]", encoding="utf-8")
        try:
            diff_runs.diff(f, g)
            raise AssertionError("a sidecar whose hash does not match must be refused")
        except SystemExit as exc:
            assert "output_sha256" in str(exc)

@check("the credential scanner FINDS a planted key in every shape seen in the family (JSON-quoted, JSON-escaped, 32-hex next to a key word) and ignores placeholders, type hints and Python-name mappings — a scanner that cannot fail is not one (CLAUDE.md §24/§25)")
def _():
    import importlib.util
    scanner = ROOT / ".github" / "ci_checks.py"
    if not (ROOT / ".github").is_dir():
        return
    spec = importlib.util.spec_from_file_location("shein_ci_checks_planted", scanner)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fake32 = "0123456789abcdef" * 2
    for planted in ('"api_key": "a8f3k2m9q7x1z5b4"', '{\\"api_key\\": \\"a8f3k2m9q7x1z5b4\\"}',
                    "TWOCAPTCHA_KEY=" + fake32, '"clientKey":"' + fake32 + '"'):
        assert mod.scan_text("planted.txt", planted), f"scanner missed a planted credential: {planted!r}"
    for harmless in ("api_key: Optional[str] = None", "TWOCAPTCHA_KEY=your-key-here", '"TWOCAPTCHA_KEY": "twocaptcha_key",'):
        assert not mod.scan_text("ok.txt", harmless), f"false positive: {harmless!r}"

@check("no workflow imports a local module inline — tests.yml calls ci_checks.py instead (CLAUDE.md §26: an inline heredoc import is red only on the first push)")
def _():
    import re as _re
    if not (ROOT / ".github").is_dir():
        return  # the Docker image ships no .github/ (CLAUDE.md §22)
    local = {p.stem for p in ROOT.glob("*.py")}
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = wf.read_text(encoding="utf-8")
        for m in _re.finditer(r"^\s*(?:from\s+([A-Za-z_]\w*)\s+import|import\s+([A-Za-z_]\w*))", text, _re.M):
            name = m.group(1) or m.group(2)
            assert name not in local, f"{wf.name}: imports local module {name!r} inline"
    assert "ci_checks.py --sample-check" in (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

@check(".gitignore covers every artefact a run writes (CLAUDE.md §22/§26): .env copies, --dump-html challenge screenshots, *.pageN dumps, live/ — while sample outputs, fixtures and .env.example stay tracked")
def _():
    import subprocess as _sp
    if not (ROOT / ".git").exists():
        return
    must_ignore = [".env", ".env.bak", ".env.local", "perplexity_results_debug_1.html",
                   "out.json.page3", "live/x.html", "perplexity_results.json", "run.json"]
    must_keep = [".env.example", "sample_output.json", "sample_output.csv", "tests/fixtures/perplexity_article_discover_live_20260930.json"]
    for path in must_ignore:
        assert _sp.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode == 0, f"not ignored: {path}"
    for path in must_keep:
        assert _sp.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode != 0, f"wrongly ignored: {path}"


@check("sidecar records sort, total_results, solves_spent, max_results/capped and the output hash; a throttle with rows is stop_reason=rate_limited (CLAUDE.md §24); diff_runs refuses runs of another --sort")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "o.json")
        code = output_writer.finish_run(
            products=[_mk_product("1")], out_path=out, fmt="json", engine="t", url="u", pages_requested=2,
            pages_completed=1, failed_pages=None, blocked=True, remote_api_error=False, allow_empty=False,
            started_at=0.0, rate_limited=True, total_results=40, max_results=1, extra_meta={"sort": None, "solves_spent": 0},
        )
        meta = json.loads(Path(out + ".meta.json").read_text())
        assert code == output_writer.EXIT_PARTIAL and meta["stop_reason"] == "rate_limited", meta
        assert meta["total_results"] == 40 and meta["capped"] is True and len(meta["output_sha256"]) == 64
        url = "https://www.perplexity.ai/discover (topic=top)"
        a = _diff_run(td, "a.json", [_mk_product("s1")], url, extra_meta={"sort": "a"})
        b = _diff_run(td, "b.json", [_mk_product("s1")], url, extra_meta={"sort": "b"})
        try:
            diff_runs.diff(a, b)
            raise AssertionError("different --sort must be refused")
        except SystemExit as exc:
            assert "--sort" in str(exc)


# --------------------------------------------------------------------------- #
# page_flow — the ONE fetch loop (CLAUDE.md §26)
# --------------------------------------------------------------------------- #
_ENGINES = (playwright_scraper, selenium_scraper, puppeteer_scraper)
_SESSION = {"playwright_scraper": "_PlaywrightSession", "selenium_scraper": "_SeleniumSession", "puppeteer_scraper": "_PyppeteerSession"}
_ENGINE = {"playwright_scraper": "_PlaywrightEngine", "selenium_scraper": "_SeleniumEngine", "puppeteer_scraper": "_PyppeteerEngine"}


@check("the fetch loop exists ONCE: no engine carries its own fetch/discover/finish copy, each calls page_flow.run and page_flow.resolve_urls; the CDP connect goes through connect_with_retry (bounded, retried, 401 explained); pyppeteer disconnects instead of closing the remote browser")
def _():
    import ast as _ast
    for mod in _ENGINES:
        src = (ROOT / f"{mod.__name__}.py").read_text(encoding="utf-8")
        defs = {n.name for n in _ast.walk(_ast.parse(src)) if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))}
        for gone in ("scrape_one_page", "scrape_urls", "collect_discover_urls"):
            assert gone not in defs, f"{mod.__name__}: still defines {gone}"
        for fragment in ("finish_run(", "page_flow.decide(", "API_RETRY_DELAYS_S", "parse_discover_feed("):
            assert fragment not in src, f"{mod.__name__}: loop logic {fragment!r} outside page_flow"
        assert src.count("page_flow.run(") == 1 and mod._resolve_urls is page_flow.resolve_urls, mod.__name__
    for mod in (playwright_scraper, puppeteer_scraper):
        assert "scraper_api_client.connect_with_retry(" in (ROOT / f"{mod.__name__}.py").read_text(encoding="utf-8")
    pw = (ROOT / "playwright_scraper.py").read_text(encoding="utf-8")
    assert "reuse_default and browser.contexts" in pw
    pup = (ROOT / "puppeteer_scraper.py").read_text(encoding="utf-8")
    assert "await browser.disconnect()" in pup and "_release(remote_browser, remote=True)" in pup
    assert "get_event_loop().run_until_complete" not in pup


@check("§26 ops set, derived from page_flow's AST (every session.<op> / engine.<op> the loop uses): each engine's session and engine class provides all of them; flags the loop reads exist in all three")
def _():
    import ast as _ast
    tree = _ast.parse((ROOT / "page_flow.py").read_text(encoding="utf-8"))
    ops = {"session": set(), "engine": set()}
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Attribute) and isinstance(node.value, _ast.Name) and node.value.id in ops:
            ops[node.value.id].add(node.attr)
    assert {"goto", "content", "fetch_json", "wait", "close"} <= ops["session"], ops
    assert {"open", "sleep", "solve_captcha", "readiness_s", "name"} <= ops["engine"], ops
    for mod in _ENGINES:
        sc, ec = getattr(mod, _SESSION[mod.__name__]), getattr(mod, _ENGINE[mod.__name__])
        assert not [o for o in ops["session"] if not hasattr(sc, o)], mod.__name__
        assert not [o for o in ops["engine"] if not hasattr(ec, o)], mod.__name__
        a = mod.build_arg_parser().parse_args(["--discover", "top"])
        assert a.max_solves == 8 and a.delay_between_pages == 2.0 and a.retries == 2, mod.__name__


class _FakeSession:
    def __init__(self, script):
        self.script, self.closed, self.fetches = dict(script), False, []

    async def goto(self, url):
        step = self.script.get("goto")
        if isinstance(step, Exception):
            raise step
        return self.script.get("status", 200)

    async def content(self):
        return self.script.get("html", "<html>" + "pplx-next-static-public " * 5 + "<h2>x</h2></html>")

    async def fetch_json(self, url):
        self.fetches.append(url)
        answers = self.script.get("api", [(200, None, None)])
        return answers[min(len(self.fetches), len(answers)) - 1]

    async def wait(self, seconds):
        return None

    async def close(self):
        self.closed = True


class _FakeEngine:
    name = "fake"
    readiness_s = 0

    def __init__(self, scripts, solve=None):
        self.scripts, self.sessions, self.slept, self._solve = list(scripts), [], [], solve

    async def open(self, proxy):
        script = self.scripts.pop(0) if len(self.scripts) > 1 else self.scripts[0]
        sess = _FakeSession(script)
        self.sessions.append(sess)
        return sess

    async def sleep(self, seconds):
        self.slept.append(seconds)

    async def solve_captcha(self, session, *, html, url):
        return self._solve() if self._solve else None


def _pflow(scripts, argv, *, solve=None):
    args = playwright_scraper.build_arg_parser().parse_args([*argv, "--delay-between-pages", "0"])
    args._solve_budget = page_flow.SolveBudget(args.max_solves)
    urls, _skipped = page_flow.resolve_urls(args)
    topic = None if urls or args.url or args.urls_file else args.discover
    engine = _FakeEngine(scripts, solve)
    with tempfile.TemporaryDirectory() as td:
        args.out = str(Path(td) / "o.json")
        rc = asyncio.run(page_flow.run(engine, args, urls=urls, discover_topic=topic, proxy_pool=None, client=None, started_at=0.0))
        meta_p = Path(args.out + ".meta.json")
        meta = json.loads(meta_p.read_text()) if meta_p.exists() else None
        rows = json.loads(Path(args.out).read_text()) if Path(args.out).exists() else None
    assert all(s.closed for s in engine.sessions), "every opened session must be closed"
    return rc, meta, rows, engine


def _urls_file(n):
    f = Path(tempfile.mkdtemp()) / "urls.txt"
    f.write_text("\n".join(f"https://www.perplexity.ai/page/a-{str(i).zfill(22)}" for i in range(n)), encoding="utf-8")
    return f


@check("page_flow END TO END with a fake engine on the REAL 2026-09-30 API captures: an article reads fully; a dead URL (API 400) is page_not_found, not blocked; a refused API is retried, then read; a Cloudflare page is blocked and never parsed; --discover pages the real feed; the paid-solve budget stops at --max-solves")
def _():
    art = _fixture_json("perplexity_article_discover_live_20260930.json")
    rc, meta, rows, eng = _pflow([{"api": [(200, art, None)]}], ["--url", _DISCOVER_URL])
    assert rc == output_writer.EXIT_OK and rows[0]["sku"] == "perplexity-heYaECNnQuaM0AZ0QSWjaw" and meta["solves_spent"] == 0

    rc, meta, rows, eng = _pflow([{"api": [(400, {"detail": "x"}, None)]}], ["--url", _DISCOVER_URL])
    assert rc == output_writer.EXIT_ZERO_PRODUCTS and len(eng.sessions[0].fetches) == 1, "a 400 is final, never retried"

    rc, meta, rows, eng = _pflow([{"api": [(403, None, "not JSON"), (403, None, "not JSON"), (200, art, None)]}], ["--url", _DISCOVER_URL])
    assert rc == output_writer.EXIT_OK and len(eng.sessions[0].fetches) == 3 and eng.slept[:2] == [3, 5], (rc, eng.slept)

    block = (_FIX / "perplexity_cloudflare_block_real.html").read_text(encoding="utf-8")
    rc, meta, rows, eng = _pflow([{"html": block, "status": 403}], ["--url", _DISCOVER_URL])
    assert rc == output_writer.EXIT_BLOCKED and eng.sessions[0].fetches == [], "a challenge page is never asked for the API"

    feed = _fixture_json("perplexity_discover_feed_live_20260930.json")
    feed_last = dict(feed, next_token=None)
    rc, meta, rows, eng = _pflow([{"api": [(200, feed_last, None)]}, {"api": [(200, art, None)]}], ["--discover", "top", "--max-results", "2"])
    assert rc == output_writer.EXIT_OK and meta["discover_topic"] == "top" and meta["pages_requested"] == 2, meta

    solves = []
    rc, meta, rows, eng = _pflow([{"html": block, "status": 403}], ["--urls-file", str(_urls_file(3)), "--max-solves", "1"],
                                 solve=lambda: solves.append(1) or {"action": "warning_solver_error"})
    assert len(solves) == 1 and rc == output_writer.EXIT_BLOCKED, (len(solves), rc)


def run() -> int:
    """All @check-decorated functions above already ran at import time
    (that's the point — see the `check()` docstring) and self-registered
    into RESULTS. This just reports them."""
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print(f"smoke_test: {passed}/{len(RESULTS)} checks passed")
    for name, detail in failed:
        print(f"  FAIL: {name}\n        {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    import sys
    sys.exit(run())
