#!/usr/bin/env python3
"""captcha_solver.py — detection + solving policy. Comments may name a site
for context; no site-specific selector or URL lives here. Per-site markers
are passed in by the caller (see BOT_CHALLENGE_MARKERS in product_parser.py)
rather than hardcoded, so this module stays reusable across the family.

Policy, matching the family's hard-won rule that "detected != blocking":

  - Detection stays broad — run it on every page, unconditionally.
  - A captcha WIDGET present on a page whose products are already rendered
    guards nothing and is not solved: `--solve-captcha when-blocked` (the
    default) only pays for a solve when `count_product_links(html) == 0`.
  - A missing key or a solver-side error is a WARNING, never a crash — the
    run continues, and only reports EXIT_BLOCKED if the page really was
    gated (see output_writer.EXIT_BLOCKED).

No page-execution primitive (page.evaluate / execute_script) crosses this
module's boundary: `build_injection_script()` below only BUILDS a plain
JavaScript source string — driver-agnostic, since it's just text — and the
engine (which already speaks its own driver's dialect) is the one that
actually runs it. That split is the same one `solve_when_blocked()` uses
for detection/solving: this module owns policy and JS-source construction,
never the act of running JS against a real page.

**Until 2026-09-21, no engine in this family actually injected a solved
token back into a locally-launched (non `--cdp-endpoint`) browser at
all** — every engine's own `_maybe_solve_captcha` only logged "solved", a
real gap Roman asked about directly after GeeTest support was added
without it. `build_injection_script()` closes that gap using each widget
type's OWN standard, publicly-documented client-integration convention
(a hidden `g-recaptcha-response` textarea for reCAPTCHA v2, a
`cf-turnstile-response` field for Turnstile, and so on) — **not** anything
confirmed against a real shein.com (or any family site's) actual widget
markup, which has never been captured for ANY type. It is the same
"standard convention, explicitly unconfirmed against this site" honesty
posture `shein_parser.py`'s own DOM fallback selectors use. reCAPTCHA v3
has no such convention — it's invisible, and the token is typically
consumed the instant the SITE'S OWN JavaScript resolves
`grecaptcha.execute()`'s promise (often straight into an XHR, never read
back off a DOM element) — so `build_injection_script()` returns `None` for
it rather than guess at site-specific code, which this shared module's own
charter says does not belong here.

Over `--cdp-endpoint`, none of this applies: 2Captcha's own
`Captcha.setAutoSolve` CDP domain (see each engine's own
`_enable_scraping_browser_auto_solve`) solves AND injects entirely inside
their infrastructure, for whichever widget types their Scraping Browser
extension recognizes (confirmed live, 2026-09-14: Turnstile, Amazon WAF,
Yandex SmartCaptcha, Lemin — GeeTest was NOT in that confirmed list,
though it may be covered and simply wasn't seen in that one capture).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Sequence

from scraper_api_client import TwoCaptchaAuthError, TwoCaptchaClient, TwoCaptchaError

# Generic signals any family site might show. A per-site BOT_CHALLENGE_MARKERS
# list (product_parser.py) is unioned with this, never a replacement for it.
GENERIC_BOT_CHALLENGE_MARKERS: Sequence[str] = (
    "cf-turnstile", "challenges.cloudflare.com", "cdn-cgi/challenge-platform",
    "g-recaptcha", "recaptcha/api.js", "grecaptcha",
    "h-captcha", "hcaptcha.com/1/api.js",
    "px-captcha", "perimeterx",
    "datadome",
    "attention required", "just a moment", "verify you are human",
)


class CaptchaType(str, Enum):
    CLOUDFLARE_TURNSTILE = "cloudflare_turnstile"
    RECAPTCHA_V2 = "recaptcha_v2"
    RECAPTCHA_V3 = "recaptcha_v3"
    HCAPTCHA = "hcaptcha"
    GEETEST_V3 = "geetest_v3"
    GEETEST_V4 = "geetest_v4"


_SITEKEY_PATTERNS = {
    CaptchaType.CLOUDFLARE_TURNSTILE: re.compile(
        r'class="[^"]*cf-turnstile[^"]*"[^>]*data-sitekey="([^"]+)"'
    ),
    CaptchaType.RECAPTCHA_V2: re.compile(
        r'class="[^"]*g-recaptcha[^"]*"[^>]*data-sitekey="([^"]+)"'
    ),
    CaptchaType.HCAPTCHA: re.compile(
        r'class="[^"]*h-captcha[^"]*"[^>]*data-sitekey="([^"]+)"'
    ),
}
# v3 ships no visible widget — the sitekey rides on the loader's `render=`
# query param instead of a data-sitekey attribute.
_RECAPTCHA_V3_LOADER_RE = re.compile(r"recaptcha/api\.js\?render=([\w-]+)")
_RECAPTCHA_EXPLICIT_LOADER_RE = re.compile(r"recaptcha/api\.js\?render=explicit")

# reCAPTCHA v2's sitekey doesn't always ship as static `<div class="g-
# recaptcha" data-sitekey="...">` markup, the only shape
# `_SITEKEY_PATTERNS[RECAPTCHA_V2]` above can see — CONFIRMED live on
# shein.com, 2026-09-21 (see README "Known limitations" and Roman's own
# question about whether other captcha vendors are in play): the site
# loads `https://www.google.com/recaptcha/api.js` and has a live
# `window.grecaptcha` v2 object (`render`/`execute`/`getResponse`/
# `reset`/`ready` — not `.enterprise`) on every normal page, but the
# sitekey itself lives in a JS config object under a site-chosen key
# name (`window.gbCommonInfo.GOOGLE_VERIFY_SITEKEY`), presumably handed
# to `grecaptcha.render()` programmatically later (e.g. after a
# suspicious login attempt) rather than rendered into the initial HTML
# at all. The static-markup regex above would silently MISS this real,
# live sitekey — a real detection gap, not a hypothetical one. reCAPTCHA
# sitekeys have a fixed, distinctive shape regardless of what variable
# or markup carries them (`6L` + 38 more URL-safe-base64 characters, 40
# total, confirmed against Google's own documented format and against
# shein.com's actual `6LcoBR4UAAAAAIi5xU3U_q37C3nFaSckeMaT-P5j`) — so
# this searches for that shape ANYWHERE in the page, gated on the v2
# loader actually being present so it can't misfire on an unrelated
# 40-character token elsewhere that happens to start with "6L".
_RECAPTCHA_SITEKEY_ANYWHERE_RE = re.compile(r"\b(6L[\w-]{38})\b")
_RECAPTCHA_V2_LOADER_RE = re.compile(r"recaptcha/api\.js")

# GeeTest — added 2026-09-21 after shein.com's own risk gateway
# (captcha_type=909, see shein_parser.BOT_CHALLENGE_MARKERS) correlated
# with GeeTest via three circumstantial signals documented in
# shein_parser.py's module docstring (the site's global stylesheet defines
# .geetest_wind/.geetest_panel on EVERY page, robots.txt disallows
# /geetest/, and the numeric captcha_type code is consistent with GeeTest's
# own convention) — NOT a confirmed vendor string the way every other
# CaptchaType below was found (an actual `cf-turnstile`/`g-recaptcha`/
# `h-captcha` class or loader URL, seen live). No family site, shein.com
# included, has ever had its actual GeeTest widget markup captured — only
# a waiting/redirect shell page, never what renders when a real challenge
# is actually shown. These two patterns are therefore UNCONFIRMED
# best-effort, built from GeeTest's own public client-integration
# documentation (a `data-captcha-id`/`captchaId` attribute for v4; `gt`
# and `challenge` fields together for v3 — both are 32-character
# hex-like ids in GeeTest's own docs), not a shein.com-specific fact.
# `# TODO: verify live` applies here exactly like shein_parser.py's own
# DOM fallback selectors — update these the moment a real capture exists.
_GEETEST_V4_ID_PATTERNS = (
    re.compile(r'data-captcha-id=["\']([a-f0-9]{32})["\']'),
    re.compile(r'captchaId["\']?\s*:\s*["\']([a-f0-9]{32})["\']'),
)
_GEETEST_V3_RE = re.compile(
    r'gt["\']?\s*:\s*["\']([a-f0-9]{32})["\'][^{}]*?challenge["\']?\s*:\s*["\']([a-f0-9]{32})["\']',
    re.S,
)


@dataclass
class CaptchaSignal:
    captcha_type: CaptchaType
    sitekey: Optional[str]
    invisible: bool = False
    # GeeTest-only fields (see the module-docstring note above on why
    # GeeTest doesn't fit the plain sitekey shape every other type uses).
    gt: Optional[str] = None
    challenge: Optional[str] = None
    captcha_id: Optional[str] = None
    api_server: Optional[str] = None


# The Scraping Browser API's managed Chromium ships 2Captcha's OWN
# captcha-solving extension pre-installed (confirmed live, 2026-09-14:
# extension id kjmkgkdkpedkejedfhmfcenooemhbpbo, content scripts named
# .../captcha/{turnstile,amazon_waf,yandex,lemin}/{interceptor,hunter}.js).
# `page.content()` over --cdp-endpoint captures THOSE injected <script>
# tags too, and their src/data attributes contain marker substrings like
# "cf-turnstile" regardless of whether the actual page has a real widget —
# on a real StockX 403 that turned out to be a plain, static Cloudflare
# WAF page with no captcha at all ("Sorry, you have been blocked" — no
# `data-sitekey` anywhere), this alone made GENERIC_BOT_CHALLENGE_MARKERS
# fire and identify_widget() correctly find nothing to solve, which then
# logged as a confusing "captcha-like marker detected but no known
# widget/sitekey" rather than a clean "no captcha present". Stripped here
# so the extension's own always-there code never counts as the page's.
_EXTENSION_SCRIPT_RE = re.compile(
    r'<script\b[^>]*\bchrome-extension://[^>]*>.*?</script>', re.I | re.S
)


def _strip_extension_noise(html: str) -> str:
    return _EXTENSION_SCRIPT_RE.sub("", html)


def detect_from_html(html: str, extra_markers: Sequence[str] = ()) -> bool:
    """Broad detection: True if ANY known marker string appears. Cheap and
    deliberately over-inclusive — see module docstring on why detection and
    blocking are decided separately."""
    haystack = _strip_extension_noise(html).lower()
    for marker in (*GENERIC_BOT_CHALLENGE_MARKERS, *extra_markers):
        if marker.lower() in haystack:
            return True
    return False


def identify_widget(html: str) -> Optional[CaptchaSignal]:
    """Best-effort: which widget, and its sitekey. Reconciles the v3-vs-
    v2-invisible ambiguity by trusting the LOADER'S query param over any
    wrapper metadata — a site can label its own wrapper "v3" while shipping
    a `render=explicit` (v2-invisible) loader; sending v3 parameters for a
    v2-invisible widget buys a token the site rejects. `render=<sitekey>`
    means v3; `render=explicit` means v2."""
    html = _strip_extension_noise(html)
    m = _RECAPTCHA_V3_LOADER_RE.search(html)
    if m and not _RECAPTCHA_EXPLICIT_LOADER_RE.search(html):
        return CaptchaSignal(CaptchaType.RECAPTCHA_V3, sitekey=m.group(1))
    for ctype, pattern in _SITEKEY_PATTERNS.items():
        m = pattern.search(html)
        if m:
            return CaptchaSignal(ctype, sitekey=m.group(1))
    # Fallback for a reCAPTCHA v2 sitekey that isn't in static markup at
    # all — see _RECAPTCHA_SITEKEY_ANYWHERE_RE's module-level comment for
    # the live shein.com evidence this closes a real gap for.
    if _RECAPTCHA_V2_LOADER_RE.search(html):
        m = _RECAPTCHA_SITEKEY_ANYWHERE_RE.search(html)
        if m:
            return CaptchaSignal(CaptchaType.RECAPTCHA_V2, sitekey=m.group(1))
    # GeeTest — v4 first (current-generation; see the UNCONFIRMED note on
    # the patterns themselves, above).
    for pattern in _GEETEST_V4_ID_PATTERNS:
        m = pattern.search(html)
        if m:
            return CaptchaSignal(CaptchaType.GEETEST_V4, sitekey=None, captcha_id=m.group(1))
    m = _GEETEST_V3_RE.search(html)
    if m:
        return CaptchaSignal(CaptchaType.GEETEST_V3, sitekey=None, gt=m.group(1), challenge=m.group(2))
    return None


def _task_payload(signal: CaptchaSignal, page_url: str, *, proxyless: bool, min_score: float = 0.3) -> dict:
    # GeeTest's task shape has no `websiteKey` at all (v3 sends `gt`/
    # `challenge`, v4 sends `captchaId`) — confirmed against 2Captcha's own
    # published API reference for GeeTestTask(Proxyless)/GeeTestV4Task
    # (Proxyless), unlike identify_widget()'s extraction patterns above,
    # which are NOT confirmed against a real shein.com capture.
    if signal.captcha_type == CaptchaType.GEETEST_V4:
        return {
            "type": "GeeTestV4TaskProxyless" if proxyless else "GeeTestV4Task",
            "websiteURL": page_url,
            "captchaId": signal.captcha_id,
        }
    if signal.captcha_type == CaptchaType.GEETEST_V3:
        task = {
            "type": "GeeTestTaskProxyless" if proxyless else "GeeTestTask",
            "websiteURL": page_url,
            "gt": signal.gt,
            "challenge": signal.challenge,
        }
        if signal.api_server:
            task["geetestApiServerSubdomain"] = signal.api_server
        return task

    type_map = {
        CaptchaType.CLOUDFLARE_TURNSTILE: "TurnstileTaskProxyless" if proxyless else "TurnstileTask",
        CaptchaType.RECAPTCHA_V2: "RecaptchaV2TaskProxyless" if proxyless else "RecaptchaV2Task",
        CaptchaType.RECAPTCHA_V3: "RecaptchaV3TaskProxyless",
        CaptchaType.HCAPTCHA: "HCaptchaTaskProxyless" if proxyless else "HCaptchaTask",
    }
    task = {"type": type_map[signal.captcha_type], "websiteURL": page_url, "websiteKey": signal.sitekey}
    if signal.captcha_type == CaptchaType.RECAPTCHA_V3:
        # 2Captcha's own field name for RecaptchaV3TaskProxyless (confirmed
        # against their API reference) — the token 2Captcha hands back is
        # produced to clear THIS threshold; it is a solve-request input, not
        # something to validate against the response afterwards. `--min-
        # score` is the CLI knob; omitting it would leave 2Captcha's own
        # server-side default in effect, silently, which is the same
        # "documented flag nobody reads" shape this family has shipped
        # before (CLAUDE.md §17) — so this module always sends one.
        task["minScore"] = min_score
    return task


def solve_when_blocked(
    *,
    client: TwoCaptchaClient,
    page_url: str,
    html: str,
    count_product_links: Callable[[str], int],
    extra_markers: Sequence[str] = (),
    proxyless: bool = True,
    min_score: float = 0.3,
) -> dict:
    """The default `--solve-captcha when-blocked` policy. Cheap first: count
    product links on the page AS-IS — no readiness wait, no scroll — because
    a page whose products are already rendered is not blocked, and running
    a 20s readiness wait first would burn it even when solving is what
    actually makes products appear on a page that IS gated.
    """
    if not detect_from_html(html, extra_markers):
        return {"action": "no_captcha_detected"}

    if count_product_links(html) > 0:
        return {"action": "skipped_products_present"}

    signal = identify_widget(html)
    if signal is None:
        return {"action": "detected_unidentified_widget"}

    try:
        task = _task_payload(signal, page_url, proxyless=proxyless, min_score=min_score)
        token = client.solve_and_wait(task)
        return {"action": "solved", "captcha_type": signal.captcha_type.value, "token": token}
    except TwoCaptchaAuthError as exc:
        return {"action": "warning_no_key", "detail": str(exc)}
    except TwoCaptchaError as exc:
        return {"action": "warning_solver_error", "detail": str(exc)}


def build_injection_script(captcha_type: CaptchaType, solution: str) -> Optional[str]:
    """Build (never run) the JavaScript that writes a solved captcha back
    into the page, for an engine to hand to its own page.evaluate /
    execute_script. See the module docstring for the honesty caveat that
    applies to every branch here: each uses that widget's own standard,
    publicly-documented integration convention, NOT anything confirmed
    against a real family-site capture. `solution` is `result["token"]`
    from `solve_when_blocked()` — a plain string for
    Turnstile/reCAPTCHA-v2/hCaptcha, a JSON-encoded solution dict for
    GeeTest v3/v4 (see `scraper_api_client.solve_and_wait`'s docstring).

    Returns `None` for reCAPTCHA v3 (see module docstring — no generic,
    site-agnostic injection point exists for it) and for any type this
    function doesn't recognize, rather than build JS that would silently
    do nothing.
    """
    token_js = json.dumps(solution)

    if captcha_type == CaptchaType.CLOUDFLARE_TURNSTILE:
        return f"""(function() {{
  var token = {token_js};
  var el = document.querySelector('[name="cf-turnstile-response"]') || document.getElementById('cf-turnstile-response');
  if (el) {{ el.value = token; el.dispatchEvent(new Event('change', {{bubbles: true}})); }}
  var widget = document.querySelector('.cf-turnstile');
  var cb = widget && widget.getAttribute('data-callback');
  if (cb && typeof window[cb] === 'function') {{ window[cb](token); }}
  return !!el || !!cb;
}})();"""

    if captcha_type == CaptchaType.RECAPTCHA_V2:
        return f"""(function() {{
  var token = {token_js};
  var el = document.getElementById('g-recaptcha-response') || document.querySelector('[name="g-recaptcha-response"]');
  if (el) {{ el.style.display = ''; el.value = token; el.dispatchEvent(new Event('change', {{bubbles: true}})); }}
  var widget = document.querySelector('.g-recaptcha');
  var cb = widget && widget.getAttribute('data-callback');
  if (cb && typeof window[cb] === 'function') {{ window[cb](token); }}
  return !!el || !!cb;
}})();"""

    if captcha_type == CaptchaType.HCAPTCHA:
        return f"""(function() {{
  var token = {token_js};
  var el = document.querySelector('[name="h-captcha-response"]') || document.getElementById('h-captcha-response');
  if (el) {{ el.value = token; el.dispatchEvent(new Event('change', {{bubbles: true}})); }}
  var widget = document.querySelector('.h-captcha');
  var cb = widget && widget.getAttribute('data-callback');
  if (cb && typeof window[cb] === 'function') {{ window[cb](token); }}
  return !!el || !!cb;
}})();"""

    if captcha_type == CaptchaType.GEETEST_V3:
        # 2Captcha's GeeTestTask(Proxyless) response fields are named
        # challenge/validate/seccode (confirmed against their API
        # reference); the page-side hidden fields most public GeeTest v3
        # integrations read are conventionally named with a "geetest_"
        # prefix — both spellings are tried since 2Captcha's own examples
        # are inconsistent about which one a given site expects.
        return f"""(function() {{
  var sol = JSON.parse({token_js});
  var fields = {{
    geetest_challenge: sol.challenge || sol.geetest_challenge,
    geetest_validate: sol.validate || sol.geetest_validate,
    geetest_seccode: sol.seccode || sol.geetest_seccode
  }};
  var setAny = false;
  Object.keys(fields).forEach(function(name) {{
    var val = fields[name];
    if (val == null) return;
    var el = document.getElementsByName(name)[0] || document.getElementById(name);
    if (el) {{ el.value = val; el.dispatchEvent(new Event('change', {{bubbles: true}})); setAny = true; }}
  }});
  return setAny;
}})();"""

    if captcha_type == CaptchaType.GEETEST_V4:
        # No standard hidden-field convention is publicly documented for
        # v4 the way v3 has one — 2Captcha's own docs say to pass the
        # solution to "the page's geetest_validate callback", without a
        # fixed name. This is the weakest-confidence branch here: it
        # stashes the solution where page code COULD read it, dispatches a
        # custom event, and calls one plausible global callback name IF
        # present — none of which is confirmed for any real site.
        return f"""(function() {{
  var sol = JSON.parse({token_js});
  window.__2captcha_geetest_v4_solution = sol;
  try {{ document.dispatchEvent(new CustomEvent('geetest_v4_solved', {{detail: sol}})); }} catch (e) {{}}
  if (typeof window.geetest_validate_callback === 'function') {{
    window.geetest_validate_callback(sol);
    return true;
  }}
  return false;
}})();"""

    return None
