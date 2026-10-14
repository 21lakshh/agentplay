"""The email signup: what is sent, when nothing is sent, and who may post."""

from __future__ import annotations

import http.client
import json
import re
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from agentplay import account
from agentplay import server as server_mod
from agentplay.server import serve
from agentplay.store import Store


class _Endpoint:
    """A stand-in for the signup endpoint that records what it was sent."""

    def __init__(self, status: int = 200, answer: bytes = b'{"ok": true}') -> None:
        received: list[dict[str, Any]] = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

        self.received = received
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/exec"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture(autouse=True)
def no_real_endpoint(monkeypatch):
    """Tests never reach the real Sheet: the built-in URL is blanked for all of them."""
    monkeypatch.setattr(account, "DEFAULT_SIGNUP_URL", "")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTPLAY_HOME", str(tmp_path / "sm"))
    for var in ("AGENTPLAY_SIGNUP_URL", "DO_NOT_TRACK", "AGENTPLAY_NO_NETWORK"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path / "sm"


@pytest.fixture()
def endpoint(home, monkeypatch):
    ep = _Endpoint()
    monkeypatch.setenv("AGENTPLAY_SIGNUP_URL", ep.url)
    yield ep
    ep.close()


@pytest.fixture()
def viewer(home):
    httpd = serve(0, store=Store(home))
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    port = httpd.server_address[1]
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()


def _post(base: str, body: Any, *, origin: str | None = "same", ctype: str = "application/json") -> tuple[int, dict]:
    data = json.dumps(body).encode()
    headers = {"Content-Type": ctype}
    if origin == "same":
        headers["Origin"] = base
    elif origin:
        headers["Origin"] = origin
    req = urllib.request.Request(base + "/api/account", data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(base: str) -> dict:
    with urllib.request.urlopen(base + "/api/account") as r:
        return json.loads(r.read())


def test_card_is_off_without_an_endpoint(home, viewer):
    assert _get(viewer) == {"success": True, "status": "none", "email": None, "enabled": False}


@pytest.mark.parametrize("var", ["DO_NOT_TRACK", "AGENTPLAY_NO_NETWORK"])
def test_opt_out_turns_the_card_off_and_sends_nothing(endpoint, viewer, monkeypatch, var):
    monkeypatch.setenv(var, "1")
    assert _get(viewer)["enabled"] is False
    status, body = _post(viewer, {"action": "signup", "email": "a@b.co"})
    assert status == 502 and endpoint.received == []
    assert not account.path().exists()


def test_signup_sends_only_the_email_and_an_install_id(endpoint, viewer):
    assert _get(viewer)["enabled"] is True
    status, body = _post(viewer, {"action": "signup", "email": "dev@acme.io"})
    assert status == 200 and body["status"] == "signed_up" and body["email"] == "dev@acme.io"
    (sent,) = endpoint.received
    assert set(sent) == {"event", "email", "installId", "version", "at"}
    assert sent["event"] == "signup" and sent["email"] == "dev@acme.io"
    assert _get(viewer)["status"] == "signed_up"


def test_failed_delivery_stores_nothing(home, viewer, monkeypatch):
    down = _Endpoint(status=500)
    monkeypatch.setenv("AGENTPLAY_SIGNUP_URL", down.url)
    try:
        status, body = _post(viewer, {"action": "signup", "email": "dev@acme.io"})
    finally:
        down.close()
    assert status == 502 and body["code"] == "unreachable"
    assert _get(viewer)["status"] == "none"


def test_skip_sends_nothing_and_is_remembered(endpoint, viewer):
    status, body = _post(viewer, {"action": "skip"})
    assert status == 200 and body["status"] == "skipped"
    assert endpoint.received == []


def test_disconnect_forgets_locally_and_tells_the_endpoint(endpoint, viewer):
    _post(viewer, {"action": "signup", "email": "dev@acme.io"})
    iid = account.load()["installId"]
    status, body = _post(viewer, {"action": "disconnect"})
    assert status == 200 and body["status"] == "skipped" and body["email"] is None
    assert [e["event"] for e in endpoint.received] == ["signup", "disconnect"]
    assert endpoint.received[1]["installId"] == iid
    assert "email" not in account.load()


def test_disconnect_still_forgets_when_the_endpoint_is_down(home, viewer, monkeypatch):
    ep = _Endpoint()
    monkeypatch.setenv("AGENTPLAY_SIGNUP_URL", ep.url)
    _post(viewer, {"action": "signup", "email": "dev@acme.io"})
    ep.close()
    status, body = _post(viewer, {"action": "disconnect"})
    assert status == 200 and body["email"] is None


@pytest.mark.parametrize("email", ["", "not-an-email", "a@b", "a b@c.co", "x" * 250 + "@a.co"])
def test_rejects_bad_emails_without_sending(endpoint, viewer, email):
    status, body = _post(viewer, {"action": "signup", "email": email})
    assert status == 400 and body["code"] == "invalid_email"
    assert endpoint.received == []


@pytest.mark.parametrize("origin", [None, "https://evil.example", "http://127.0.0.1.evil.example:7317"])
def test_rejects_posts_from_other_sites(endpoint, viewer, origin):
    status, _ = _post(viewer, {"action": "signup", "email": "dev@acme.io"}, origin=origin)
    assert status == 403 and endpoint.received == []


def test_accepts_the_dev_server_origin_only_when_configured(endpoint, viewer, monkeypatch):
    status, _ = _post(viewer, {"action": "skip"}, origin="http://localhost:5173")
    assert status == 403
    monkeypatch.setenv("AGENTPLAY_DEV_ORIGINS", "http://localhost:4000, http://localhost:5173/")
    status, _ = _post(viewer, {"action": "skip"}, origin="http://localhost:5173")
    assert status == 200


def test_accepts_its_own_origin_under_any_local_name(endpoint, viewer):
    port = viewer.rsplit(":", 1)[1]
    for host in ("127.0.0.1", "localhost", "LOCALHOST"):
        status, _ = _post(viewer, {"action": "skip"}, origin=f"http://{host}:{port}")
        assert status == 200, host


@pytest.mark.parametrize("origin", ["http://localhost:1", "http://127.0.0.1:8080", "https://localhost:{port}"])
def test_rejects_other_local_ports(endpoint, viewer, origin):
    origin = origin.format(port=viewer.rsplit(":", 1)[1])
    status, _ = _post(viewer, {"action": "signup", "email": "dev@acme.io"}, origin=origin)
    assert status == 403 and endpoint.received == []


def test_rejects_form_posts(endpoint, viewer):
    status, _ = _post(viewer, {"action": "skip"}, ctype="text/plain")
    assert status == 415


def test_network_code_lives_in_one_file():
    src = Path(account.__file__).parent
    hits = sorted(p.name for p in src.rglob("*.py") if "urllib.request" in p.read_text(encoding="utf-8"))
    assert hits == ["account.py"]


def test_importing_the_package_makes_no_network_call():
    code = ("import socket\n"
            "def no(*a, **k): raise AssertionError('network')\n"
            "socket.socket.connect = no\n"
            "import agentplay.cli, agentplay.server, agentplay.account\n")
    subprocess.run([sys.executable, "-c", code], check=True)


@pytest.mark.parametrize(("url", "ok"), [
    ("https://script.google.com/macros/s/x/exec", True),
    ("https://script.googleusercontent.com/macros/echo?x=1", True),
    ("https://api.entelligence.ai/signup", True),
    ("https://example.com/exec", False),
    ("https://script.google.com.evil.example/exec", False),
    ("https://user:pw@script.google.com/exec", False),
    ("https://10.0.0.5/exec", False),
    ("http://script.google.com/exec", False),
    ("http://127.0.0.1:8787/exec", True),
    ("http://localhost:8787/exec", True),
    ("http://[::1]:8787/exec", True),
    ("http://example.com/exec", False),
    ("file:///etc/passwd", False),
    ("ftp://example.com/x", False),
    ("https:///nohost", False),
    ("", False),
])
def test_only_https_or_this_machine_is_allowed(url, ok):
    assert account.allowed_url(url) is ok


def test_a_disallowed_url_turns_the_card_off_and_sends_nothing(home, viewer, monkeypatch):
    monkeypatch.setenv("AGENTPLAY_SIGNUP_URL", "file:///etc/passwd")
    assert _get(viewer)["enabled"] is False
    status, _ = _post(viewer, {"action": "signup", "email": "dev@acme.io"})
    assert status == 502 and not account.path().exists()


def _garbage_endpoint() -> tuple[str, socket.socket]:
    """Answers every connection with a malformed status line."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()

    def loop() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                conn.recv(65536)
                conn.sendall(b"NOT-HTTP garbage\r\n\r\n")

    threading.Thread(target=loop, daemon=True).start()
    return f"http://127.0.0.1:{srv.getsockname()[1]}/exec", srv


def test_a_malformed_answer_is_a_failed_signup_and_never_blocks_disconnect(endpoint, viewer, monkeypatch):
    _post(viewer, {"action": "signup", "email": "dev@acme.io"})
    url, srv = _garbage_endpoint()
    monkeypatch.setenv("AGENTPLAY_SIGNUP_URL", url)
    try:
        status, body = _post(viewer, {"action": "disconnect"})
        assert status == 200 and body["email"] is None
        assert "email" not in account.load()
        account.skip()
        status, body = _post(viewer, {"action": "signup", "email": "dev@acme.io"})
        assert status == 502 and body["code"] == "unreachable"
    finally:
        srv.close()


def test_a_remote_client_never_sees_the_email(endpoint, viewer, monkeypatch):
    _post(viewer, {"action": "signup", "email": "dev@acme.io"})
    monkeypatch.setattr(server_mod, "_is_loopback", lambda _addr: False)
    assert _get_as(viewer, {}) == (403, FORBIDDEN)


def test_a_redirect_to_an_untrusted_host_is_a_failed_signup(home, viewer, monkeypatch):
    class R(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(302)
            self.send_header("Location", "http://example.com/steal")
            self.send_header("Content-Length", "0")
            self.end_headers()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), R)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    monkeypatch.setenv("AGENTPLAY_SIGNUP_URL", f"http://127.0.0.1:{httpd.server_address[1]}/exec")
    # Record every host a connection is opened to, and never actually reach the
    # outside: the guard must stop the redirect before any connection is made.
    contacted: list[str] = []
    real_connect = http.client.HTTPConnection.connect

    def spy(conn: http.client.HTTPConnection) -> None:
        contacted.append(conn.host)
        if conn.host not in ("127.0.0.1", "localhost"):
            raise AssertionError(f"contacted {conn.host}")
        real_connect(conn)

    monkeypatch.setattr(http.client.HTTPConnection, "connect", spy)
    try:
        status, body = _post(viewer, {"action": "signup", "email": "dev@acme.io"})
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert "example.com" not in contacted
    assert status == 502 and not account.path().exists()


@pytest.mark.parametrize(("start", "target", "ok"), [
    ("https://script.google.com/macros/s/x/exec", "https://script.googleusercontent.com/echo", True),
    ("https://script.google.com/macros/s/x/exec", "http://127.0.0.1:8080/admin", False),
    ("https://script.google.com/macros/s/x/exec", "http://localhost/admin", False),
    ("https://script.google.com/macros/s/x/exec", "https://evil.example/", False),
    ("http://127.0.0.1:9/exec", "http://localhost:9/echo", True),  # a local test endpoint
    ("http://127.0.0.1:9/exec", "https://script.googleusercontent.com/echo", True),
    ("http://127.0.0.1:9/exec", "https://evil.example/", False),
])
def test_a_redirect_lands_on_this_machine_only_from_a_local_url(start, target, ok):
    """Checked on the handler directly: nothing is opened, so nothing can leak."""
    handler = account._CheckedRedirects()
    req = urllib.request.Request(start, data=b"{}", method="POST")
    if ok:
        assert handler.redirect_request(req, None, 302, "Found", {}, target).full_url == target
        return
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(req, None, 302, "Found", {}, target)


FORBIDDEN = {"success": False, "code": "forbidden", "error": "Only the local viewer can read this."}


def _get_as(base: str, headers: dict[str, str]) -> tuple[int, dict]:
    req = urllib.request.Request(base + "/api/account", headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.mark.parametrize("headers", [
    {"Host": "evil.example:7317"},
    {"Host": "127.0.0.1.evil.example"},
    {"Origin": "https://evil.example"},
])
def test_a_rebinding_page_never_reads_the_email(endpoint, viewer, headers):
    _post(viewer, {"action": "signup", "email": "dev@acme.io"})
    assert _get_as(viewer, headers) == (403, FORBIDDEN)
    assert _get(viewer)["email"] == "dev@acme.io"


@pytest.mark.parametrize("answer", [
    b"<!DOCTYPE html><title>Error</title>TypeError: Cannot read properties of null",  # a crashed Apps Script
    b"",
    b'{"ok": false}',
    b"[]",
    b"[" * 1000 + b"]" * 1000,  # nested deep enough to make json.loads raise RecursionError
])
def test_a_200_without_ok_true_is_a_failed_signup(home, viewer, monkeypatch, answer):
    ep = _Endpoint(answer=answer)
    monkeypatch.setenv("AGENTPLAY_SIGNUP_URL", ep.url)
    try:
        status, body = _post(viewer, {"action": "signup", "email": "dev@acme.io"})
    finally:
        ep.close()
    assert status == 502 and body["code"] == "unreachable"
    assert _get(viewer)["status"] == "none"


def test_the_built_in_endpoint_is_a_trusted_https_url():
    import importlib

    fresh = importlib.reload(account)  # the module constant, before the autouse blanking
    try:
        assert fresh.DEFAULT_SIGNUP_URL.startswith("https://script.google.com/macros/s/")
        assert fresh.allowed_url(fresh.DEFAULT_SIGNUP_URL)
    finally:
        importlib.reload(account)


def test_signup_script_accepts_exactly_what_the_client_sends():
    """docs/signup-endpoint.md's Apps Script must not reject an address the CLI posts."""
    doc = Path(__file__).resolve().parents[2] / "docs" / "signup-endpoint.md"
    if not doc.exists():
        pytest.skip("docs/ is not in an unpacked sdist")
    m = re.search(r"if \(!/(.+?)/\.test\(email\)\)", doc.read_text(encoding="utf-8"))
    assert m, "email check not found in the reference script"
    assert m.group(1) == account._EMAIL.pattern
    assert account.valid_email("+foo@example.com")
    # Where Python's and JavaScript's \s differ, the client must be the stricter side
    # so it never posts an address the endpoint refuses.
    assert not account.valid_email("a\ufeff@example.com")
    assert not account.valid_email("a\u2009b@example.com")
