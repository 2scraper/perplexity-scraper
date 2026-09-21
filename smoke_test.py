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
    banned = (
        "cloud browser", "antidetect browser", "2scraper antidetect browser",
        "gate.2prx.com", "--antidetect", "antidetect_local_api",
    )
    exempt_names = {"smoke_test.py", "CLAUDE.md"}
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in (".py", ".md", ".html", ".toml", ".cfg", ".yml", ".yaml"):
            continue
        if path.name in exempt_names or path.name.startswith("2scraper"):
            continue
        if ".git" in path.parts:
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


@check("all engines do a single scroll-to-bottom-and-back per page, not a repeated pagination loop")
def _():
    for path in ("playwright_scraper.py", "selenium_scraper.py", "puppeteer_scraper.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "document.body.scrollHeight" in src, f"{path}: missing the lazy-content scroll pass"
        assert "s-load-more__button" not in src, f"{path}: leftover lidl-specific pagination control"


@check("captcha markers only classify a page as blocked when the article did not render")
def _():
    for path in ("playwright_scraper.py", "selenium_scraper.py", "puppeteer_scraper.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "captcha_detected and not result.products" in src, (
            f"{path}: a marker can still turn a healthy Page into EXIT_BLOCKED"
        )


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


def _mk_product(sku, **kw):
    defaults = dict(
        sku=sku, source="perplexity.ai", category=None, title="An Example Page",
        brand=None, price=None, currency=None, price_source=None,
        product_url=f"https://www.perplexity.ai/page/an-example-page-{sku}",
        image_url=None, scraped_at="2026-09-21T00:00:00Z",
        author="Henry", view_count=100, follow_up_question_count=5,
    )
    defaults.update(kw)
    return output_writer.Product(**defaults)


@check("finish_run precedence: remote_api_error status is never laundered into 'complete' just because products were present")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[_mk_product("a")], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=None,
            blocked=True, remote_api_error=True, allow_empty=True, started_at=0.0,
        )
        assert code == output_writer.EXIT_REMOTE_API_ERROR
        assert Path(out).exists(), "already-collected products must still be written out"
        meta = json.loads(Path(f"{out}.meta.json").read_text())
        assert meta["status"] == "remote_api_error", meta["status"]


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
    for name in ("author", "view_count", "follow_up_question_count", "source_count", "sources_json", "slug"):
        assert name in tail, f"{name} missing from Product's site-specific tail"


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
@check("parse_page_ref splits a real-shaped /page/{slug}-{id} URL into (slug, id)")
def _():
    slug, page_id = pp.parse_page_ref("https://www.perplexity.ai/page/ai-generated-images-tools-prom-efxu3L04SpufSVPD532HQg")
    assert slug == "ai-generated-images-tools-prom"
    assert page_id == "efxu3L04SpufSVPD532HQg"
    assert pp.is_page_url("https://www.perplexity.ai/page/foo-AbCdEfGhIjKlMnOpQrStUv")
    assert not pp.is_page_url("https://www.perplexity.ai/search?q=foo")


@check("parse_page_ref returns (None, None) for a non-Page URL")
def _():
    slug, page_id = pp.parse_page_ref("https://www.perplexity.ai/discover")
    assert slug is None and page_id is None


@check("is_disallowed_path matches robots.txt's own disallowed prefixes, and lets /page/ through")
def _():
    assert pp.is_disallowed_path("https://www.perplexity.ai/search?q=foo")
    assert pp.is_disallowed_path("https://www.perplexity.ai/search/new")
    assert pp.is_disallowed_path("https://www.perplexity.ai/onboarding/step1")
    assert not pp.is_disallowed_path("https://www.perplexity.ai/page/foo-AbCdEfGhIjKlMnOpQrStUv")


@check("make_sku prefers the Page's own id over a URL fingerprint, and is deterministic")
def _():
    a = pp.make_sku(page_id="AbCdEfGhIjKlMnOpQrStUv", url="https://www.perplexity.ai/page/x-AbCdEfGhIjKlMnOpQrStUv")
    b = pp.make_sku(page_id="AbCdEfGhIjKlMnOpQrStUv", url="https://www.perplexity.ai/page/y-AbCdEfGhIjKlMnOpQrStUv")
    assert a == b == "perplexity-AbCdEfGhIjKlMnOpQrStUv"
    c1 = pp.make_sku(page_id=None, url="https://www.perplexity.ai/page/some-page")
    c2 = pp.make_sku(page_id=None, url="https://www.perplexity.ai/page/some-page")
    c3 = pp.make_sku(page_id=None, url="https://www.perplexity.ai/page/other-page")
    assert c1 == c2, "same URL must fingerprint to the same sku across runs"
    assert c1 != c3


_JSON_LD_ARTICLE_HTML = """
<html><body>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Article","headline":"Example Article Title",
 "author":{"@type":"Person","name":"Henry"},
 "image":["https://img.example/cover.jpg"],
 "url":"https://www.perplexity.ai/page/example-article-AbCdEfGhIjKlMnOpQrStUv",
 "articleBody":"This is a short body of example text used only to exercise the word count path.",
 "datePublished":"2026-08-01T00:00:00Z"}
</script>
</body></html>
"""


@check("parse_page: a JSON-LD Article node is the preferred path when present")
def _():
    res = pp.parse_page(_JSON_LD_ARTICLE_HTML, url="https://www.perplexity.ai/page/example-article-AbCdEfGhIjKlMnOpQrStUv")
    assert res.source_used == "json_ld"
    assert len(res.products) == 1
    p = res.products[0]
    assert p.sku == "perplexity-AbCdEfGhIjKlMnOpQrStUv"
    assert p.title == "Example Article Title"
    assert p.author == "Henry"
    assert p.image_url == "https://img.example/cover.jpg"
    assert p.published_at == "2026-08-01T00:00:00Z"
    assert p.word_count and p.word_count > 0
    assert p.category is None and p.price is None, "not applicable to a Page — see output_writer.Product docstring"


_OG_META_ONLY_HTML = """
<html><head>
<meta property="og:title" content="OG-only Example Page">
<meta property="og:image" content="https://img.example/og-cover.jpg">
<meta property="og:url" content="https://www.perplexity.ai/page/og-only-example-BcDeFgHiJkLmNoPqRsTuVw">
<link rel="canonical" href="https://www.perplexity.ai/page/og-only-example-BcDeFgHiJkLmNoPqRsTuVw">
</head><body><div id="app"></div></body></html>
"""


@check("parse_page: OG meta tags are the fallback when no JSON-LD Article/CreativeWork/WebPage node is present")
def _():
    res = pp.parse_page(_OG_META_ONLY_HTML, url="https://www.perplexity.ai/page/og-only-example-BcDeFgHiJkLmNoPqRsTuVw")
    assert res.source_used == "og_meta", res.source_used
    assert len(res.products) == 1
    p = res.products[0]
    assert p.title == "OG-only Example Page"
    assert p.image_url == "https://img.example/og-cover.jpg"
    assert p.slug == "og-only-example"


@check("parse_page: JSON-LD is preferred over OG meta when both are present on the same page")
def _():
    combined = _JSON_LD_ARTICLE_HTML.replace("</body>", _OG_META_ONLY_HTML.split("<body>")[1])
    res = pp.parse_page(combined, url="https://www.perplexity.ai/page/example-article-AbCdEfGhIjKlMnOpQrStUv")
    assert res.source_used == "json_ld"


_DOM_FALLBACK_HTML = """
<html><body>
<article>
<h1>Rendered Fallback Page</h1>
<div class="byline">By Nikhil</div>
<span aria-label="1.2k views">1.2k</span>
<span aria-label="42 questions asked">42</span>
<h2>First Section</h2>
<p>Some example body text used only to exercise the word/section counting path.</p>
<h2>Second Section</h2>
<div class="sources">
  <a href="https://example.com/one">Example Source One</a>
  <a href="https://example.com/two">Example Source Two</a>
</div>
</article>
</body></html>
"""


@check("parse_page: DOM fallback parses title/author/counts/sources when no JSON-LD or OG meta is present")
def _():
    res = pp.parse_page(_DOM_FALLBACK_HTML, url="https://www.perplexity.ai/page/rendered-fallback-CdEfGhIjKlMnOpQrStUvWx")
    assert res.source_used == "dom", res.source_used
    assert len(res.products) == 1
    p = res.products[0]
    assert p.title == "Rendered Fallback Page"
    assert p.author == "By Nikhil"
    assert p.view_count == 1200, p.view_count
    assert p.follow_up_question_count == 42, p.follow_up_question_count
    assert p.source_count == 2
    sources = json.loads(p.sources_json)
    assert {s["url"] for s in sources} == {"https://example.com/one", "https://example.com/two"}
    assert p.section_count == 2
    assert p.word_count and p.word_count > 0


@check("parse_page returns source_used='none' and no products when nothing recognisable renders")
def _():
    res = pp.parse_page("<html><body><div id='app'></div></body></html>", url="https://www.perplexity.ai/page/empty-shell-DeFgHiJkLmNoPqRsTuVwXy")
    assert res.products == []
    assert res.source_used == "none"


@check("_parse_count handles both a raw number and an abbreviated k/m suffix")
def _():
    assert pp._parse_count("1,234") == 1234
    assert pp._parse_count("1.2k") == 1200
    assert pp._parse_count("3.4M") == 3_400_000
    assert pp._parse_count(None) is None
    assert pp._parse_count("no digits here") is None


@check("count_result_cards is a cheap presence check with no readiness wait")
def _():
    assert pp.count_result_cards(_DOM_FALLBACK_HTML) == 1
    assert pp.count_result_cards("<html><body>nothing here</body></html>") == 0


@check("safe_parse_page degrades a bad page instead of crashing the whole batch")
def _():
    # Family invariant (CLAUDE.md §6/§10): a parse-time exception on one
    # page must degrade to an empty result, never propagate and crash the
    # multi-URL batch, discarding every Page already collected from earlier
    # URLs. Reproduce a genuine parsing-time defect (not a caller passing
    # the wrong type) by monkeypatching the primary extraction path.
    original = pp.extract_json_ld
    pp.extract_json_ld = lambda *a, **kw: (_ for _ in ()).throw(ValueError("simulated parser defect"))
    try:
        res = pp.safe_parse_page("<html></html>", url="https://www.perplexity.ai/page/x-AbCdEfGhIjKlMnOpQrStUv")
    finally:
        pp.extract_json_ld = original
    assert res.products == []
    assert res.source_used == "none"


# --------------------------------------------------------------------------- #
# CLI validation — bad usage never crashes, never writes output
# --------------------------------------------------------------------------- #
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
