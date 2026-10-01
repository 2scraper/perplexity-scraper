# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/) as closely as a CLI toolkit can manage. A patch
release means "fixes", not that every flag and default is frozen — a
behaviour-changing default gets called out explicitly in its entry below
rather than being a silent violation of that.

## [Unreleased]

### Added — 2026-10-01, `--scraper-api`: no browser, 2Captcha's Scraper API
- New `scraper_api_engine.py`, a `page_flow` engine with no page: the
  Discover feed and every `/rest/article/` call are one Scraper API call
  each, with `cdpurl` set to `PERPLEXITY_CDP_ENDPOINT`. Retries, parsing,
  exit codes and the sidecar are the shared ones (`engine: scraper_api`).
  All three scripts take `--scraper-api`; none of their drivers is
  needed, and Selenium can use it despite its CDP limitation.
- Measured live: on its own pool the Scraper API got Cloudflare's
  challenge (target 403) for the page, the article API and the feed;
  through a US profile the JSON came back, 10/10 on a Discover run.
- A refused key or empty balance stops further calls and ends the run as
  `remote_api_error` (exit 5); `page_flow.run` now reports that for any
  engine whose last call failed on the service's side.
- `scraper_api_client.scrape_url`: the `waitFor` example was
  `networkidle`, which the API refuses (422); it is `load`.
- `smoke_test.py`: 58 → 60 checks.

### Changed — 2026-10-01, one shared fetch loop; docs brought up to date
- **One fetch loop.** Navigate, wait out a Cloudflare challenge, solve
  within budget, read `/rest/article/` with retries: this was copied into
  each of the three engines. It now lives once in
  `page_flow.fetch_article()`; each engine supplies an `Engine` whose
  page sessions expose `goto`, `content`, `fetch_json`, `wait` and
  `close`, so no JavaScript crosses the boundary. `resolve_urls()` is
  shared too.
- **`--max-solves`** (default 8): one cap on PAID captcha solves for the
  whole run (`0` = never pay), recorded in the sidecar as
  `solves_spent`.
- CI: the Docker image (build, entrypoint, a real Chromium launch, no
  `.env`/tests/fixtures inside) is its own `docker` job.
- Docs: README, TESTING.md, CONTRIBUTING.md, SECURITY.md and the landing
  page rewritten where they still described the first build's HTML
  parsing (JSON-LD / Open Graph / DOM), "local-first" and "no live
  capture yet". `scraper_api_client.py` no longer names
  `--scraper-api*` flags this repo never had.
- Re-verified live over a US Browser API profile: Playwright and
  Puppeteer, 2 URLs and Discover (25 and 5), all `complete`, 0 solves
  (TESTING.md).

### Changed — 2026-09-30, shared core synced with shein-scraper
- `output_writer`: rows plus an unfinished run is `partial` (exit 6) with
  a `stop_reason`; the sidecar gains `max_results`, `capped`,
  `total_results` and `output_sha256`.
- `diff_runs`: refuses to compare different selections, adds a
  `currency_changed` bucket, checks the output hash.
- `captcha_solver` and `scraper_api_client` match the family core,
  including `connect_with_retry` for an expiring CDP endpoint.
- `.github/ci_checks.py`: the credential scan and `--sample-check`; CI
  imports the built wheel outside the checkout.
- `.gitignore`: `.env*` (except `.env.example`), `live/`, `.venv-*/`.

### Changed — 2026-09-30, canary on the Browser API; honest setup badge
- `canary.yml`: the local-browser job is removed. Cloudflare blocked every
  local browser tried, so it could only ever be red. The remaining job
  runs over `PERPLEXITY_CDP_ENDPOINT`: `--discover top --max-results 5`
  (>= 3 rows) plus one known Page, and requires `complete` with rows only
  the article API fills (sections, sources, `published_at`). It skips
  with a notice when the secret is absent. The check script was dry-run
  against real outputs from the live runs: pass on a good run, red on
  exit 3.
- README: the `local-first: yes` badge became `setup: Browser API (CDP)`.

### Changed — 2026-09-30, second live profile: no rows from HTML, API retry, slower default pace
- **The HTML fallback is gone.** A dead URL (API 400) rendered the
  previously viewed article's full title and sections, and no article id
  appears in the HTML, so the earlier run wrote a row for a nonexistent
  article carrying another article's title. With no API answer there is
  now no row: blocked on 401/403/429, otherwise "no article read".
  `_parse_page_from_dom` and `extract_og_meta` are removed.
- **API retry**: a fresh profile's first `/rest/article/` call got 403
  (~3s after load) and 200 at ~12s; a burst of 13 articles 0.5s apart got
  403 on every later call. All three engines retry a refused call after
  3, 5, 8 and 15s (`page_flow.API_RETRY_DELAYS_S`).
- `--delay-between-pages` defaults to 2s (was 1s). At the defaults, the
  profile that throttled at 0.5s did `--discover top --max-results 25`
  25/25.
- Verified live on that profile: Playwright and Puppeteer on the 3-URL
  set (2 rows plus `page_not_found`) and Discover (5 and 25), all
  `complete`.

### Changed — 2026-09-30, rebuilt on the first live capture of real articles
- Recon through a US Scraping Browser API profile over CDP (HTTP 200, no
  Cloudflare challenge) replaced every guessed selector with confirmed
  facts. **The article data comes from the site's own JSON endpoint
  `/rest/article/{slug-or-uuid}`**; the HTML carries no JSON-LD, and its
  Open Graph tags are the site-wide defaults on every page.
- `page_parser.py` rewritten. `parse_article_json()` is the primary path
  (title, author, summary, read time, publish/update times,
  view/like/fork counters, sections, words, deduped sources, hero image).
  `parse_discover_feed()` reads the Discover feed. The HTML fallback reads
  only the title and section headings.
- **Both URL kinds**: classic `/page/{slug}-{id}` (still resolves; creating
  new Pages is retired) and `/discover/{topic}/{slug}-{id}`, where new
  articles live. `sku` is the 22-character id, which survived a live slug
  change.
- **Bugs the live data exposed**:
  - ids containing `.` (`.FiJwwm9STi9_gZ5rgyhXQ`) failed the old
    `[A-Za-z0-9_-]` id regex;
  - the OG path would have titled EVERY article "Perplexity" with the
    site root as its URL (default OG tags);
  - on a local run, the DOM fallback turned Cloudflare's own
    "Performing security verification" heading into an article and
    reported exit 0. The fallback now requires the app's own asset host
    (`pplx-next-static-public`: 195 hits per real page, 0 on both
    challenge captures).
- New `page_flow.py`: one pure `decide()` for all three engines — API row,
  degraded HTML row, not found (API 400: no row, not blocked), blocked
  (challenge that never cleared; HTTP 4xx with nothing usable).
- All three engines: open the article, wait up to 15s for a Cloudflare
  challenge to clear, then `fetch()` `/rest/article/` from inside the page
  with its cookies. `--discover TOPIC` reads the feed, 20 per page, up to
  `--max-results`. English locale pinned: Perplexity translated a title to
  a Russian-locale browser's language live.
- Playwright over CDP reuses the profile's default context (keeps its
  cookies), as shein-scraper needed.
- pyppeteer: `connect()` is now bounded (60s). Live, a Browser API
  handshake refused with 401 hung the run forever; it now exits 5.
- pyppeteer: over CDP, one connection per run is DISCONNECTED, never
  closed (`close()` ended the remote session). `asyncio.run()` replaces
  `get_event_loop()`. Its "Target closed" / "No session with given id"
  futures no longer log as ERROR on every page close.
- `Product`: removed `follow_up_question_count` (nothing the site serves
  carries it — CLAUDE.md §9); added `like_count`, `fork_count`, `summary`,
  `read_time_minutes`, `updated_at`. `category` is the Discover topic.
- Real API captures committed as fixtures
  (`tests/fixtures/*_live_20260930.json`); `sample_output.*` are real rows.
  `smoke_test.py`: 50 → 51 checks, the parser ones rebuilt on those
  fixtures.
- Live results: see TESTING.md's table. Cloudflare blocked every local
  browser tried; `--cdp-endpoint` is now the recommended setup.

### Added — 2026-09-21, first live incident: a real Cloudflare managed challenge
- **First live data point ever captured for this site, and it's a
  block.** A full browser-rendering tool (not a `curl`-style shell — see
  `TESTING.md` for that distinction) made two separate, freshly-opened
  requests against two different real `https://www.perplexity.ai/page/
  {slug}-{id}` URLs. Both got redirected to the bare origin and served the
  identical Cloudflare "managed challenge" interstitial (`cType:
  'managed'`, Ray ID `a3e80852cfb8ae37`) instead of any Page content —
  ruling out a one-off fluke or a URL-specific block.
- Verified `captcha_solver.detect_from_html()` against the actual
  captured HTML: returns `True` (three generic markers hit: `"cf-turnstile"`,
  `"challenges.cloudflare.com"`, `"cdn-cgi/challenge-platform"`) — this
  repo's existing block detection would correctly report exit `3`
  (`blocked`) on this exact page, not misreport it as `4` (`empty`).
- Added `page_parser.BOT_CHALLENGE_MARKERS` (`"cf-chl-widget"`,
  `"_cf_chl_opt"`) as durable, site-specific corroboration of the same
  incident, mirroring `skyscanner-scraper`'s PerimeterX precedent — not a
  replacement for the generic detector, which already caught this on its
  own.
- Saved the captured page as `tests/fixtures/perplexity_cloudflare_block_real.html`
  (trimmed for size, nothing sensitive removed — it's Cloudflare's own
  generic challenge page, not any perplexity.ai content or account data)
  and added a `smoke_test.py` check asserting both the site-specific
  markers and the generic detector correctly flag it.
- Updated `page_parser.py`'s module docstring, `README.md`'s "Read this
  before trusting a run" and "Known limitations" sections, and
  `TESTING.md`'s step 2 to reflect this as confirmed, not hypothetical.
- **What this does NOT confirm, to be precise about the boundary**:
  whether this repo's own engines (`playwright_scraper.py`/
  `selenium_scraper.py`/`puppeteer_scraper.py`) get the same treatment —
  the capture came from a different client than a real headless
  Playwright run, and a Cloudflare "managed" challenge can pass silently
  for a client Cloudflare trusts. No selector in `page_parser.py` is any
  more or less confirmed than before — this was a block page, not a
  results page. The first real run of this repo's own engines against
  this site, and the question of whether `--proxy`/`--cdp-endpoint`/
  `--fingerprint` end up being necessary defaults rather than opt-in power
  options for this specific site, both remain open — see `TESTING.md`.

### Added — 2026-09-21, initial build
- First build of `perplexity-scraper`, the fourth member of the
  [2scraper](https://github.com/2scraper) family (after `stockx-scraper`,
  `skyscanner-scraper`, `lidl-scraper`), targeting
  [Perplexity Pages](https://www.perplexity.ai/) — public, wiki-style
  articles at `https://www.perplexity.ai/page/{slug}-{id}`.
- **Genuine CLI divergence from every sibling repo (CLAUDE.md §1),
  documented rather than silent**: this repo takes `--url`/`--urls-file`
  instead of `--query`/`--category`. Perplexity Pages have no site-search
  mechanism to point a query at — `robots.txt` explicitly disallows
  `/*?*q=` and `/search*` for every crawler, and no `?q=`-style endpoint
  against perplexity.ai itself surfaces Pages. A Page is reachable only
  via its own specific URL. See `page_parser.py`'s module docstring for
  the full research this is based on.
- Ported the family-shared, no-site-knowledge modules near-verbatim from
  `lidl-scraper` (CLAUDE.md §7): `output_writer.py` (exit codes,
  `STATUS_BY_EXIT`, `finish_run()` precedence — unchanged), `proxy_pool.py`,
  `captcha_solver.py`, `fingerprint_client.py`, `scraper_api_client.py`,
  `diff_runs.py`, `env_config.py` (renamed to `PERPLEXITY_*` env keys).
- **`output_writer.Product`'s schema divergence, also documented rather
  than silent**: `category`/`brand`/`price`/`currency`/`price_source` are
  always `null` — a Page is a free wiki article, not a commerce listing —
  kept for schema parity across the family instead of dropped, the same
  way `skyscanner-scraper` repurposes `brand` for "operating airline"
  rather than leaving the shared field order behind. New site-specific
  fields: `author`, `view_count`, `follow_up_question_count`,
  `source_count`, `sources_json` (a JSON-encoded citations list, kept as a
  string so every row stays one flat CSV line), `section_count`,
  `word_count`, `slug`, `published_at`.
- New `page_parser.py` — this repo's own site-knowledge module (the
  analog of `lidl_parser.py`/`flight_parser.py`/`product_parser.py`).
  Three parsing paths in priority order: a generic schema.org JSON-LD
  `Article`/`CreativeWork`/`WebPage` lookup, Open Graph/Twitter Card meta
  tags, then a best-effort DOM fallback for the rendered article body,
  byline, sources list, and the two engagement counters.
- Three engines (`playwright_scraper.py` primary, `selenium_scraper.py`,
  `puppeteer_scraper.py`), adapted from `lidl-scraper`'s own: no
  scroll/pagination loop (a Page isn't paginated) — one bounded
  scroll-to-bottom-and-back pass per URL to surface any lazy-loaded
  content, then parse once. A URL whose path matches a robots.txt-
  disallowed prefix is filtered out before ever being requested, never
  attempted and reported as a failure.
- Standard doc set, CI workflows (`tests.yml`'s offline/docker/
  engine-smoke jobs, `canary.yml`, `claude.yml`, `claude-code-review.yml`),
  `Dockerfile` (Playwright-only, per CLAUDE.md §14), and a fictional
  `sample_output.json`/`.csv` (clearly marked as such — no real capture
  exists yet, per CLAUDE.md §15).

### Known gap at the initial build (closed 2026-09-30 — see above)
- **No live browser capture of perplexity.ai exists yet.** Direct HTTP
  access to the site is blocked from every automated shell available
  while building this repo (a cloud sandbox and a separate sandboxed VM
  both hit a proxy-level 403 on a plain `curl`). Every selector in
  `page_parser.py`, and the `NAV_TIMEOUT_MS`/`READINESS_WAIT_MS` constants
  in each engine, are `# TODO: verify live` best-effort guesses — some
  grounded in public research (robots.txt, the sitemap, Perplexity's own
  announcement blog for the Pages feature, third-party writeups), some
  just a reasonable bet (the OG-meta path). See README "Read this before
  trusting a run" and `TESTING.md` for exactly what's confirmed versus
  guessed, and what the first live run against this site should check.
