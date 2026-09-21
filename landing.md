# Perplexity Pages Scraper by 2scraper

**Open-source scraper for Perplexity Pages — three engines, your own infrastructure by default, 2Captcha's paid products when you actually need them.**

Pull a Page's title, author, sources/citations, view count, follow-up-question count, and section/body shape — straight from a Page URL into JSON or CSV.

[**View source on GitHub →**](https://github.com/2scraper/perplexity-scraper)

---

## Before you scrape: official channels

Check Perplexity's own site and API offerings for anything a formal integration already covers your use case. This scraper exists for everything outside that: reading a Page's own already-published content, one URL (or a batch of them) at a time.

## Read this before you rely on it

**Update: a live incident now exists.** On 2026-09-21, perplexity.ai
served a real Cloudflare managed challenge — twice, to two different
real Page URLs — instead of any Page content. This repo's block
detection correctly catches it (confirmed against the actual captured
page). What it does NOT yet confirm is whether this repo's own scraper
engines get the same treatment as a plain browser, or any selector below
against a real, successfully-rendered Page — this was a block page, not
a results page. Every selector in `page_parser.py` for an actual Page's
content is still a best-effort guess — some grounded in public research
(`robots.txt`, the sitemap, Perplexity's own announcement blog for the
Pages feature), some just a reasonable bet (that Open Graph meta tags are
server-rendered for sharing). What IS confirmed: Perplexity Pages are
real, public wiki-style articles at `perplexity.ai/page/{slug}-{id}`, not
blocked by `robots.txt`; they have no site-search mechanism — which is
why this tool takes a URL (or a file of them) rather than a search query,
unlike this project's sibling scrapers; and the site fronts at least some
requests with a Cloudflare challenge. The architecture (exit codes,
output schema, dedupe, credential handling, all three engines) is real
and tested, same as every 2scraper repo. Full honesty section, with
exactly what's confirmed and what's still a guess, in the [repository
README](https://github.com/2scraper/perplexity-scraper#readme) — read it
before you point this at anything that matters.

## What you get

- Free, open-source scraper, one script per engine — **Playwright** (primary, local-first), **Selenium**, and **Puppeteer** (via pyppeteer), all producing the identical output schema and exit codes
- Fetch a single Page by URL, or a batch from a file — no query-based search exists for this site, so this is the actual, structural equivalent
- Three parsing paths in priority order: schema.org JSON-LD, Open Graph/Twitter Card meta tags, then a DOM fallback for the rendered article
- Page-specific fields other 2scraper repos don't need: author, view count, follow-up-question count, a JSON-encoded sources/citations list, section count, word count, slug, publish date
- JSON and CSV export, with a documented `Product` schema and a `.meta.json` sidecar on every completed/partial run
- Optional 2Captcha integration, wired in but never required to get started
- Respects `robots.txt` by construction: a disallowed path is filtered out before ever being requested, not attempted and reported as a failure

## 2Captcha products, when you want them

| Product | What it's for |
|---|---|
| **Captcha solving — [2captcha.com](https://2captcha.com)** | Detects a challenge, decides whether it's actually blocking you (not just present), solves it |
| **Scraping Browser API — 2captcha.com** | A remote browser session over CDP with its own proxy, fingerprint and captcha auto-solve bundled — `--cdp-endpoint` |
| **Browser fingerprints — 2captcha Fingerprint API** | Pin a specific OS/browser/country fingerprint for a locally-launched browser |
| **Proxies — 2captcha.com/proxy** (2prx.com is the same product, different name) | Drop credentials into `.env`, rotated automatically with per-exit failure tracking |

## Who this is for

Anyone who wants a Perplexity Page's already-published content (not its live "Ask AI" feature, which is explicitly out of scope) in a script rather than a browser tab — archivists, researchers tracking a set of Pages over time with `diff_runs.py`, or anyone building on top of Pages content they already have URLs for. Read the README's honesty section first: this is the newest, least-verified member of this scraper family.

## Get started

```bash
git clone https://github.com/2scraper/perplexity-scraper.git
cd perplexity-scraper
pip install -r requirements-playwright.txt && playwright install chromium
cp .env.example .env   # optional — not required for a normal local-first run

python3 playwright_scraper.py --url "https://www.perplexity.ai/page/some-article-AbCdEfGhIjKlMnOpQrStUv" --format json --out results.json
```

Full setup, CLI reference, and configuration details in the [repository README](https://github.com/2scraper/perplexity-scraper#readme).

---

**Need it running at scale, with proxies, fingerprints, and captcha solving already configured?**
[Talk to us →](https://2captcha.com/contact) · Proxies by [2captcha.com/proxy](https://2captcha.com/proxy) · Scraping Browser API & captcha solving by [2captcha.com](https://2captcha.com)
