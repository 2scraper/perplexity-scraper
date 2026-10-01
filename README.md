# perplexity-scraper

![release](https://img.shields.io/github/v/release/2scraper/perplexity-scraper?sort=semver)
![tests](https://github.com/2scraper/perplexity-scraper/actions/workflows/tests.yml/badge.svg)
![canary](https://github.com/2scraper/perplexity-scraper/actions/workflows/canary.yml/badge.svg)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![licence](https://img.shields.io/badge/licence-MIT-green)
![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer-informational)
![setup](https://img.shields.io/badge/setup-Browser%20API%20%28CDP%29-informational)

**Scrape [Perplexity](https://www.perplexity.ai/) articles into clean JSON
or CSV.** Give it a classic Page (`/page/...`), a Discover article
(`/discover/{topic}/...`), a file of such URLs, or just the Discover feed;
get back one row per article: title, author, summary, publish and update
times, view/like/fork counts, every cited source, section and word counts.

Three engines (Playwright primary, Puppeteer/pyppeteer and Selenium for
parity), an open, documented `Product` schema. Part of the
[2scraper](https://github.com/2scraper) family — same output contract,
exit codes and shared modules as `shein-scraper`, `lidl-scraper`,
`stockx-scraper` and `skyscanner-scraper`.

## Read this before trusting a run

**Live-verified 2026-09-30, re-run 2026-10-01.** Playwright and
Puppeteer, connected to a US 2Captcha Scraping Browser API profile over
`--cdp-endpoint`, scraped real articles end to end: a 2024 classic Page,
current Discover articles, a 25-article `--discover top` run (25/25 rows,
every applicable column filled), and a dead URL correctly reported as not
found. `status=complete`, exit `0` on every run, no paid captcha solves.
Run log: [`TESTING.md`](TESTING.md).

What the live capture established (details in `page_parser.py`):

- **The data comes from the site's own JSON API, not the HTML.** Every
  article page calls `GET /rest/article/{slug-or-uuid}`, which returns the
  whole article: title, summary, author, publish/update times, read time,
  view/like/fork counters, every section's text and sources, the hero
  image. The engines open the article page (so the browser holds the
  site's cookies) and call that same endpoint with `fetch()` from inside
  the page.
- **The HTML is never used as row data.** No JSON-LD; the Open Graph tags
  are the site-wide defaults on every page (`og:title` "Perplexity"); and
  a DEAD URL rendered the previously viewed article's full headings, with
  no article id anywhere in the HTML to tell them apart. A title read
  from the HTML could belong to another article, so when the API does not
  answer there is no row (the run says blocked or empty instead).
- **Both URL kinds work.** Old `/page/{slug}-{id}` links still resolve
  (the app rewrites them to `/page/{uuid}`); new articles live at
  `/discover/{topic}/{slug}-{id}`. The 22-character id is the stable
  `sku`: it stayed the same when a Page's slug changed. Ids can contain
  `.` and `_`.
- **Discover feed**: `/rest/discover/feed?topic=top` pages 20 at a time.
  The other topics (`tech`, `finance`, `arts`, `sports`, `entertainment`)
  returned zero items for an anonymous visitor.
- **Cloudflare blocks a local browser.** Local Chromium from a residential
  Mac, headless or headful, stayed on a managed challenge (Playwright,
  Selenium and pyppeteer alike); runs honestly exit `3`. Through a US
  residential proxy, a headful Selenium Chrome got the page but the API
  answered 403. **Use `--cdp-endpoint` for real work.**
- **The API throttles bursts.** 13 articles 0.5s apart, then 403 on every
  call for a few minutes. `--delay-between-pages` now defaults to 2s, and
  a refused API call is retried after 3, 5, 8 and 15s (a fresh profile
  also needs ~10s before its first call succeeds). At the defaults, 25/25
  on the same profile.

## Recommended setup

A 2Captcha Scraping Browser API profile over `--cdp-endpoint` (Playwright
or Puppeteer). Local runs are still supported and still report correctly,
but on 2026-09-30 Cloudflare did not let one through from the machine it
was tested on. Selenium cannot use the Browser API (see "Engines").

## Install

Pick one engine (installing more than one into the same environment is not
supported — see "Engines" below):

```bash
pip install -r requirements-playwright.txt && playwright install chromium   # primary
pip install -r requirements-selenium.txt                                    # needs a matching chromedriver
pip install -r requirements-puppeteer.txt                                   # pyppeteer — see its own warning below
```

Copy `.env.example` to `.env` and set `PERPLEXITY_CDP_ENDPOINT` (see
"Recommended setup"). `python3 env_config.py` shows what was picked up
without ever printing a secret.

## Usage

```bash
# one article (a classic Page or a Discover article)
python3 playwright_scraper.py --url "https://www.perplexity.ai/page/How-to-Generate-VzUTuvQVSIqru3QGvPihlg"

# the Discover feed, 40 articles, as CSV
python3 playwright_scraper.py --discover top --max-results 40 --format csv --out discover.csv

# a batch, one URL per line in a file
python3 playwright_scraper.py --urls-file pages.txt --max-results 20 --dump-html
```

Put the Scraping Browser API endpoint in `.env` as `PERPLEXITY_CDP_ENDPOINT`
(see "Recommended setup"); every command above then uses it.

`selenium_scraper.py` and `puppeteer_scraper.py` accept the identical flag
set and produce the identical output contract — see "Engines" for the two
places they genuinely can't behave the same as Playwright.

### Flags

`--url --urls-file --discover --max-results --delay-between-pages --format --out
--retries --retry-delay --proxy --proxy-file --proxy-shuffle
--proxy-block-retries --twocaptcha-key --captcha-api --solve-captcha
--max-solves --min-score --cdp-endpoint --fingerprint --fp-tags
--fp-country --allow-empty --dump-html --headless/--headful`

Identical across all three engines — a `smoke_test.py` check asserts the
three parsers' flag sets never drift apart. `--url` wins over
`--urls-file`, which wins over `--discover TOPIC`. `--max-results` caps how
many articles are fetched this run. A URL that is not an article
(`/page/...` or `/discover/{topic}/...` on perplexity.ai), or whose path is
disallowed by `robots.txt`, is skipped — logged, never fetched; input made
up entirely of such URLs is `EXIT_BAD_USAGE`. `--fingerprint`/`--fp-tags`/
`--fp-country` apply to all three engines on a locally launched browser
(ignored with `--cdp-endpoint`, which brings its own fingerprint): each
sets whatever user agent the 2Captcha Fingerprint API returns via its own
driver's real primitive (Playwright's `new_context(user_agent=...)`,
pyppeteer's `page.setUserAgent()`, Chrome's own `--user-agent=` switch
under Selenium) — see "What this repo deliberately does NOT apply from a
fingerprint" below. `--max-solves` caps paid 2Captcha solves for the whole
run (default 8, `0` = never pay; the sidecar records `solves_spent`). `--captcha-api`
overrides the 2Captcha REST base URL (testing only). `--min-score` is
2Captcha's own `minScore` field on a `RecaptchaV3Task` request (0.3
default, matching the rest of the family).

### Family flags that don't apply here — and why

- **`--query` / `--category` / `--sort` / `--zip` / `--store-id`**: this
  site has no search or commerce concept to attach any of these to — see
  "Read this before trusting a run" above. `--url`/`--urls-file` are this
  repo's actual, structurally different equivalent.
- **`--max-scrolls` / `--stall-rounds` / `--scroll-delay`**: every sibling
  repo's scroll/pagination loop exists because a search-results page
  keeps rendering more results as you scroll or click "load more". A
  single article is not paginated that way, and its data comes whole from
  one API call; `--delay-between-pages` is this repo's equivalent
  (politeness between fetches, and between Discover feed pages).
- **`--concurrency` / `--proxy-rotate`**: same reasoning as
  `skyscanner-scraper`/`lidl-scraper` — no independently-addressable units
  to parallelize or rotate an exit between within a single fetch.
  `proxy_pool.ProxyPool.worker_view()` is still kept verbatim from the
  shared family core and stays tested, for if a future feature (e.g.
  fetching a `--urls-file` batch concurrently) introduces an actual
  parallelizable unit.

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
author, view_count, like_count, fork_count, source_count, sources_json,
section_count, word_count, slug, summary, read_time_minutes, published_at, updated_at
```

`sku` is `perplexity-{id}`, the 22-character id at the end of the
article's canonical slug. `category` is the Discover topic (`top`) or
`null` for a classic Page. `brand`, `price`, `currency` and `price_source`
are always `null`: kept for the family's shared row prefix.
`sources_json` is every distinct cited URL across all sections, as a JSON
string (`[{"url", "title"}]`) so a CSV row stays flat. `summary` exists
for Discover articles only. `follow_up_question_count` was removed on
2026-09-30: nothing the site serves carries it.

Every row comes from the article API. `sample_output.json` / `sample_output.csv` are real rows from
the 2026-09-30 live run.

**Exit codes**: `0` complete · `1` crash · `2` bad usage · `3` blocked ·
`4` zero products (and nothing was written) · `5` remote API error · `6`
partial.

Every complete or partial run writes `<out>.meta.json` next to the
output: `status`, `stop_reason`, `pages_requested`, `pages_completed`,
`failed_pages`, `product_count`, `solves_spent`, `max_results`, `capped`,
`output_sha256` and timestamps (`price_confirmed_pct` is always `null`
here). A blocked, empty or remote-API-error run writes no output and no
sidecar, so it can never overwrite a previous good run. `--allow-empty`
lifts only the "don't write an empty result" half of that guard; rows
being present never turn a blocked run into "complete" (see
`output_writer.finish_run`).

## How one article is fetched

1. Open the article URL and wait for it to settle. If it is a Cloudflare
   challenge, wait up to 15s for it to clear by itself, then try the
   2Captcha solver when a key is set (`--solve-captcha`, within the run's
   `--max-solves` budget).
2. Call `/rest/article/{ref}` with `fetch()` from inside the page; on a
   refusal, retry after 3, 5, 8 and 15s.
3. `page_flow.decide()` turns what was seen into one outcome: an API row;
   not found (API 400, no row, not a block); blocked (a challenge that
   never cleared, or the API still refusing); or nothing read.

All of this is one loop, `page_flow.fetch_article()`, shared by the three
engines; each engine only supplies its driver's own way to open a page,
read its HTML and call `fetch()`.

A URL that fails navigation after `--retries` goes into `failed_pages` and
the run is `partial` (exit 6) if others succeeded. `--discover TOPIC`
first reads the feed (20 per page, by `offset`) up to `--max-results`
URLs, then fetches each article the same way.

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

- **Local runs are blocked by Cloudflare** in every test so far (see "Read
  this before trusting a run"). They exit `3`, never a fake success. Use
  `--cdp-endpoint`.
- **Only the `top` Discover topic has items** for an anonymous visitor.
  Other topic slugs return an empty feed, and the run reports zero
  products (exit 4) rather than guessing.
- **Counters can lag.** `view_count`/`like_count`/`fork_count` read `0` on
  articles published within the last day in the live capture.
- **Live Q&A answers are out of scope.** Only published articles are read.
- **Selenium cannot use the Browser API** and was only run locally, where
  Cloudflare or the API refused it.
- **One Browser API connection per profile.** Starting a second run right
  after the first can time out (60s, exit 5) while the provider releases
  the previous session; wait a little and rerun.
- **Solving Cloudflare's challenge page locally is not implemented.** Over
  `--cdp-endpoint` the Browser API handles Cloudflare itself.

## Development

```bash
python3 smoke_test.py     # or: pytest tests/test_smoke.py
```

Offline, no network, and passes with **no** engine library installed
(each engine guards its driver import behind `try/except ImportError`).
The parser checks run on real captures in `tests/fixtures/`: the article
API for a classic Page and a Discover article, the Discover feed (all
2026-09-30), and a real Cloudflare challenge page (2026-09-21). CI
(`.github/workflows/tests.yml`) also builds the wheel, the Docker image,
and runs the suite once per engine in its own venv.

**Testing against the live site**: see [`TESTING.md`](TESTING.md).

## License

MIT — see `LICENSE`.
