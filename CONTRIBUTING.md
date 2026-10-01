# Contributing

1. Fork, branch, make your change.
2. Run `python3 smoke_test.py` (or `pytest tests/test_smoke.py`). It is offline and must pass with **no** engine installed; install one engine (`pip install -r requirements-playwright.txt`, or `-selenium` / `-puppeteer`) to exercise its code too.
3. **If you touched `page_parser.py`**: every parser check runs on real captures in `tests/fixtures/` (the `/rest/article/` API for a classic Page and a Discover article, the Discover feed, and a Cloudflare challenge page). If the site changed shape, save a scrubbed capture of what it serves now next to them, update the parser to match, and add a `smoke_test.py` check against the new fixture. Don't change the parser on a guess without a capture behind it.
4. Keep the three engines behaving identically: same exit codes, same `Product` schema, same CLI flags. The fetch logic itself lives once in `page_flow.fetch_article()`; an engine only supplies its driver's own page operations. If one engine has to diverge (see `selenium_scraper.py`'s CDP-credential limitation), say why in a comment.
5. Open a PR. CI runs the offline suite on two Python versions, builds the wheel and the Docker image, and runs one `engine-smoke` job per engine in its own virtualenv. A daily `canary` job scrapes the live site over the Scraping Browser API when the repo has the `PERPLEXITY_CDP_ENDPOINT` secret (see `TESTING.md`).

Bug reports and feature requests: open an issue. Please include the exact command you ran (without credentials) and the `.meta.json` sidecar from the run — or say that none was written, which is itself informative (see `output_writer.finish_run`). If an article came back blocked or empty, a `--dump-html` capture is the most useful thing you can attach.
