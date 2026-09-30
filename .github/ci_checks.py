#!/usr/bin/env python3
"""Repository-wide credential guard used by both CI and smoke_test.py.

Only paths and rule names are printed. A suspicious value is never echoed
back into a CI log, because an exception/report is a credential leak too.

Unlike tipranks-scraper (no credential model at all), this repo has a
real one: a 2Captcha API key, proxy credentials, a Scraping Browser CDP
endpoint, a Fingerprint API key. `env_config.py` is the only place any of
these are read from (`.env`, never argv — CLAUDE.md Sec.3), and
`proxy_pool.redact_credentials` is the only place a credentialed URL gets
logged or re-raised (CLAUDE.md Sec.8). This scanner is the third,
independent check: it never trusts that either of those held everywhere
they should have -- it just re-reads every committed/staged file and
looks for the shapes a real credential takes, the same way a reviewer
skimming a diff would.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ALLOWLIST = {
    ".github/ci_checks.py",  # contains the detector patterns themselves
    ".env.example",  # documents placeholder credentials verbatim, on purpose (CLAUDE.md Sec.17)
    "smoke_test.py",  # deliberate credential-shaped masking/redaction test fixtures
}

TOKEN_RULES = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    "github_token": re.compile(r"(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
}
URL_CREDENTIALS = re.compile(r"\b(?:https?|wss?)://([^\s/@:]+):([^\s/@]+)@", re.I)
# The name may itself be quoted, and JSON-escaped inside a string
# (`"api_key": "…"`, `\"api_key\": \"…\"`) — CLAUDE.md §24: the old form
# required `=`/`:` right after the bare name and missed both.
SECRET_ASSIGNMENT = re.compile(
    r"\b(?:api[_-]?key|client[_-]?key|twocaptcha[_-]?key|TWOCAPTCHA_KEY|PERPLEXITY_PROXY|"
    r"PERPLEXITY_CDP_ENDPOINT|FINGERPRINT_API_KEY|CLAUDE_CODE_OAUTH_TOKEN)"
    r"\\?['\"]?\s*(?:=|:)\s*\\?"
    r"(?:(['\"])([^'\"\\]{8,})\\?\1|([^\s#'\"\\,}]{8,}))",
    re.I,
)
# A 2Captcha key is 32 lowercase hex characters; next to a key-like word it
# is a credential whatever the assignment syntax around it.
KEYISH_HEX32 = re.compile(r"(?i)(?:key|token)\W{0,8}\b[0-9a-f]{32}\b")
PLACEHOLDER_WORDS = (
    "user", "username", "login", "pass", "password", "example", "fake",
    "secret", "redacted", "changeme", "your", "test",
)
_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_IDENTIFIER_RE = re.compile(r"[a-z_]+")


def _words(token: str) -> set[str]:
    """Split into whole alphanumeric-run words on any other character
    (`-`, `_`, `:`, ...), lowercased — never substring containment (see
    `is_placeholder`'s docstring)."""
    return {w for w in _WORD_SPLIT_RE.split(token.lower()) if w}


_PLACEHOLDER_WORD_SET = {w for phrase in PLACEHOLDER_WORDS for w in _words(phrase)}

# A virtualenv (one per engine in CI — .venv-playwright/.venv-selenium/
# .venv-puppeteer, not just .venv) or a node_modules tree carries a huge
# amount of third-party test/fixture/license text that legitimately
# contains credential-shaped strings. `.gitignore` is supposed to keep
# these out of `git ls-files --others --exclude-standard` already, but
# relying on that alone is exactly the "two independent checks for the
# same thing WILL drift apart" trap — filtering by name here too means a
# stale or incomplete .gitignore degrades to redundant, not broken.
_NOT_SOURCE_DIR = re.compile(r"(^|/)(\.venv[^/]*|venv|env|node_modules|__pycache__|\.git)(/|$)")


def _in_virtualenv(relative: str) -> bool:
    """True when any parent directory holds a `pyvenv.cfg` — a virtualenv
    under ANY name (`myenv/`, `.venv-selenium/`), not just the names the
    regex above knows (CLAUDE.md §23)."""
    parent = (ROOT / relative).parent
    while parent != ROOT and ROOT in parent.parents:
        if (parent / "pyvenv.cfg").exists():
            return True
        parent = parent.parent
    return False


def repository_files() -> list[str]:
    """`git ls-files` when ROOT is a real git working tree (true in CI's
    `actions/checkout`, and in any real clone) — a plain filesystem walk
    as a fallback otherwise (a file-only sync of this repo with no .git
    directory, or an extracted release tarball), so the scanner works
    either way rather than crashing when .git is absent."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT, check=True, capture_output=True,
        )
        paths = [p.decode("utf-8", "surrogateescape") for p in result.stdout.split(b"\0") if p]
    except (subprocess.CalledProcessError, FileNotFoundError):
        paths = [
            str(p.relative_to(ROOT))
            for p in ROOT.rglob("*")
            if p.is_file()
        ]
    return [p for p in paths if not _NOT_SOURCE_DIR.search(p) and not _in_virtualenv(p)]


def is_placeholder(user: str, password: str) -> bool:
    """True when `user`/`password` look like a documentation placeholder
    rather than a real credential. A whole-word check, not substring
    containment — `myrealuser1234:myrealpass5678` must NOT match on
    "user"/"pass" as a substring, or a real credential silently never
    gets scanned."""
    combined = f"{user}{password}"
    if "{" in combined or "}" in combined:
        return True
    return bool((_words(user) | _words(password)) & _PLACEHOLDER_WORD_SET)


_TYPE_HINT_RE = re.compile(
    r"^(?:Optional|Union|List|Dict|Tuple|Set|FrozenSet|Callable|Any|"
    r"str|int|float|bool|bytes|dict|list|tuple|set|None)\b"
)


def _looks_like_type_hint(value: str) -> bool:
    """True when an *unquoted* SECRET_ASSIGNMENT match is actually a
    Python type annotation, not a value. `api_key: Optional[str]` in a
    function signature matches the same `NAME\\s*(?::|=)\\s*<value>`
    shape a real `PERPLEXITY_PROXY: some-value` config line would (the `:`
    case exists to catch YAML/env-style assignments), and Python
    parameter names routinely happen to be exactly the words this
    scanner watches for (`api_key`, ...). Checked ONLY for the unquoted
    alternative — a real secret is never legitimately written as an
    unquoted Python expression starting with a typing keyword."""
    return bool(_TYPE_HINT_RE.match(value))


def scan_text(relative: str, text: str) -> list[tuple[str, int, str]]:
    """The rules, on one file's text — separate so a check can PLANT a
    credential and require it to be found (a scanner that cannot fail is
    not a scanner, CLAUDE.md §25)."""
    findings: list[tuple[str, int, str]] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if True:
            for rule, pattern in TOKEN_RULES.items():
                if pattern.search(line):
                    findings.append((relative, line_no, rule))
            for match in URL_CREDENTIALS.finditer(line):
                if not is_placeholder(match.group(1), match.group(2)):
                    findings.append((relative, line_no, "credentialed_url"))
            for match in SECRET_ASSIGNMENT.finditer(line):
                if match.group(3) and _looks_like_type_hint(match.group(3)):
                    continue
                value = match.group(2) or match.group(3)
                if _IDENTIFIER_RE.fullmatch(value):
                    continue  # a mapping to a Python name (env_config.ENV_KEYS), not a value
                if not is_placeholder(value, value):
                    findings.append((relative, line_no, "secret_assignment"))
            if KEYISH_HEX32.search(line):
                findings.append((relative, line_no, "hex32_key"))
    return findings


def scan() -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    for relative in repository_files():
        if relative in FIXTURE_ALLOWLIST:
            continue
        try:
            text = (ROOT / relative).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        findings.extend(scan_text(relative, text))
    return findings


def sample_check() -> int:
    """sample_output.{json,csv} carry exactly the Product columns. Lives
    here so tests.yml calls it instead of importing a local module inline
    (CLAUDE.md §26)."""
    import csv
    import json
    sys.path.insert(0, str(ROOT))
    from output_writer import PRODUCT_FIELD_NAMES
    rows = json.loads((ROOT / "sample_output.json").read_text(encoding="utf-8"))
    assert rows and set(rows[0]) == set(PRODUCT_FIELD_NAMES), "sample_output.json columns drifted"
    with (ROOT / "sample_output.csv").open(newline="", encoding="utf-8") as f:
        assert next(csv.reader(f)) == PRODUCT_FIELD_NAMES, "sample_output.csv header drifted"
    print("sample_output OK")
    return 0


def main() -> int:
    if "--sample-check" in sys.argv[1:]:
        return sample_check()
    findings = scan()
    if findings:
        for path, line, rule in findings:
            print(f"{path}:{line}: possible committed credential ({rule})")
        print("credential scan failed", file=sys.stderr)
        return 1
    print("credential scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
