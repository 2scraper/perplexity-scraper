#!/usr/bin/env python3
"""scraper_api_client.py — the browserless HTTP client for 2Captcha's REST
surface. No site knowledge. `captcha_solver.py` and `fingerprint_client.py`
both sit on top of this rather than calling `requests` directly, so there is
exactly one place that knows the base URL, auth shape and error envelope.

Covers four of 2Captcha's five separately-billed products behind one key
(the fifth, proxies, is `proxy_pool.py` + PERPLEXITY_PROXY — a 2captcha.com/
proxy credential, 2prx.com is a synonym for the same product, not a
separate host):

  - classic captcha solving (createTask / getTaskResult)
  - the Scraping Browser API (POST /browser/connection -> a CDP URL)
  - the Fingerprint API (GET /fingerprint/random)
  - the Scraper API (POST https://scraper.2captcha.com/tasks/sync) — added
    2026-09-22. A GENUINELY DIFFERENT product from the Scraping Browser
    API above, and easy to confuse with it: the Scraping Browser API hands
    this process a CDP URL and WE drive a real Playwright/Selenium/
    Puppeteer browser against it; the Scraper API instead runs the fetch
    ENTIRELY on 2Captcha's own infrastructure (their own headless browser,
    not ours) and hands back the rendered HTML/Markdown/screenshot over a
    single HTTP call — no local or remote browser session on our end at
    all. Lives on its own hostname (`scraper.2captcha.com`, not
    `api.2captcha.com`) with its own Bearer-token auth, which is why it
    gets its own `scraper_api_base` override rather than reusing
    `self.api_base`. Confirmed live 2026-09-22 against shein.com: it DOES
    fetch real pages (verified real product markup came back), but it is
    NOT a bypass of shein's own bot-mitigation — the same `/risk/
    challenge` interstitial that a local Playwright browser hits shows up
    here too, intermittently. By itself it also has no captcha solving at
    all (there's no live page/DOM here for a solved token to be injected
    into) and no documented parameter to pin the exit country/locale (a
    clean fetch landed on shein.com's Netherlands locale in one live test,
    which this repo's parser — tuned for the US/English markup — then
    read as zero products, not blocked). `scrape_url()`'s `cdp_url`
    parameter (below) is the fix for both — chaining this product to
    2Captcha's own Scraping Browser instead of their default pool.
    This module is the core shared with shein-scraper: in THIS repo no
    engine calls `scrape_url()` or `scraping_browser_connection_url()` —
    perplexity reads articles from /rest/article/ inside a live page,
    which a browserless fetch cannot do. The engines' Browser API path
    is `--cdp-endpoint` / PERPLEXITY_CDP_ENDPOINT.

Never construct a competitor's API call from this module.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional

import requests

from proxy_pool import redact_credentials

API_BASE = "https://api.2captcha.com"
SCRAPER_API_BASE = "https://scraper.2captcha.com"


class TwoCaptchaError(RuntimeError):
    pass


class TwoCaptchaAuthError(TwoCaptchaError):
    """Missing/invalid key. Callers treat this as a WARNING, not a crash —
    a run continues without solving and reports EXIT_BLOCKED only if the
    page turns out to actually need it."""


@dataclass
class CaptchaTask:
    task_id: int


@dataclass
class ScrapeResult:
    """One Scraper API `/tasks/sync` response. `target_status` is the
    TARGET page's own HTTP status (shein.com's, not 2Captcha's) — shein's
    own risk gateway answers with 200 for a challenge page exactly like it
    does for a real one (see shein_parser.py), so a caller still has to run
    the same `detect_from_html(body, ...)` marker check as the browser
    engines do; `target_status` alone does not tell blocked apart from ok."""
    target_status: Optional[int]
    headers: dict
    body: str


# --------------------------------------------------------------------------- #
# Scraping Browser API connect policy — CLAUDE.md §26, measured on a sibling
# repo: a profile stays `profile_locked` for ~1.6-1.9s after a clean
# disconnect (HTTP 500 to an immediate reconnect), pyppeteer's connect()
# never resolves on a refused handshake, and a 401 means the endpoint's
# credentials expired (they last about a day). Seen here too on 2026-09-30:
# a 500 on a back-to-back run, a 401 on an older profile.
# --------------------------------------------------------------------------- #
CDP_CONNECT_ATTEMPTS = 3
CDP_CONNECT_RETRY_DELAY_S = 3.0
CDP_CONNECT_TIMEOUT_S = 10.0


def classify_cdp_failure(message: str) -> tuple:
    """(retryable, reader-facing reason) for a failed CDP connect. The
    message is expected to be redacted already."""
    text = message or ""
    if " 401" in text or "Unauthorized" in text:
        return False, ("the Scraping Browser endpoint's credentials were refused (HTTP 401) — they "
                       "expire after about a day; get a fresh endpoint from your 2Captcha dashboard")
    if " 500" in text or "profile_locked" in text or "timed out" in text.lower() or "Timeout" in text:
        return True, ("the Scraping Browser profile is busy or not ready (HTTP 500 / timeout) — usually "
                      "the previous session is still being released, or another run holds this pid")
    return False, "the Scraping Browser connection failed"


async def connect_with_retry(connect, *, redact, sleep=None, log=None):
    """Run `connect()` (an awaitable factory) up to CDP_CONNECT_ATTEMPTS
    times, each bounded by CDP_CONNECT_TIMEOUT_S, retrying only a
    retryable failure. Raises RuntimeError with a redacted, explained
    message when every attempt failed."""
    import asyncio
    sleep = sleep or asyncio.sleep
    last = ""
    for attempt in range(1, CDP_CONNECT_ATTEMPTS + 1):
        try:
            return await asyncio.wait_for(connect(), CDP_CONNECT_TIMEOUT_S)
        except asyncio.TimeoutError:
            last = f"connect timed out after {CDP_CONNECT_TIMEOUT_S:.0f}s"
        except Exception as exc:  # noqa: BLE001 — classified below, never propagated raw
            last = redact(str(exc)).splitlines()[0] if str(exc) else type(exc).__name__
        retryable, reason = classify_cdp_failure(last)
        if not retryable or attempt == CDP_CONNECT_ATTEMPTS:
            raise RuntimeError(f"CDP connection failed: {reason} ({last})") from None
        if log is not None:
            log.warning("CDP connect attempt %d/%d failed (%s) — retrying in %.0fs.", attempt,
                        CDP_CONNECT_ATTEMPTS, reason, CDP_CONNECT_RETRY_DELAY_S)
        await sleep(CDP_CONNECT_RETRY_DELAY_S)
    raise RuntimeError(f"CDP connection failed: {last}")


class TwoCaptchaClient:
    def __init__(
        self, api_key: Optional[str], timeout: int = 30, api_base: Optional[str] = None,
        scraper_api_base: Optional[str] = None,
    ):
        self.api_key = api_key
        self.timeout = timeout
        # `--captcha-api` (testing only): override the base URL so a smoke
        # test or a local run can point this at a mock server instead of
        # the real https://api.2captcha.com. CLAUDE.md §17 names the exact
        # failure mode this guards against — a CLI flag defined on the
        # parser but never read by anything is "a documented feature not
        # working at all", found live in this family more than once.
        # `self.api_base` (an instance attribute, never the module-level
        # API_BASE) is what every call below actually uses.
        self.api_base = api_base or API_BASE
        # Same reasoning, same escape hatch, but a SEPARATE override and a
        # SEPARATE default — the Scraper API lives on its own hostname
        # (`scraper.2captcha.com`), not `api.2captcha.com`. Only a caller
        # passing `scraper_api_base=` sets this; `--captcha-api` never does.
        self.scraper_api_base = scraper_api_base or SCRAPER_API_BASE

    def _require_key(self) -> str:
        if not self.api_key:
            raise TwoCaptchaAuthError(
                "TWOCAPTCHA_KEY is not set — get one at https://2captcha.com/setting"
            )
        return self.api_key

    def _post(self, path: str, payload: dict) -> dict:
        # createTask/getTaskResult/getBalance all come through here, and
        # until this fix a network failure (2Captcha down, DNS failure, a
        # `--captcha-api` override pointed at nothing) raised a bare
        # `requests.exceptions.RequestException` instead of
        # `TwoCaptchaError` — invisible to captcha_solver.solve_when_
        # blocked's `except TwoCaptchaAuthError / except TwoCaptchaError`
        # pair, so it propagated all the way up as an uncaught crash.
        # That directly contradicts this codebase's own stated policy (see
        # captcha_solver.py's module docstring): "a solver-side error is a
        # WARNING, never a crash — the run continues". Confirmed the
        # identical, still-unfixed gap in stockx-scraper's
        # scraper_api_client.py (left there per the standing instruction
        # not to touch that repo).
        try:
            resp = requests.post(f"{self.api_base}{path}", json=payload, timeout=self.timeout)
        except requests.exceptions.RequestException as exc:
            raise TwoCaptchaError(f"{path} request failed: {redact_credentials(str(exc))}") from None
        try:
            data = resp.json()
        except ValueError as exc:
            raise TwoCaptchaError(f"non-JSON response from {path}: {resp.text[:200]}") from exc
        return data

    # ----------------------------------------------------------------- #
    # Classic captcha solving — createTask / getTaskResult
    # ----------------------------------------------------------------- #
    def create_task(self, task: dict) -> CaptchaTask:
        key = self._require_key()
        data = self._post("/createTask", {"clientKey": key, "task": task})
        if data.get("errorId"):
            raise TwoCaptchaError(f"createTask failed: {data.get('errorCode')} {data.get('errorDescription')}")
        return CaptchaTask(task_id=data["taskId"])

    def get_task_result(self, task: CaptchaTask) -> dict:
        key = self._require_key()
        return self._post("/getTaskResult", {"clientKey": key, "taskId": task.task_id})

    def get_balance(self) -> float:
        """Account balance in USD, shared across all four 2Captcha products
        behind this one key. Cheap way to confirm a key is live without
        spending anything (unlike createTask/random_fingerprint, both of
        which are billed)."""
        key = self._require_key()
        data = self._post("/getBalance", {"clientKey": key})
        if data.get("errorId"):
            raise TwoCaptchaError(f"getBalance failed: {data.get('errorCode')} {data.get('errorDescription')}")
        return float(data["balance"])

    def solve_and_wait(self, task: dict, poll_interval: float = 5.0, max_wait: float = 180.0) -> str:
        """Blocks (in small polls) until the task resolves, and returns a
        string the caller can use to unblock the page. Raises
        TwoCaptchaError on failure/timeout.

        For every widget this client originally supported (Turnstile,
        reCAPTCHA v2/v3, hCaptcha), 2Captcha's `solution` is one opaque
        token string (`token` or `gRecaptchaResponse`) — the whole widget
        collapses to "paste this into one hidden field/callback argument".
        GeeTest (added 2026-09-21 for captcha_solver.CaptchaType.GEETEST_V3/
        GEETEST_V4 — see that module's docstring) does NOT: its solution is
        several fields the page's own JS callback expects TOGETHER (v3:
        `challenge`/`validate`/`seccode`; v4: `captcha_id`/`lot_number`/
        `pass_token`/`gen_time`/`captcha_output`), not one string. Rather
        than raise on every successful GeeTest solve, this method falls
        back to returning the JSON-encoded solution dict for any task whose
        solution has no `token`/`gRecaptchaResponse` — a caller must
        `json.loads()` this and call the real page's GeeTest callback with
        the parsed fields, not treat it as a single value the way every
        other widget's return works. This is genuinely different from the
        single-token contract this method's return type otherwise
        promises, so a caller that only knows the old widgets can keep
        assuming a plain opaque string; a GeeTest caller has to know to
        parse it.
        """
        created = self.create_task(task)
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            result = self.get_task_result(created)
            if result.get("errorId"):
                raise TwoCaptchaError(
                    f"getTaskResult failed: {result.get('errorCode')} {result.get('errorDescription')}"
                )
            if result.get("status") == "ready":
                solution = result.get("solution", {})
                token = solution.get("token") or solution.get("gRecaptchaResponse")
                if token:
                    return token
                if solution:
                    return json.dumps(solution)
                raise TwoCaptchaError(f"solved task carried no usable token: {solution!r}")
            time.sleep(poll_interval)
        raise TwoCaptchaError(f"solve timed out after {max_wait:.0f}s")

    # ----------------------------------------------------------------- #
    # Scraping Browser API — one live CDP connection per profile
    # ----------------------------------------------------------------- #
    def scraping_browser_connection_url(
        self, *, country: Optional[str] = None, profile_id: Optional[str] = None,
        account_id: Optional[int] = None,
    ) -> str:
        """Ask Browser API for a ready-made CDP URL with its own credentials.

        The regular API key is not the browser login or password. Country is
        a selection constraint on an existing account, not a country switch:
        changing a saved proxy/account is a separate Browser API operation.
        """
        key = self._require_key()
        try:
            response = requests.get(
                f"{self.api_base}/browser/accounts", params={"key": key}, timeout=self.timeout,
            )
            response.raise_for_status()
            listing = response.json()
        except (requests.exceptions.RequestException, ValueError) as exc:
            raise TwoCaptchaError(f"Browser API account lookup failed ({type(exc).__name__})") from None
        if listing.get("status") != "OK":
            raise TwoCaptchaError(f"Browser API account lookup failed: {listing.get('errorCode', 'unknown error')}")
        raw_accounts = listing.get("data") or []
        accounts = list(raw_accounts.values()) if isinstance(raw_accounts, dict) else raw_accounts
        accounts = [a for a in accounts if isinstance(a, dict)]
        if account_id is not None:
            accounts = [a for a in accounts if str(a.get("id")) == str(account_id)]
        if country:
            accounts = [a for a in accounts if (a.get("country") or "").lower() == country.lower()]
        if len(accounts) != 1:
            raise TwoCaptchaError(
                "Browser API needs exactly one matching account; configure an account for the "
                "requested country and select it with account_id"
            )
        selected_id = accounts[0].get("id")
        payload = {"key": key, "accountId": selected_id}
        if profile_id:
            payload["profileId"] = profile_id
        try:
            response = requests.post(
                f"{self.api_base}/browser/connection", json=payload, timeout=self.timeout,
            )
            response.raise_for_status()
            connection = response.json()
        except (requests.exceptions.RequestException, ValueError) as exc:
            raise TwoCaptchaError(f"Browser API connection lookup failed ({type(exc).__name__})") from None
        if connection.get("status") != "OK" or not connection.get("connectionUri"):
            raise TwoCaptchaError(
                f"Browser API connection lookup failed: {connection.get('errorCode', 'missing connectionUri')}"
            )
        uri = connection["connectionUri"]
        if country and f"-country-{country.lower()}" not in uri.lower():
            raise TwoCaptchaError("Browser API selected a profile with a different country")
        return uri

    # ----------------------------------------------------------------- #
    # Fingerprint API
    # ----------------------------------------------------------------- #
    def random_fingerprint(self, *, tags: Optional[str] = None, country: Optional[str] = None) -> dict:
        # Unlike createTask/getTaskResult/getBalance above, this endpoint
        # takes its key as a URL query parameter (`?key=...`), not a JSON
        # body — and `requests` puts the FULL URL, query string included,
        # into the text of every HTTPError and connection error (CLAUDE.md
        # §8 names this exact endpoint). Redact before raising, and drop
        # the original exception from the chain (`from None`) so its own
        # unredacted text can't surface via "the above exception was the
        # direct cause of…".
        key = self._require_key()
        params = {"key": key, "format": "chromium"}
        if tags:
            params["tags"] = tags
        if country:
            params["country"] = country
        try:
            resp = requests.get(f"{self.api_base}/fingerprint/random", params=params, timeout=self.timeout)
            resp.raise_for_status()
        except requests.exceptions.RequestException as exc:
            raise TwoCaptchaError(f"fingerprint/random request failed: {redact_credentials(str(exc))}") from None
        return resp.json()

    # ----------------------------------------------------------------- #
    # Scraper API — one browserless fetch per call, no local/CDP browser
    # ----------------------------------------------------------------- #
    def scrape_url(
        self, url: str, *, data_format: str = "raw", timeout: int = 60,
        wait_for: Optional[dict] = None, cdp_url: Optional[str] = None,
    ) -> ScrapeResult:
        """POST https://scraper.2captcha.com/tasks/sync — see the module
        docstring for how this differs from `scraping_browser_connection_
        url()` above. `timeout` is 2Captcha's own bound on how long THEY
        wait for the target page to finish loading (1-120s, their limit,
        not ours) — not this call's own HTTP timeout, which is set a
        little higher below so our client doesn't give up before 2Captcha
        itself would. `wait_for`, if given, must already be the dict
        2Captcha's own Playwright-shaped wait condition expects (e.g.
        `{"state": "networkidle"}`) — confirmed live 2026-09-22 that a
        JSON-encoded STRING here is rejected with `422 ScrapeParser:
        params.waitFor must be an object`, despite the published docs
        showing it as a string; this client sends it as a real JSON object
        to match what the API actually accepts, not what its own docs
        say. `cdp_url` (2Captcha's `cdpurl` field) lets a caller point
        this fetch at a CDP session THEY already control instead of
        2Captcha's own default browser pool. Fill it with
        `scraping_browser_connection_url()`'s own output — 2Captcha's OWN
        Scraping Browser, not an arbitrary CDP session (one isn't known to
        support this field) — to get this endpoint real captcha auto-solve
        (this endpoint alone has none) and the country pinning described
        in the module docstring. No caller in this repo (see the module
        docstring)."""
        key = self._require_key()
        payload = {
            "task_type": "scrape",
            "url": url,
            "format": "json",
            "data_format": data_format,
            "timeout": timeout,
        }
        if wait_for:
            payload["waitFor"] = wait_for
        if cdp_url:
            payload["cdpurl"] = cdp_url
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            resp = requests.post(
                f"{self.scraper_api_base}/tasks/sync", headers=headers, json=payload,
                timeout=timeout + 15,
            )
        except requests.exceptions.RequestException as exc:
            raise TwoCaptchaError(f"Scraper API request failed: {redact_credentials(str(exc))}") from None
        if resp.status_code == 401:
            raise TwoCaptchaAuthError("Scraper API: invalid/missing TWOCAPTCHA_KEY")
        if resp.status_code == 402:
            raise TwoCaptchaError("Scraper API: insufficient 2Captcha balance")
        if resp.status_code == 408:
            raise TwoCaptchaError(f"Scraper API: task did not finish within {timeout}s")
        if resp.status_code != 200:
            raise TwoCaptchaError(
                f"Scraper API returned HTTP {resp.status_code}: {redact_credentials(resp.text[:300])}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise TwoCaptchaError(f"Scraper API: non-JSON response ({resp.text[:200]})") from exc
        return ScrapeResult(
            target_status=data.get("http_code"), headers=data.get("headers") or {},
            body=data.get("body") or "",
        )
