# perplexity-scraper

![release](https://img.shields.io/github/v/release/2scraper/perplexity-scraper?sort=semver)
![tests](https://github.com/2scraper/perplexity-scraper/actions/workflows/tests.yml/badge.svg)
![canary](https://github.com/2scraper/perplexity-scraper/actions/workflows/canary.yml/badge.svg)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![licence](https://img.shields.io/badge/licence-MIT-green)
![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer-informational)
![local-first](https://img.shields.io/badge/local--first-yes-success)

A [Perplexity Pages](https://www.perplexity.ai/) scraper: a Page URL (or a
file of them) in, one row of title/author/sources/engagement-counter data
out per Page. Three engines (Playwright primary, Selenium and
Puppeteer/pyppeteer for parity), JSON or CSV output, an open, documented
`Product` schema. Part of the [2scraper](https://github.com/2scraper)
family — same output contract, exit codes, and family modules as
`lidl-scraper` / `stockx-scraper` / `skyscanner-scraper`.

## Read this before trusting a run

**No live browser capture of perplexity.ai exists yet.** Every sibling
repo in this family started the same way and got corrected by a real
capture (see `lidl-scraper`'s own README history for what that looked
like); this repo hasn't reached that point. What's below is either
confirmed from public, static sources, or explicitly marked as an
unconfirmed guess — nothing in between, per this family's honesty rule
(`CLAUDE.md` §15).

**Confirmed** (from `robots.txt`, the sitemap, and third-party/first-party
writeups — no browser needed):

- Perplexity Pages are real, public, wiki-style articles at
  `https://www.perplexity.ai/page/{slug}-{22-char-id}`. `robots.txt`
  disallows `/*?*q=`, `/search*`, `/search/new`, `/marketing/prerelease/`,
  `/onboarding/`, `/join/` for every crawler — `/page/...` is **not** on
  that list.
- **Pages have no site-search mechanism.** There is no `?q=`-style
  endpoint against perplexity.ai that surfaces Pages, and `robots.txt`
  explicitly disallows the shapes that would look like one. This is why
  this repo's CLI is `--url`/`--urls-file`, not the `--query`/`--category`
  every sibling repo uses — see `page_parser.py`'s module docstring for
  the full reasoning. It's a genuine architectural fact about this site,
  not a missing feature.
- A Page has a title, section headings, images, body text, a sources/
  citations list, a view count, a follow-up-question count, and author
  attribution (Perplexity's own announcement blog for the feature
  confirms all of these; a live "Ask AI" box is also present but is
  explicitly out of scope here — see "Known limitations").

**Unconfirmed — best-effort guesses, marked `# TODO: verify live` in
`page_parser.py`**: the exact DOM selectors for every field above, whether
the site emits any JSON-LD at all, whether Open Graph meta tags are
server-rendered for a Page (a reasonable bet — see `page_parser.py` — but
untested), and `NAV_TIMEOUT_MS`/`READINESS_WAIT_MS`, which are carried
over unchanged from `lidl-scraper`'s own measured values, not
independently measured against this site.

Direct HTTP access to perplexity.ai is blocked from every automated shell
this repo was built in — the first real live run has to come from a
human's own terminal, exactly as it did for `lidl-scraper`/
`skyscanner-scraper`/`stockx-scraper` before this repo existed. If you run
this against the real site, please open an issue or PR with what you
found (matching or not) — `page_parser.py`'s selectors are written to be
easy to correct in place once a real capture confirms or refutes them.

## Local-first

Like the rest of the family, this does **not** require 2Captcha's paid
Scraping Browser API to run. The default is an ordinary local headless
Chromium, no proxy, no key, no account. `--proxy` / `--cdp-endpoint` /
`--fingerprint` are opt-in power options for volume, a specific exit
country, or a consistent device identity — carried over here as an
architectural choice, same as every sibling repo, even though (see above)
it hasn't been live-measured on *this* site yet — it's unconfirmed whether
perplexity.ai challenges a plain browser visit at all.

## Install

Pick one engine (installing more than one into the same environment is not
supported — see "Engines" below):

```bash
pip install -r requirements-playwright.txt && playwright install chromium   # primary
pip install -r requirements-selenium.txt                                    # needs a matching chromedriver
pip install -r requirements-puppeteer.txt                                   # pyppeteer — see its own warning below
```

Copy `.env.example` to `.env` — leave it blank for a normal first run (see
"Local-first" above) and fill in what you use later. `python3 env_config.py`
shows what was picked up without ever printing a secret.

## Usage

```bash
# a single Page
python3 playwright_scraper.py --url "https://www.perplexity.ai/page/some-article-AbCdEfGhIjKlMnOpQrStUv" --format json --out results.json

# a batch of Pages, one URL per line in a file
python3 playwright_scraper.py --urls-file pages.txt --max-results 20 --dump-html

# with 2Captcha's Scraping Browser API (opt-in — see "Local-first" above)
python3 playwright_scraper.py --url "https://www.perplexity.ai/page/..." --cdp-endpoint "$PERPLEXITY_CDP_ENDPOINT"
```

`selenium_scraper.py` and `puppeteer_scraper.py` accept the identical flag
set and produce the identical output contract — see "Engines" for the two
places they genuinely can't behave the same as Playwright.

### Flags

`--url --urls-file --max-results --delay-between-pages --format --out
--retries --retry-delay --proxy --proxy-file --proxy-shuffle
--proxy-block-retries --twocaptcha-key --captcha-api --solve-captcha
--min-score --cdp-endpoint --fingerprint --fp-tags --fp-country
--allow-empty --dump-html --headless/--headful`

Identical across all three engines — a `smoke_test.py` check asserts the
three parsers' flag sets never drift apart. `--url` takes priority over
`--urls-file` when both are given. `--max-results` caps how many URLs from
`--urls-file` are actually fetched this run (there is exactly zero or one
Product per Page — it does not cap a product count within one page, the
way it does in the query-based sibling repos). A URL whose path matches
one of `robots.txt`'s disallowed prefixes is skipped — logged and never
fetched — rather than attempted; `--url`/`--urls-file` made up entirely of
disallowed paths is `EXIT_BAD_USAGE`. `--fingerprint`/`--fp-tags`/
`--fp-country` apply to all three engines: each sets whatever user agent
the 2Captcha Fingerprint API returns via its own driver's real primitive
(Playwright's `new_context(user_agent=...)`, pyppeteer's
`page.setUserAgent()`, Chrome's own `--user-agent=` switch under
Selenium) — see "What this repo deliberately does NOT apply from a
fingerprint" below. `--captcha-api` overrides the 2Captcha REST base URL
(testing only). `--min-score` is 2Captcha's own `minScore` field on a
`RecaptchaV3Task` request (0.3 default, matching the rest of the family).

### Family flags that don't apply here — and why

- **`--query` / `--category` / `--sort` / `--zip` / `--store-id`**: this
  site has no search or commerce concept to attach any of these to — see
  "Read this before trusting a run" above. `--url`/`--urls-file` are this
  repo's actual, structurally different equivalent.
- **`--max-scrolls` / `--stall-rounds` / `--scroll-delay`**: every sibling
  repo's scroll/pagination loop exists because a search-results page
  keeps rendering more results as you scroll or click "load more". A
  single Page is not paginated that way — this engine does one bounded
  scroll-to-bottom-and-back pass per Page (to surface any lazy-loaded
  content) and stops; `--delay-between-pages` is this repo's actual
  equivalent for a `--urls-file` batch (politeness between fetches, not
  between scroll rounds).
- **`--concurrency` / `--proxy-rotate`**: same reasoning as
  `skyscanner-scraper`/`lidl-scraper` — no independently-addressable units
  to parallelize or rotate an exit between within a single fetch.
  `proxy_pool.ProxyPool.worker_view()` is still ported verbatim per the
  family's "copy the core, verbatim" rule (§7) and stays tested, for if
  a future feature (e.g. fetching a `--urls-file` batch concurrently)
  introduces an actual parallelizable unit.

### What this repo deliberately does NOT apply from a fingerprint

`fingerprint_client.py` only ever extracts and applies the user agent from
a 2Captcha Fingerprint API profile — never a locale or timezone, for the
same reason every sibling repo states: this session could not get a
confirmed field name for either from 2Captcha's own public reference, and
a previous family member shipped a *fabricated* locale that went unnoticed
for months. Omitting a signal honestly beats guessing it.

Credentials belong in `.env` / `PERPLEXITY_PROXY` / `TWOCAPTCHA_KEY` —
never as literal `--proxy`/`--twocaptcha-key` text on a shared or logged
command line if you can avoid it.

## Output contract

`Product` (`output_writer.py`) — family-common columns first, Page-specific
columns after:

```
sku, source, category, title, brand, price, currency, price_source, product_url,
image_url, scraped_at,
author, view_count, follow_up_question_count, source_count, sources_json,
section_count, word_count, slug, published_at
```

Unlike a commerce listing, a Perplexity Page is a free wiki article:
`category`, `brand`, `price`, `currency`, and `price_source` are always
`null` here — kept for schema parity across the family (CLAUDE.md §9)
rather than dropped, exactly the way `skyscanner-scraper` repurposes
`brand` for "operating airline" instead of leaving the shared contract
behind. `sku` is the Page's own 22-character id (the stable part of its
URL), falling back to a deterministic fingerprint of the full URL only
when no id-shaped segment could be split out. `sources_json` is the
Page's citations list, JSON-encoded as a string so every row still fits
one flat CSV line — decode it with `json.loads()` if you need the list
back. `sample_output.json`/`sample_output.csv` are **clearly fictional
placeholder rows** (see the `[FICTIONAL SAMPLE ROW]` marker in the title
field) — not a real capture, per the "Read this before trusting a run"
section above.

**Exit codes**: `0` complete · `1` crash · `2` bad usage · `3` blocked ·
`4` zero products (and nothing was written) · `5` remote API error · `6`
partial. Every completed/partial run writes a `<out>.meta.json` sidecar
with `status`, `pages_completed` (URLs actually fetched, here),
`failed_pages` and `price_confirmed_pct` (always `null` here — see
"Output contract" above) — **except** a failed/empty/blocked/remote-API-
error run, which writes no sidecar and no output at all, so it can never
overwrite a previous good run (`--allow-empty` opts out of the "don't
write an empty result" half of that guard only — see `output_writer.
finish_run`'s docstring for the exact precedence rule and why products
being present never launders a blocked/remote-API-error run into
"complete").

## Fetching, not pagination

There is nothing to paginate: each engine fetches one Page URL, does a
single bounded scroll-to-bottom-and-back pass (in case any content on the
page is lazy-loaded — unconfirmed either way), parses it once, and moves
to the next URL in `--urls-file` if there is one. A URL that fails
navigation after `--retries` is recorded in the run's `failed_pages` and
the run is `partial` (exit 6) if any Page in the same batch still
succeeded — never a crash that discards Pages already collected earlier
in the batch (CLAUDE.md §6).

## Engines

Playwright is primary; Selenium and pyppeteer are parity copies — all
three agree on exit codes and the `Product` schema via the shared
`output_writer.finish_run()`. Real, stated limits (identical to the rest
of the family's, since these are properties of the drivers, not the
site):

- **Selenium cannot use an authenticated remote CDP endpoint.**
  chromedriver's `debuggerAddress` takes a bare `host:port`; the Scraping
  Browser API's `ws://login:pass@host:port` shape needs an authenticated
  WebSocket upgrade, which only Playwright's `connect_over_cdp` and
  pyppeteer's `connect` support. `selenium_scraper.py` refuses a
  credentialed `--cdp-endpoint` outright (exit 2).
- **Selenium's `--proxy-server` cannot authenticate at all.** A `--proxy`
  with credentials has them stripped before reaching Chrome, with a loud
  warning — never a silent no-op.
- **pyppeteer is effectively unmaintained** (its own README points at
  Playwright) — shipped for parity, not as a recommendation.
- Install **exactly one** engine per environment — Playwright and
  pyppeteer declare mutually unsatisfiable `pyee` pins, and pyppeteer
  collides with Selenium's `urllib3` pin. Use a venv per engine, same as
  `.github/workflows/tests.yml`'s `engine-smoke` job.

## Known limitations

- **No selector on this site has been confirmed against a real page
  yet.** See "Read this before trusting a run" above — this is the
  single biggest gap this repo has, and the reason its own canary job
  (`.github/workflows/canary.yml`) is the most important signal to watch
  once this is actually deployed somewhere with real network access.
- **Live Q&A answers are explicitly out of scope**, not just unimplemented.
  Perplexity's interactive "Ask AI" feature on a Page is a live, per-visitor
  feature, not static published content, and `robots.txt` disallows the
  search/query paths that would be needed to reach it anyway. This repo
  only ever targets a Page's own already-published content.
- **No site-specific block-page marker exists.** `page_parser.
  BOT_CHALLENGE_MARKERS` is deliberately empty — detection still runs via
  `captcha_solver.GENERIC_BOT_CHALLENGE_MARKERS` (Cloudflare/reCAPTCHA/
  hCaptcha/PerimeterX/DataDome wording), but nothing specific to how
  perplexity.ai's own block page (if one exists) reads has been captured
  yet. If you hit one, `TESTING.md` explains how to add it.
- **Captcha token injection on a locally-launched browser is not
  implemented**, same reason as the rest of the family: injecting a
  solved token is widget/site-specific, and no real challenge from this
  site was ever available to verify an injector against. Over
  `--cdp-endpoint` (the Scraping Browser API), this doesn't matter —
  2Captcha's own `Captcha.setAutoSolve` CDP domain handles it entirely
  inside their infrastructure.

## Development

```bash
python3 smoke_test.py     # or: pytest tests/test_smoke.py
```

Passes with **no** engine library installed at all (each engine guards its
driver import behind a module-level `try/except ImportError`). Every HTML
fixture `smoke_test.py` uses is synthetic — there is no real-capture
fixture yet (unlike `lidl-scraper`'s `tests/fixtures/lidl_search_real.html`)
— see `smoke_test.py`'s own module docstring.

**Testing against the live site**: see [`TESTING.md`](TESTING.md). Every
item in it is currently open — this is the family's first repo to reach
this stage with zero live checks done.

## License

MIT — see `LICENSE`.
