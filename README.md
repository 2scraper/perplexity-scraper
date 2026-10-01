# perplexity-scraper

![release](https://img.shields.io/github/v/release/2scraper/perplexity-scraper?sort=semver)
![tests](https://github.com/2scraper/perplexity-scraper/actions/workflows/tests.yml/badge.svg)
![canary](https://github.com/2scraper/perplexity-scraper/actions/workflows/canary.yml/badge.svg)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![licence](https://img.shields.io/badge/licence-MIT-green)
![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer-informational)

**Scrape Perplexity articles into clean JSON or CSV.** Give it an article
URL, a file of URLs, or just the Discover feed; get back one row per
article: title, author, summary, publish and update times, view/like/fork
counts, every cited source, section and word counts.

It reads both kinds of public articles on
[perplexity.ai](https://www.perplexity.ai): classic Pages (`/page/...`)
and Discover articles (`/discover/{topic}/...`).

- **Real data, not scraped markup.** Every row comes from the site's own
  article API (`/rest/article/`), called from inside the article page.
  The HTML is never used: its meta tags are the same on every page.
- **Verified live.** On 2026-09-30 and 2026-10-01, over a US Scraping
  Browser API profile: classic Pages, Discover articles, 25 of 25 on a
  Discover run, a dead URL reported as not found. No paid captcha solves.
- **Honest results.** A blocked, throttled or partial run says so in its
  exit code and a `.meta.json` file next to the output. A run that finds
  nothing never overwrites your last good data.
- **Three browser engines** (Playwright, Puppeteer, Selenium) running one
  shared fetch loop, 2Captcha's **Scraping Browser API** over CDP, a
  browserless **Scraper API** mode, rotating proxies, and a run-to-run
  diff tool.

## Quick start

```bash
git clone https://github.com/2scraper/perplexity-scraper.git
cd perplexity-scraper
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-playwright.txt
playwright install chromium
cp .env.example .env        # put PERPLEXITY_CDP_ENDPOINT here, never on the command line

python3 playwright_scraper.py --discover top --max-results 20
```

Results land in `perplexity_results.json`, with
`perplexity_results.json.meta.json` beside it.

**Recommended setup:** a 2Captcha Scraping Browser API profile (US) in
`PERPLEXITY_CDP_ENDPOINT`. Cloudflare kept every local browser tried on
its challenge page; the Browser API gets through.
`python3 env_config.py` shows what was picked up, without printing
secrets.

**No browser at all:** add `TWOCAPTCHA_KEY` and pass `--scraper-api`.
Each article is then one 2Captcha Scraper API call routed through the
same profile; nothing has to be installed beyond `requirements.txt`.

## Examples

```bash
# the Discover feed, 40 articles, as CSV
python3 playwright_scraper.py --discover top --max-results 40 --format csv --out discover.csv

# one article: a classic Page or a Discover article
python3 playwright_scraper.py --url "https://www.perplexity.ai/page/ai-generated-images-tools-prom-efxu3L04SpufSVPD532HQg"

# a batch, one URL per line (lines starting with # are skipped)
python3 playwright_scraper.py --urls-file articles.txt

# the same with Puppeteer
python3 puppeteer_scraper.py --discover top --max-results 20

# without a browser: 2Captcha's Scraper API through the same profile
python3 playwright_scraper.py --scraper-api --discover top --max-results 20

# compare two runs
python3 diff_runs.py monday.json tuesday.json
```

## Sample output

A real row from a run on 2026-09-30 (`sample_output.json` has two;
`sources_json` shortened here):

```json
{
  "sku": "perplexity-heYaECNnQuaM0AZ0QSWjaw",
  "source": "perplexity.ai",
  "category": "top",
  "title": "OpenAI unveils Dots, an always-on AI agent, at DevDay 2026",
  "brand": null,
  "price": null,
  "currency": null,
  "price_source": null,
  "product_url": "https://www.perplexity.ai/discover/top/openai-unveils-dots-an-always-heYaECNnQuaM0AZ0QSWjaw",
  "image_url": "https://pplx-res.cloudinary.com/image/fetch/s--ujP6_Ewb--/t_limit/https://static.simonwillison.net/static/2026/live-20260929-100334.webp",
  "scraped_at": "2026-09-30T08:35:03Z",
  "author": "pagesandbits",
  "view_count": 0,
  "like_count": 0,
  "fork_count": 0,
  "source_count": 6,
  "sources_json": "[{\"url\": \"https://openai.com/index/introducing-dots/\", \"title\": \"Introducing dots\"}, ...]",
  "section_count": 4,
  "word_count": 380,
  "slug": "openai-unveils-dots-an-always-heYaECNnQuaM0AZ0QSWjaw",
  "summary": "Powered by GPT-6 Astra, each Dot gets its own cloud computer and can connect to over 4,000 apps to handle tasks around the clock.",
  "read_time_minutes": 3,
  "published_at": "2026-09-29T17:14:10.246772+00:00",
  "updated_at": "2026-09-29T17:15:35.292639"
}
```

| Field | Meaning |
|---|---|
| `sku` | `perplexity-` + the article's 22-character id; stays the same when the slug changes |
| `category` | the Discover topic (`top`); `null` for a classic Page |
| `product_url` / `slug` | the article's canonical URL and its last path segment |
| `view_count` / `like_count` / `fork_count` | the site's counters; they can read `0` for a day after publishing |
| `sources_json` | every distinct cited source across all sections, as a JSON string of `{"url", "title"}` so a CSV row stays flat |
| `source_count` / `section_count` / `word_count` | counts over the whole article |
| `summary` | Discover articles only; `null` for a classic Page |
| `read_time_minutes`, `published_at`, `updated_at` | as the site reports them |
| `brand`, `price`, `currency`, `price_source` | always `null`: articles have none, the columns keep the shared row layout |

## How an article is fetched

1. Open the article page, so the browser holds the site's cookies. If
   Cloudflare shows a challenge, wait up to 15s for it to clear by
   itself, then try 2Captcha's solver if a key is set (`--solve-captcha`,
   within `--max-solves`).
2. Call `/rest/article/{id}` with `fetch()` from inside the page. A
   refused call is retried after 3, 5, 8 and 15s: a fresh profile's first
   call is often refused, and the API throttles bursts.
3. Turn the answer into one outcome: a row; **not found** (API 400 — no
   row, not a block); **blocked** (the challenge never cleared, or the
   API kept refusing); or nothing read.

`--discover TOPIC` first reads the feed, 20 articles per page, up to
`--max-results`, then fetches each article this way, `--delay-between-pages`
apart (2s; at 0.5s the API throttled after 13 articles).

## Scraper API mode

With `--scraper-api`, no browser is driven: the feed and every
`/rest/article/` call go through 2Captcha's Scraper API, routed through
the Scraping Browser profile in `PERPLEXITY_CDP_ENDPOINT`. Step 1 above is
skipped; retries, parsing, exit codes and the `.meta.json` are the same
(`engine: scraper_api`). It needs both `TWOCAPTCHA_KEY` and
`PERPLEXITY_CDP_ENDPOINT`, and works with any of the three scripts, none
of their drivers required.

Measured on 2026-10-01: through the profile, a 10-article Discover run
was complete in 71s; on the Scraper API's own pool every request got
Cloudflare's challenge, which is why the profile is required. A refused
key or an empty balance stops the run at once with exit 5.
`--proxy`, `--fingerprint` and `--dump-html` do not apply.

Both modes share the profile's rate limit: right after about 20 Scraper
API calls, a browser run on the same profile got API 403 for a few
minutes, then worked again.

## Run results and exit codes

Every run writes `<out>` and `<out>.meta.json` (status, `stop_reason`,
URLs requested and completed, failed URLs, article count, whether
`--max-results` capped it, solves spent, and a hash of the output file).
A run that collects nothing writes neither, so it never replaces your
previous good file.

| Exit | Meaning |
|---|---|
| `0` | complete |
| `6` | partial: rows were written, but the run did not finish cleanly — `stop_reason` says why (`blocked`, `rate_limited`, `remote_api_error`, `failed_pages`, `rejected_rows`) |
| `3` | blocked or throttled, no rows |
| `4` | nothing read and nothing blocked (e.g. a Discover topic with no items) |
| `5` | a remote service failed (the Scraping Browser connection, the Scraper API, the 2Captcha key or balance), no rows |
| `2` | bad usage, including input where every URL was skipped |
| `1` | crash (a bug; please report it) |

## Comparing runs

`diff_runs.py old.json new.json` compares two complete runs by `sku`:
articles added and removed. It refuses comparisons that would mislead:
two runs of different selections, or a `.meta.json` that does not match
its file. When a run was capped by `--max-results`, an article missing
from the new run is listed as `left_selection` (it dropped out of the
top N), not as removed. `--json` prints the diff as JSON.

## Options

Same flags for all three engines. Credentials go in `.env`
(`PERPLEXITY_CDP_ENDPOINT`, `TWOCAPTCHA_KEY`, `PERPLEXITY_PROXY`), never on
the command line.

| Option | Default | |
|---|---|---|
| `--url` / `--urls-file` / `--discover TOPIC` | | what to scrape, in that order of precedence; only `top` has Discover items for an anonymous visitor |
| `--max-results` | 30 | articles to fetch |
| `--delay-between-pages` | 2s | pause between articles and between feed pages |
| `--format` / `--out` | json / `perplexity_results.<format>` | output format and path |
| `--cdp-endpoint` | `PERPLEXITY_CDP_ENDPOINT` | connect to a Scraping Browser API profile instead of launching a browser |
| `--scraper-api` | off | no browser: fetch through 2Captcha's Scraper API, routed through that profile (needs `TWOCAPTCHA_KEY`) |
| `--proxy` / `--proxy-file` / `--proxy-shuffle` | `PERPLEXITY_PROXY` | one proxy or a rotating pool, for a local browser |
| `--proxy-block-retries` | 3 | proxy failures in a row before that proxy is dropped from the pool |
| `--solve-captcha` | when-blocked | `off` / `when-blocked` / `always` |
| `--max-solves` | 8 | paid 2Captcha solves per run (0 = never pay) |
| `--min-score` | 0.3 | minimum reCAPTCHA v3 score asked of 2Captcha |
| `--retries` / `--retry-delay` | 2 / 3s | navigation retries per article |
| `--fingerprint` / `--fp-tags` / `--fp-country` | off | apply a 2Captcha Fingerprint API user agent (local browsers only) |
| `--dump-html` | off | save each page's HTML next to the output, for debugging |
| `--allow-empty` | off | write an output file even when nothing was found |
| `--headless` / `--headful` | headless | show the browser window |

A URL that is not an article, or whose path `robots.txt` disallows, is
logged and skipped, never requested. Run any engine with `--help` for the
full list.

## Engines

- **Playwright** (`playwright_scraper.py`) is the recommended engine,
  with the Scraping Browser API or a local Chromium.
- **Puppeteer** (`puppeteer_scraper.py`, via pyppeteer) supports the same
  two modes and was verified live the same way. pyppeteer itself is no
  longer maintained.
- **Selenium** (`selenium_scraper.py`) runs a local Chrome only.
  chromedriver cannot authenticate a Scraping Browser endpoint (the run
  exits 2 before fetching; use `--scraper-api` instead), and its
  `--proxy-server` cannot use a proxy password (the credentials are
  stripped, with a warning).

Install one engine per virtualenv (`requirements-playwright.txt`,
`requirements-puppeteer.txt`, `requirements-selenium.txt`). Their
dependencies conflict with each other.

All three engines share one fetch loop (`page_flow.py`), so they agree on
results, exit codes and when money is spent. Docker:
`docker build -t perplexity-scraper .` gives an image with Playwright and
Chromium.

## Known limitations

- **Local browsers are blocked by Cloudflare** in every test so far; the
  run exits 3. Use `--cdp-endpoint`.
- **Only the `top` Discover topic has items** for an anonymous visitor.
  Other topics return an empty feed (exit 4).
- **The API throttles bursts.** Keep `--delay-between-pages` at 2s or
  more; a throttled run reports `blocked` or `rate_limited`.
- **One Browser API connection per profile.** A second run started right
  after the first can time out (exit 5) while the previous session is
  released; wait a little and rerun.
- **Published articles only.** Perplexity's live answers, accounts and
  anything behind a login are out of scope.

## Development

```bash
python3 smoke_test.py            # 60 offline checks, no network, no engine needed
python3 .github/ci_checks.py     # credential scan
```

Parser checks run on real captures in `tests/fixtures/`. CI runs the
offline suite on Python 3.9 and 3.12, installs the built wheel outside
the checkout, builds the Docker image and launches Chromium in it, and
runs each engine in its own virtualenv. `TESTING.md` describes live
testing with real credentials; `CHANGELOG.md` has the history.

## Licence

MIT, see `LICENSE`.
