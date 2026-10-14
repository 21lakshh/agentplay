"""The optional email signup: local state, and the package's only network call.

When someone types their email into the viewer's welcome card, the local server
sends it to the signup endpoint with :func:`send`. That is the one place the
package talks to the network, and this is the one file that imports a network
client. ``grep -rn urllib.request python/src/`` finds it and nothing else.

What is sent is the event (``signup`` or ``disconnect``), the email, a random
install id and the package version. No session data, no paths, no repo names.

Nothing is sent when ``DO_NOT_TRACK`` or ``AGENTPLAY_NO_NETWORK`` is set, or when
no endpoint is configured; the viewer then skips the card entirely.

State lives in ``~/.agentplay/account.json``::

    {"status": "signed_up" | "skipped", "email": "...", "installId": "...", "at": "..."}
"""

from __future__ import annotations

import contextlib
import http.client
import ipaddress
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import __version__
from .store import _atomic_write, root

# The endpoint the welcome card posts to: a Google Apps Script web app that
# appends a row to a Sheet. AGENTPLAY_SIGNUP_URL overrides it (for testing).
# To change the script, edit this deployment rather than making a new one, or
# released copies keep posting here. See docs/signup-endpoint.md.
DEFAULT_SIGNUP_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbzmLDzEVW3e25b2Zc96sd7uLoKLGGCTv1h52S7IXLOZV64tmm4AV_85c7U92eCDGM5o/exec"
)
TIMEOUT_S = 8
# The only hosts an email may go to over https: a Google Apps Script web app
# (which answers from googleusercontent after a redirect) or Entelligence.
TRUSTED_HOSTS = ("script.google.com", "script.googleusercontent.com", "entelligence.ai")
# \ufeff too: JavaScript's \s matches it and Python's does not, and the signup
# endpoint (docs/signup-endpoint.md) checks with this same pattern in JavaScript.
_EMAIL = re.compile(r"^[^@\s\ufeff]{1,64}@[^@\s\ufeff]+\.[^@\s\ufeff]{2,}$")
_OFF = {"", "0", "false", "no", "off"}


def path() -> Path:
    return root() / "account.json"


def signup_url() -> str:
    return (os.environ.get("AGENTPLAY_SIGNUP_URL") or DEFAULT_SIGNUP_URL).strip()


def _trusted_https(parts: Any, host: str) -> bool:
    return parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in TRUSTED_HOSTS)


def _local_http(parts: Any, host: str) -> bool:
    if parts.scheme != "http":
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def allowed_url(url: str, *, local: bool = True) -> bool:
    """``https`` to a trusted host, or ``http`` to this machine (the local stand-in).

    The URL comes from ``DEFAULT_SIGNUP_URL`` or the user's own environment, never
    from a request. Even so, nothing else is reachable: urllib also speaks
    ``file://`` and ``ftp://``, and an arbitrary https host could be an internal
    service. Checked again on every redirect. ``local=False`` refuses the http
    stand-in too.
    """
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if not host or parts.username or parts.password:
        return False
    return _trusted_https(parts, host) or (local and _local_http(parts, host))


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to a URL :func:`allowed_url` accepts.

    A redirect may land on this machine only from a URL that was already there
    (a local test endpoint): the real endpoint answering with a redirect to
    ``http://127.0.0.1`` must not turn into a request to a local service.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201 - urllib's signature
        came_from_local = allowed_url(req.full_url) and not allowed_url(req.full_url, local=False)
        if not allowed_url(newurl, local=came_from_local):
            raise urllib.error.HTTPError(newurl, code, "redirect to an untrusted host", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_CheckedRedirects)


def network_disabled() -> bool:
    """True when the user has asked for no network at all, by either convention."""
    return any(
        (os.environ.get(var) or "").strip().lower() not in _OFF for var in ("DO_NOT_TRACK", "AGENTPLAY_NO_NETWORK")
    )


def valid_email(email: str) -> bool:
    return len(email) <= 254 and bool(_EMAIL.match(email))


def load() -> dict[str, Any]:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, Any]) -> None:
    _atomic_write(path(), json.dumps(data, indent=2).encode("utf-8"))


def _install_id(state: dict[str, Any]) -> str:
    iid = state.get("installId")
    return iid if isinstance(iid, str) and iid else uuid.uuid4().hex


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def public_state() -> dict[str, Any]:
    """What the viewer needs to decide whether to show the card."""
    state = load()
    status = state.get("status") if state.get("status") in ("signed_up", "skipped") else "none"
    return {
        "status": status,
        "email": state.get("email") if status == "signed_up" else None,
        # The card is only offered when an email could actually be delivered.
        "enabled": allowed_url(signup_url()) and not network_disabled(),
    }


def send(event: str, email: str, install_id: str) -> None:
    """POST one event to the signup endpoint. Raises ``OSError`` if it can't be delivered."""
    url = signup_url()
    if not url or network_disabled():
        raise OSError("signup is turned off")
    if not allowed_url(url):
        raise OSError("signup URL must be https (or http to this machine)")
    body = json.dumps({"event": event, "email": email, "installId": install_id,
                       "version": __version__, "at": _now()}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": f"agentplay/{__version__}"})
    # A Google Apps Script web app answers a POST with a redirect to its output;
    # urllib follows it as a GET, the same as `curl -L`. The row is written by
    # the POST either way.
    try:
        with _opener.open(req, timeout=TIMEOUT_S) as resp:
            if resp.status >= 400:
                raise OSError(f"signup endpoint answered {resp.status}")
            answer = resp.read(4096)
    except http.client.HTTPException as exc:  # a malformed answer is a failed delivery too
        raise OSError(f"signup endpoint answered badly: {exc}") from exc
    # Apps Script reports a crashed script as a 200 HTML error page, so the
    # status alone proves nothing. Only an explicit {"ok": true} counts.
    try:
        ok = json.loads(answer).get("ok") is True
    except (ValueError, AttributeError, RecursionError):
        ok = False
    if not ok:
        raise OSError("signup endpoint did not confirm the row was written")


def sign_up(email: str) -> dict[str, Any]:
    """Deliver the email, then remember it. Nothing is stored if delivery fails."""
    state = load()
    iid = _install_id(state)
    send("signup", email, iid)
    _save({"status": "signed_up", "email": email, "installId": iid, "at": _now()})
    return public_state()


def skip() -> dict[str, Any]:
    state = load()
    _save({"status": "skipped", "installId": _install_id(state), "at": _now()})
    return public_state()


def disconnect() -> dict[str, Any]:
    """Forget the email locally and ask the endpoint to drop it.

    The local copy goes even when the endpoint can't be reached: disconnecting
    must never fail on this machine. The card is not shown again.
    """
    state = load()
    iid = _install_id(state)
    email = state.get("email")
    if state.get("status") == "signed_up" and isinstance(email, str):
        # Anything the endpoint does must not keep the email on this machine.
        with contextlib.suppress(Exception):
            send("disconnect", email, iid)
    _save({"status": "skipped", "installId": iid, "at": _now()})
    return public_state()
