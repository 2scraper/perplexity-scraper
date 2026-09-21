# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/) as closely as a CLI toolkit can manage. A patch
release means "fixes", not that every flag and default is frozen — a
behaviour-changing default gets called out explicitly in its entry below
rather than being a silent violation of that.

## [Unreleased]

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

### Known gap, read before trusting anything above
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
