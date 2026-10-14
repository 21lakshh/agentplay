#!/usr/bin/env python3
"""CI guard: fail if private references or credentials appear in tracked files.

Flags patterns that are wrong whatever the file: credential formats, ticket
ids, private document links, cross-repo PR references, personal home paths
and real-looking email addresses.
"""

import re
import subprocess
import sys

# Prefixes that look like ticket ids but are standard names (UTF-8, SHA-256...).
TICKET_ALLOW = {
    "UTF", "SHA", "GPT", "ISO", "CVE", "PEP", "RFC", "AES", "RSA", "TLS", "SSL", "MD", "HTTP",
    "ASCII", "BASE", "TCP", "UDP", "CRC", "EC", "ED", "GB", "KB", "MB", "TB", "PNG", "JPEG",
    "WEBP", "UTC", "GMT", "IEEE", "ECMA", "WCAG", "NFC", "NFD", "NFKC", "NFKD", "HS", "RS",
    "ES", "PS", "SM", "ARM", "X", "TS", "JS", "CP", "IBM", "SJIS", "EUC", "GBK", "BIG", "KOI",
    "AMD", "MP", "H", "VP", "AV", "OP", "OS", "P", "T", "E", "HTML", "CSS", "ICO", "AWS",
    "ACME",  # placeholder tracker prefix used in fixtures
}
OK_EMAIL = re.compile(
    r"(?:^info@entelligence\.ai$)"  # the project's published contact
    r"|@(?:example\.(?:com|org|net)|[\w.-]*acme[\w.-]*|[a-z]\.co|company\.com|github\.com"
    r"|users\.noreply\.github\.com|noreply\.github\.com|anthropic\.com)$",
    re.I,
)
EMAIL = re.compile(r"(?<![:/\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b")
HOME_OK = {"dev", "you", "ada", "me", "runner", "user", "name", "x", "username", "yourname"}
HOME = re.compile(r"/Users/([A-Za-z0-9._-]+)/")
# The same home as a Claude Code project folder name (-Users-<name>-repo) and
# a default macOS hostname (<name>s-MacBook-Pro.local).
SLUG = re.compile(r"(?<![\w-])-Users-([A-Za-z0-9._]+)-")
MAC_HOST = re.compile(r"\b([A-Za-z0-9]+)s-(?:MacBook|MBP|iMac|Mac-mini)", re.I)
TICKET = re.compile(r"\b([A-Z]{2,5})-\d{2,5}\b")

PATTERNS = {
    "private document link": re.compile(r"claude\.ai/(?:code/)?artifact/(?!AbCdEf)[A-Za-z0-9-]{8,}"),
    "cross-repo PR ref": re.compile(r"(?<!acme/)\b\w+#\d{3,5}\b"),
    "issue tracker link": re.compile(r"linear\.app"),
    "aws key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "api key": re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{24,}\b"),
}
SKIP = ("scripts/check_clean.py", "pnpm-lock.yaml", "package-lock.json", "uv.lock")
BINARY = (".png", ".jpg", ".gif", ".woff", ".woff2", ".ico", ".gz")

def scan_line(line: str) -> list[str]:
    """Return a label for every problem found in one line."""
    found = []
    for label, pat in PATTERNS.items():
        if pat.search(line):
            found.append(label)
    if any(m.group(1) not in TICKET_ALLOW for m in TICKET.finditer(line)):
        found.append("ticket id")
    homes = [m.group(1) for pat in (HOME, SLUG, MAC_HOST) for m in pat.finditer(line)]
    if any(h.lower() not in HOME_OK for h in homes):
        found.append("personal home path")
    if any(not OK_EMAIL.search(m.group(0)) for m in EMAIL.finditer(line)):
        found.append("real-looking email")
    return found


def main() -> int:
    files = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"], capture_output=True, text=True, check=True
    ).stdout.split("\n")
    bad = 0
    for f in filter(None, files):
        if f.endswith(SKIP) or f.endswith(BINARY):
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                text = fh.read()
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for label in scan_line(line):
                print(f"{f}:{n}: {label}")
                bad += 1
    if bad:
        print(f"\n{bad} problem(s). Remove them before committing.")
        return 1
    print("clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
