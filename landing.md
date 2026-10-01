# Perplexity Scraper by 2scraper

**Open-source scraper for Perplexity articles — classic Pages and Discover — three engines, run over 2Captcha's Scraping Browser API.**

Pull an article's title, author, summary, publish and update times, view/like/fork counts, every cited source, and section and word counts — from a URL, a file of URLs, or the Discover feed — into JSON or CSV.

[**View source on GitHub →**](https://github.com/2scraper/perplexity-scraper)

---

## Before you scrape: official channels

Check Perplexity's own site and API offerings for anything a formal integration already covers your use case. This scraper exists for everything outside that: reading articles that are already published, one URL, a batch, or the Discover feed at a time.

## What to expect

Every row comes from the site's own article API (`/rest/article/`), called from inside the article page, not from the HTML. Live-verified on 2026-09-30 and again on 2026-10-01 over a US Scraping Browser API profile: Playwright and Puppeteer scraped classic Pages and Discover articles end to end, 25 of 25 on a `--discover top` run, and a dead URL was correctly reported as not found. Cloudflare blocked every local browser tried, and the run reports that (exit 3) instead of returning bad data — so the Browser API is the recommended setup. Details in the [README](https://github.com/2scraper/perplexity-scraper#readme).

## What you get

- Free, open-source scraper, one script per engine — **Playwright** (recommended), **Puppeteer** (via pyppeteer) and **Selenium**, all producing the identical output schema and exit codes
- One article by URL, a batch from a file, or the Discover feed (`--discover top`) up to `--max-results`
- Article fields: author, summary, publish/update times, read time, view/like/fork counts, a JSON-encoded sources list, section and word counts, slug
- JSON and CSV export, with a documented `Product` schema and a `.meta.json` sidecar on every completed/partial run
- A browserless mode (`--scraper-api`) that needs no browser driver installed
- A failed article never hides the ones that succeeded: the sidecar lists every failed URL with its reason
- Respects `robots.txt` by construction: a disallowed path is filtered out before ever being requested

## 2Captcha products, when you want them

| Product | What it's for |
|---|---|
| **Scraping Browser API — 2captcha.com** | A remote browser session over CDP with its own proxy, fingerprint and captcha auto-solve bundled — `--cdp-endpoint`. The setup this scraper was verified on |
| **Scraper API — 2captcha.com** | No browser at all: `--scraper-api` fetches the feed and every article through the same profile, one HTTP call each |
| **Browser fingerprints — 2captcha Fingerprint API** | Pin a specific OS/browser/country fingerprint for a locally-launched browser |
| **Proxies — 2captcha.com/proxy** (2prx.com is the same product, different name) | Drop credentials into `.env`, rotated automatically with per-exit failure tracking |

## Who this is for

Researchers, archivists and anyone tracking Perplexity articles over time (`diff_runs.py` compares two runs), who want published article content in a script rather than a browser tab. Perplexity's live "Ask AI" answers are out of scope.

## Get started

```bash
git clone https://github.com/2scraper/perplexity-scraper.git
cd perplexity-scraper
pip install -r requirements-playwright.txt && playwright install chromium
cp .env.example .env   # PERPLEXITY_CDP_ENDPOINT goes here

python3 playwright_scraper.py --discover top --max-results 20 --format json --out results.json
```

Full setup, CLI reference, and configuration details in the [repository README](https://github.com/2scraper/perplexity-scraper#readme).

---

**Need it running at scale, with proxies, fingerprints, and captcha solving already configured?**
[Talk to us →](https://2captcha.com/contact) · Proxies by [2captcha.com/proxy](https://2captcha.com/proxy) · Scraping Browser API & captcha solving by [2captcha.com](https://2captcha.com)
