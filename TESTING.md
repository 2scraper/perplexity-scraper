# Testing with real credentials and the live site

**Live status, 2026-09-30.** Over a US Scraping Browser API profile
(`PERPLEXITY_CDP_ENDPOINT`):

| Engine | Run | Result |
|---|---|---|
| Playwright | 3 URLs (old Page, Discover article, dead URL) | 2 full rows, dead URL `page_not_found`, exit 0 |
| Playwright | `--discover top --max-results 25` | 25/25 rows, every applicable column filled, exit 0 |
| Puppeteer | the same 3 URLs; `--discover top --max-results 5` | same rows, exit 0 |
| Playwright, local headless and headful | 1-2 URLs | Cloudflare challenge never cleared → exit 3 |
| Selenium, local headless | 1 URL | Cloudflare challenge → exit 3 |
| Selenium, headful via US residential proxy | 3 URLs | page served, API 403 (HTML-only rows at the time; the HTML path was since removed) |
| Playwright + Puppeteer, a second fresh profile | 3 URLs; Discover 5 and 25 (default 2s delay) | all complete, 25/25; at 0.5s delay the API throttled after 13 |

**Re-run 2026-10-01**, after the engines moved onto the one shared fetch
loop (`page_flow.fetch_article`), over a US Browser API profile:

| Engine | Run | Result |
|---|---|---|
| Playwright | 2 URLs (old Page, dead URL) | 1 full row, dead URL `page_not_found`, exit 0, 0 solves |
| Playwright | `--discover top --max-results 25` | 25/25 rows, exit 0 |
| Puppeteer | the same 2 URLs; `--discover top --max-results 5` | same row; 5/5, exit 0 |
| Selenium | `--cdp-endpoint` with credentials | refused up front, exit 2 (by design: chromedriver cannot authenticate) |
| Playwright, local headless | 1 URL; `--discover top` | Cloudflare challenge → exit 3; Discover feed 403 |

Still open: a local run that Cloudflare lets through, and Selenium
against the article API.

The quickest real check:

```bash
python3 playwright_scraper.py --discover top --max-results 5 --out /tmp/pplx.json
cat /tmp/pplx.json.meta.json        # status: complete, product_count: 5
```

"article API HTTP 403 — retrying" is normal on a fresh profile's first
article. If it ends in "blocked", the API is throttling: raise
`--delay-between-pages`. "served a bot challenge that did not clear"
means Cloudflare blocked this browser; switch to `--cdp-endpoint`.


## 1. Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-playwright.txt
playwright install chromium
cp .env.example .env
```

Put a US Scraping Browser API connection string in `.env` as
`PERPLEXITY_CDP_ENDPOINT` (format in `.env.example`). `TWOCAPTCHA_KEY` and
`PERPLEXITY_PROXY` are needed only for steps 5 and 7.
`python3 env_config.py` shows what was picked up without printing a
secret.

## 2. One article, and what the row should look like

```bash
python3 playwright_scraper.py \
  --url "https://www.perplexity.ai/page/ai-generated-images-tools-prom-efxu3L04SpufSVPD532HQg" \
  --out /tmp/perplexity_test.json
echo "exit code: $?"
cat /tmp/perplexity_test.json.meta.json
```

- **Exit `0`, `"status": "complete"`, one row**: open the JSON. A real
  row has `title`, `author`, `published_at`, `section_count`,
  `word_count` and a non-empty `sources_json`; `brand`/`price`/`currency`/
  `price_source` are always `null`. Compare it with `sample_output.json`.
- **Exit `3` (blocked)**: either Cloudflare kept the challenge up ("served
  a bot challenge that did not clear" — expected without
  `--cdp-endpoint`), or the article API kept answering 401/403/429 after
  every retry (throttling — wait a few minutes, raise
  `--delay-between-pages`). Add `--dump-html` and compare the page with
  `tests/fixtures/perplexity_cloudflare_block_real.html`. A different
  challenge is new information: save a scrubbed capture next to it and
  extend `page_parser.BOT_CHALLENGE_MARKERS`.
- **Exit `4` (zero products, nothing written)**: nothing was read and
  nothing looked like a block — the logged warning says which URL and
  why; with `--discover`, usually a topic with no items.
- **A row with fields that look wrong**: the API's shape changed. Save the
  `/rest/article/` response under `tests/fixtures/`, update
  `page_parser.parse_article_json()` and add a `smoke_test.py` check (see
  `CONTRIBUTING.md`).

A dead article id answers API 400: logged as `page_not_found`, no row,
not counted as a block.

## 3. Puppeteer (pyppeteer)

```bash
python3 -m venv .venv-puppeteer       # separate venv — see README "Engines"
source .venv-puppeteer/bin/activate
pip install -r requirements-puppeteer.txt
python3 puppeteer_scraper.py --discover top --max-results 5 --out /tmp/perplexity_puppeteer.json
```

Same `.env`, same output and exit codes as Playwright.

## 4. Selenium

```bash
python3 -m venv .venv-selenium
source .venv-selenium/bin/activate
pip install -r requirements-selenium.txt
python3 selenium_scraper.py --url "https://www.perplexity.ai/page/..." --out /tmp/perplexity_selenium.json
```

Selenium cannot use the Browser API: with a credentialed
`PERPLEXITY_CDP_ENDPOINT` it exits `2` before fetching anything. Comment
the variable out to test it with a local browser (expect Cloudflare,
exit `3`) or with step 7's proxy.

## 5. The 2Captcha REST API, with your real key

Confirms the key on an endpoint that bills nothing:

```bash
python3 -c "
import env_config
from scraper_api_client import TwoCaptchaClient
args = type('A', (), {'twocaptcha_key': None, 'proxy': None, 'cdp_endpoint': None, 'url': None})()
env_config.apply_env(args)
c = TwoCaptchaClient(args.twocaptcha_key)
print('balance: \$%.2f' % c.get_balance())
"
```

## 6. Proxy and Browser API together

If `.env` has BOTH `PERPLEXITY_CDP_ENDPOINT` and `PERPLEXITY_PROXY`, the
proxy is ignored with a warning: a CDP session already carries its own
exit IP (the same goes for `--fingerprint`). Comment out whichever one you
are not testing.

## 7. The residential proxy (`--proxy` / `PERPLEXITY_PROXY`)

With `PERPLEXITY_CDP_ENDPOINT` commented out:

```bash
python3 playwright_scraper.py --url "https://www.perplexity.ai/page/..." --out /tmp/perplexity_proxy.json
```

On 2026-09-30 a US residential proxy got the page but the article API
answered 403; a run that gets rows this way is worth recording above.

## 8. A `--urls-file` batch, and the robots.txt skip

```bash
cat > /tmp/pages.txt <<'URLS'
https://www.perplexity.ai/page/ai-generated-images-tools-prom-efxu3L04SpufSVPD532HQg
https://www.perplexity.ai/page/a-0000000000000000000000
https://www.perplexity.ai/search?q=this-should-be-skipped
URLS
python3 playwright_scraper.py --urls-file /tmp/pages.txt --out /tmp/perplexity_batch.json
```

Expect: the `/search` line logged as skipped and never requested
(`page_parser.is_disallowed_path`), the second line `page_not_found`, and
a `complete` run with one row.

## 9. Push to GitHub and let CI do the rest

```bash
git remote add origin git@github.com:2scraper/perplexity-scraper.git
git push -u origin main
```

Then, in the GitHub repo's Settings → **Secrets and variables → Actions**:

- `PERPLEXITY_CDP_ENDPOINT` — the canary SKIPS without it (there is no
  local-browser canary: Cloudflare blocks local browsers).
- `TWOCAPTCHA_KEY` — optional for the canary.
- `CLAUDE_CODE_OAUTH_TOKEN` — for `claude.yml` / `claude-code-review.yml`;
  both no-op without it rather than failing every PR.
- Optionally the `PERPLEXITY_CANARY_URL` **variable** (not a secret) — a
  Page you control, so the canary does not depend on someone else's Page
  staying up.

Run **Actions → canary → Run workflow** once by hand and read its log and
uploaded artifact, not just the badge.

## 10. What "done" looks like

- `tests.yml` green: `offline` on both Python versions, `docker`, and all
  three `engine-smoke` legs.
- At least one manually dispatched `canary.yml` run, looked at.
- Steps 2-3 reproduce the table at the top on your own profile.
