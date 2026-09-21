# Testing with real credentials and the live site

**Nothing below has been run live yet.** This repo's build environment
could not reach perplexity.ai directly (every automated shell available
hit a proxy-level 403) — every item here is open. Offline checks
(`smoke_test.py`) are useful for the architecture, but do not substitute
for the runs below; see README "Read this before trusting a run" for
what's confirmed from static research versus what's an unverified guess.

Run everything below from a normal terminal on your own machine — wherever
this repo lives for you.

## 1. Basic setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-playwright.txt
playwright install chromium
cp .env.example .env
```

Leave `.env` blank for the first run — the whole point of "local-first" is
that nothing in it is required. Fill in `TWOCAPTCHA_KEY` /
`PERPLEXITY_PROXY` / `PERPLEXITY_CDP_ENDPOINT` later, only if you want to
test those specifically.

## 2. The most important run you can do: check what a real Page looks like

This is the step nothing else in this repo could do for you. Pick any
real Page URL — the one found during this repo's research was
`https://www.perplexity.ai/page/ai-generated-images-tools-prom-efxu3L04SpufSVPD532HQg`,
but any real `/page/{slug}-{id}` URL you have works:

```bash
python3 playwright_scraper.py \
  --url "https://www.perplexity.ai/page/ai-generated-images-tools-prom-efxu3L04SpufSVPD532HQg" \
  --format json --out /tmp/perplexity_test.json --dump-html
echo "exit code: $?"
cat /tmp/perplexity_test.json.meta.json 2>/dev/null || echo "(no sidecar — see below)"
```

Four outcomes, and what each one means:

- **`exit code: 0`, a `.meta.json` with `"status": "complete"` and one
  product**: one of the three parsing paths (`json_ld` / `og_meta` /
  `dom` — check `sources_used`... actually check the row itself, since the
  sidecar doesn't carry `source_used` per-row; add a print if you need it)
  happened to match the real page. Open `/tmp/perplexity_test.json` and
  actually look at the row — a plausible `title`/`author`/`sources_json`
  is what "happened to match" looks like; a mostly-null row with only
  `title` filled in is what "matched the wrong shape" looks like even
  when the exit code says 0.
- **`exit code: 4` (zero products), no `.meta.json` written** (by design —
  see `output_writer.finish_run`): open `perplexity_test_debug_1.html` and
  check, in this order: (1) does the raw HTML contain an
  `application/ld+json` block at all, and if so what `@type` — confirms or
  refutes `page_parser._find_article_node`'s guessed type list; (2) does
  it carry `<meta property="og:title">`/`og:image` — confirms or refutes
  the OG-meta bet; (3) compare the actual rendered markup against
  `page_parser.py`'s `_TITLE_SELECTORS`/`_AUTHOR_SELECTORS`/etc. This is
  the expected first-run outcome if the selectors need updating — not
  evidence the Page itself is inaccessible.
- **`exit code: 3` (blocked)**: a `captcha_solver.GENERIC_BOT_CHALLENGE_
  MARKERS` hit, or an HTTP >=400 status. No incident like this has ever
  been captured for perplexity.ai — if you get one, this is genuinely new
  information: save a scrubbed capture, and add site-specific markers to
  `page_parser.BOT_CHALLENGE_MARKERS` the same way `skyscanner-scraper`'s
  PerimeterX incident did for that repo (see its CHANGELOG entry for the
  shape of that kind of entry).
- **A real Page renders but the sources/citations list or the two
  engagement counters don't parse even though the title does**: this is
  the single most likely partial-miss outcome given how little of
  `page_parser.py`'s DOM fallback is confirmed. Capture it and fix just
  those selectors — the title/author path being right doesn't mean the
  rest is.

Whatever you find, **updating `page_parser.py`'s selectors/heuristics to
match what you actually saw — with a saved, scrubbed fixture under
`tests/fixtures/` and a new `smoke_test.py` check against it — is the
single most valuable contribution this repo can receive** (see
`CONTRIBUTING.md`).

## 3. Selenium, for real

```bash
python3 -m venv .venv-selenium   # separate venv — see README "Engines"
source .venv-selenium/bin/activate
pip install -r requirements-selenium.txt
python3 selenium_scraper.py --url "https://www.perplexity.ai/page/..." --out /tmp/perplexity_selenium.json
```

## 4. Puppeteer (pyppeteer), for real

```bash
python3 -m venv .venv-puppeteer
source .venv-puppeteer/bin/activate
pip install -r requirements-puppeteer.txt
python3 puppeteer_scraper.py --url "https://www.perplexity.ai/page/..." --out /tmp/perplexity_puppeteer.json
```

## 5. The 2Captcha REST API, with your real key

Confirms the key and hits a real, billed-nothing endpoint first:

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

## 6. The Scraping Browser API (`--cdp-endpoint`), for real

`PERPLEXITY_CDP_ENDPOINT` in `.env` is picked up automatically:

```bash
python3 playwright_scraper.py --url "https://www.perplexity.ai/page/..." --out /tmp/perplexity_cdp.json
```

**Gotcha**, same as the rest of the family: if `.env` has BOTH
`PERPLEXITY_CDP_ENDPOINT` and `PERPLEXITY_PROXY` set, the code ignores
`PERPLEXITY_PROXY` and warns — a CDP session already carries its own exit
IP, stacking a second one on top is a contradiction, not better cover
(same for a fingerprint over `--cdp-endpoint`). Comment out whichever
you're not testing if you want to test them in isolation.

## 7. The residential proxy (`--proxy` / `PERPLEXITY_PROXY`), for real

```bash
python3 playwright_scraper.py --url "https://www.perplexity.ai/page/..." --out /tmp/perplexity_proxy.json
```

## 8. A real `--urls-file` batch, and the robots.txt skip

```bash
cat > /tmp/pages.txt <<'EOF'
https://www.perplexity.ai/page/some-real-page-one
https://www.perplexity.ai/page/some-real-page-two
https://www.perplexity.ai/search?q=this-should-be-skipped
EOF
python3 playwright_scraper.py --urls-file /tmp/pages.txt --out /tmp/perplexity_batch.json
```

Confirm the third line is logged as skipped (never actually requested —
see `page_parser.is_disallowed_path`) and the run still completes with two
products from the first two lines.

## 9. Push to GitHub and let CI do the rest

```bash
git remote add origin git@github.com:2scraper/perplexity-scraper.git
git push -u origin main
git push --tags
```

Then, in the GitHub repo's Settings:

- **Secrets and variables → Actions**: add `TWOCAPTCHA_KEY`,
  `PERPLEXITY_CDP_ENDPOINT` (only if you want the `canary-cdp` job using
  it — `canary-local` needs no secrets at all), and
  `CLAUDE_CODE_OAUTH_TOKEN` (for `claude.yml` / `claude-code-review.yml` —
  both silently no-op without it, by design, rather than failing every
  PR check). Optionally set the `PERPLEXITY_CANARY_URL` repo/org
  **variable** (not a secret — it's just a URL) to a Page you control, so
  `canary.yml` doesn't depend on someone else's Page staying unedited
  forever — see that workflow's own comments.
- **Actions → canary → Run workflow**: dispatch it manually at least once
  rather than waiting a day for the cron and trusting the badge blind —
  this is this repo's actual FIRST live test, so look at the run's log and
  uploaded artifact, not just the badge color.

## 10. What "done" looks like

- `tests.yml` green on both Python versions and all three `engine-smoke`
  matrix legs.
- At least one manually-dispatched `canary.yml` run, looked at — not just
  the badge — including whichever of the outcomes in step 2 above it
  landed on.
- `page_parser.py` updated to match what you actually saw, with a fixture
  under `tests/fixtures/` and a new `smoke_test.py` check, per
  `CONTRIBUTING.md` — this repo has no real-capture fixture at all yet,
  unlike `lidl-scraper`'s `tests/fixtures/lidl_search_real.html`.
- Whether perplexity.ai emits any JSON-LD on a Page, and whether OG meta
  tags are actually server-rendered there, answered one way or the other
  (see README "Read this before trusting a run") and reflected in
  `page_parser.py`'s module docstring.
