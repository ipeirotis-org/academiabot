"""The page fetcher's safety rules (SPECS.md, Anya week 4). One test per rule.

No test reaches the internet. Tests that need real sockets use a local server on
127.0.0.1 and patch is_public to accept that one address; every other test fakes the
DNS answer and the connection and checks what the fetcher tried to do.
"""
import socket
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from wikidata_discover import fetcher, run_log

PUBLIC = "93.184.216.34"


def answer(ip):
    """A getaddrinfo-shaped answer with one address."""
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(fam, socket.SOCK_STREAM, 6, "", (ip, 80))]


def never_connect(*a, **kw):
    raise AssertionError("the fetcher must not connect")


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b"<html>ok</html>"):
        self.status, self._headers, self._body = status, headers or {}, body

    def getheader(self, name, default=None):
        return self._headers.get(name, default)

    def read1(self, n):
        chunk, self._body = self._body[:n], self._body[n:]
        return chunk


class FakeConnection:
    """Records every connection the fetcher opens: (scheme, host, port, ip)."""

    def __init__(self, responses):
        self.responses, self.opened = list(responses), []
        self.sock = None

    def __call__(self, scheme, host, port, ip, timeout):
        self.opened.append((scheme, host, port, ip))
        return self

    def request(self, *a, **kw):
        pass

    def getresponse(self):
        return self.responses.pop(0)

    def close(self):
        pass


# ── rule 1: only http and https ─────────────────────────────────────────

@pytest.mark.parametrize("url", ["ftp://example.org/a", "file:///etc/passwd", "javascript:alert(1)",
                                 "data:text/html,hi", "gopher://example.org/", "example.org/no-scheme",
                                 "http://user:pw@example.org/"])
def test_only_http_and_https_urls_are_fetched(url):
    r = fetcher.fetch(url, resolve=lambda *a: answer(PUBLIC), connect=never_connect)
    assert r.outcome == "refused" and r.content is None and r.hops == []


# ── rule 2: non-public addresses are refused ────────────────────────────

@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254",
                                "0.0.0.0", "100.64.0.1", "224.0.0.1", "240.0.0.1", "255.255.255.255",
                                "::1", "fe80::1", "fd00::1", "::ffff:10.0.0.1", "::ffff:169.254.169.254",
                                "2002:a00:1::", "::", "not-an-ip"])
def test_non_public_addresses_are_not_public(ip):
    assert fetcher.is_public(ip) is False


@pytest.mark.parametrize("ip", ["8.8.8.8", PUBLIC, "2001:4860:4860::8888"])
def test_public_addresses_are_public(ip):
    assert fetcher.is_public(ip) is True


@pytest.mark.parametrize("url", ["http://localhost/", "http://169.254.169.254/latest/meta-data/",
                                 "http://127.0.0.1:8080/", "http://[::1]/", "http://10.0.0.5/admin"])
def test_localhost_metadata_and_private_urls_are_refused_without_connecting(url):
    # The real resolver: these names and literals never leave this machine.
    r = fetcher.fetch(url, connect=never_connect)
    assert r.outcome == "refused" and "non-public" in r.detail and r.hops == []


def test_a_name_with_any_private_answer_is_refused():
    both = answer(PUBLIC) + answer("10.0.0.7")
    r = fetcher.fetch("http://mixed.example/", resolve=lambda *a: both, connect=never_connect)
    assert r.outcome == "refused" and "10.0.0.7" in r.detail


@pytest.mark.parametrize("target", ["http://169.254.169.254/latest/meta-data/", "http://localhost/",
                                    "http://inside.example/"])
def test_a_redirect_to_a_non_public_address_is_refused_before_connecting(target):
    def resolve(host, *a):
        return answer({"inside.example": "10.9.9.9", "localhost": "127.0.0.1"}.get(host, PUBLIC))
    conn = FakeConnection([FakeResponse(302, {"Location": target})])
    r = fetcher.fetch("https://public.example/start", resolve=resolve, connect=conn)
    assert r.outcome == "refused" and r.detail.startswith("redirect 1:")
    assert [o[3] for o in conn.opened] == [PUBLIC]           # only the public first hop was contacted
    assert r.final_url == target and r.content is None


# ── rule 3: at most 5 redirects ─────────────────────────────────────────

def test_five_redirects_are_followed_and_a_sixth_is_not():
    hops = [FakeResponse(301, {"Location": f"/step{i}"}) for i in range(1, 6)]
    ok = FakeConnection(hops + [FakeResponse(200)])
    assert fetcher.fetch("http://a.example/", resolve=lambda *a: answer(PUBLIC), connect=ok).outcome == "ok"
    assert len(ok.opened) == 6

    hops = [FakeResponse(302, {"Location": f"/step{i}"}) for i in range(1, 7)]
    too_many = FakeConnection(hops + [FakeResponse(200)])
    r = fetcher.fetch("http://a.example/", resolve=lambda *a: answer(PUBLIC), connect=too_many)
    assert r.outcome == "too_many_redirects" and len(too_many.opened) == 6 and r.content is None
    assert fetcher.MAX_REDIRECTS == 5


def test_every_redirect_is_resolved_and_checked_again():
    asked = []

    def resolve(host, *a):
        asked.append(host)
        return answer(PUBLIC)
    conn = FakeConnection([FakeResponse(302, {"Location": "http://b.example/x"}), FakeResponse(200)])
    r = fetcher.fetch("http://a.example/", resolve=resolve, connect=conn)
    assert r.outcome == "ok" and asked == ["a.example", "b.example"] and r.final_url == "http://b.example/x"


# ── real local server for size, time, and the pinned connection ─────────

class Handler(BaseHTTPRequestHandler):
    routes = {}
    seen_hosts = []

    def do_GET(self):
        Handler.seen_hosts.append(self.headers.get("Host"))
        Handler.routes[self.path](self)

    def log_message(self, *a):
        pass


@pytest.fixture
def server(monkeypatch):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    # Only inside these tests: the local test server counts as public.
    monkeypatch.setattr(fetcher, "is_public", lambda ip: ip == "127.0.0.1")
    Handler.routes, Handler.seen_hosts = {}, []
    yield httpd.server_address[1]
    httpd.shutdown()


def send(body: bytes, status=200, delay=0.0):
    def route(h):
        h.send_response(status)
        h.send_header("Content-Type", "text/html")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        if delay:
            for i in range(0, len(body), 1):
                h.wfile.write(body[i:i + 1])
                h.wfile.flush()
                time.sleep(delay)
        else:
            h.wfile.write(body)
    return route


# ── rule 4: at most 5 MB ────────────────────────────────────────────────

def test_a_page_over_5_mb_is_not_saved_and_one_of_5_mb_is(server):
    Handler.routes["/big"] = send(b"x" * (fetcher.MAX_BYTES + 1))
    Handler.routes["/max"] = send(b"x" * fetcher.MAX_BYTES)
    big = fetcher.fetch(f"http://127.0.0.1:{server}/big")
    assert big.outcome == "too_large" and big.content is None and big.status == 200
    full = fetcher.fetch(f"http://127.0.0.1:{server}/max")
    assert full.outcome == "ok" and len(full.content) == fetcher.MAX_BYTES
    assert fetcher.MAX_BYTES == 5 * 1024 * 1024


# ── rule 5: at most 20 seconds ──────────────────────────────────────────

def test_a_server_that_drips_bytes_is_stopped_at_the_time_limit(server):
    Handler.routes["/slow"] = send(b"y" * 200, delay=0.05)          # 200 bytes over 10 seconds
    t0 = time.monotonic()
    r = fetcher.fetch(f"http://127.0.0.1:{server}/slow", max_seconds=1.0)
    assert r.outcome == "timeout" and r.content is None and time.monotonic() - t0 < 3
    assert fetcher.MAX_SECONDS == 20


def test_a_server_that_never_answers_is_stopped_at_the_time_limit(server):
    Handler.routes["/hang"] = lambda h: time.sleep(5)
    t0 = time.monotonic()
    r = fetcher.fetch(f"http://127.0.0.1:{server}/hang", max_seconds=1.0)
    assert r.outcome == "timeout" and time.monotonic() - t0 < 3


def test_the_time_limit_covers_all_redirects_together():
    clock = iter([0.0, 5.0, 10.0, 15.0, 21.0, 30.0, 40.0]).__next__
    hops = [FakeResponse(302, {"Location": f"/r{i}"}) for i in range(5)]
    r = fetcher.fetch("http://a.example/", resolve=lambda *a: answer(PUBLIC), connect=FakeConnection(hops),
                      clock=clock)
    assert r.outcome == "timeout"



def test_a_dns_lookup_that_hangs_is_stopped_at_the_time_limit():
    """The lookup may use only the time that is left. Its late answer is never used."""
    release = threading.Event()

    def hangs(host, *a):
        release.wait(10)
        return answer(PUBLIC)
    t0 = time.monotonic()
    try:
        r = fetcher.fetch("http://slow-dns.example/", resolve=hangs, connect=never_connect, max_seconds=1.0)
    finally:
        release.set()
    assert time.monotonic() - t0 < 2
    assert r.outcome == "timeout" and "DNS lookup" in r.detail and r.hops == [] and r.content is None


def test_a_redirect_whose_dns_lookup_hangs_is_stopped_at_the_time_limit():
    release = threading.Event()

    def resolve(host, *a):
        if host == "b.example":
            release.wait(10)
        return answer(PUBLIC)
    conn = FakeConnection([FakeResponse(302, {"Location": "http://b.example/next"})])
    t0 = time.monotonic()
    try:
        r = fetcher.fetch("http://a.example/", resolve=resolve, connect=conn, max_seconds=1.0)
    finally:
        release.set()
    assert time.monotonic() - t0 < 2
    assert r.outcome == "timeout" and r.detail.startswith("redirect 1: ") and "DNS lookup" in r.detail
    assert conn.opened == [("http", "a.example", 80, PUBLIC)]          # b.example: no connection
    assert r.final_url == "http://b.example/next"


def test_a_server_that_sends_headers_slowly_is_stopped_at_the_time_limit(server):
    """Each header byte arrives well inside a per-read timeout, but all of them together
    take 5 s. The budget covers the headers too, so the fetch ends after 1 s."""
    def slow_headers(h):
        head = (b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nX-Pad: " + b"a" * 200
                + b"\r\nContent-Length: 2\r\n\r\nok")
        try:
            for i in range(len(head)):
                h.wfile.write(head[i:i + 1])
                h.wfile.flush()
                time.sleep(0.02)
        except OSError:
            pass                                          # the fetcher hung up, as it should
    Handler.routes["/slow-headers"] = slow_headers
    t0 = time.monotonic()
    r = fetcher.fetch(f"http://127.0.0.1:{server}/slow-headers", max_seconds=1.0)
    assert time.monotonic() - t0 < 2
    assert r.outcome == "timeout" and r.status is None and r.content is None


def test_a_tls_handshake_sent_slowly_is_stopped_at_the_time_limit(monkeypatch):
    """The server announces a long TLS record and sends it a byte at a time. HTTPS reads
    after the handshake use the same rule as plain HTTP reads (_DeadlineSSLSocket)."""
    assert fetcher._ssl_context().sslsocket_class is fetcher._DeadlineSSLSocket
    monkeypatch.setattr(fetcher, "is_public", lambda ip: ip == "127.0.0.1")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def drip():
        conn, _ = listener.accept()
        try:
            conn.recv(4096)                               # the client hello
            conn.sendall(b"\x16\x03\x03\x40\x00")           # a handshake record of 16 kB
            for _ in range(250):
                conn.sendall(b"\x02")
                time.sleep(0.02)
        except OSError:
            pass
        finally:
            conn.close()
    threading.Thread(target=drip, daemon=True).start()
    t0 = time.monotonic()
    try:
        r = fetcher.fetch(f"https://127.0.0.1:{listener.getsockname()[1]}/", max_seconds=1.0)
    finally:
        listener.close()
    assert time.monotonic() - t0 < 2
    assert r.outcome == "timeout" and r.status is None

# ── rule 6: the connection goes to the address that was checked ─────────

def test_dns_rebinding_cannot_reach_the_private_address():
    """The name answers public at the check and private afterwards. The fetcher resolves
    once and connects to the checked public address; it never asks DNS again."""
    calls = []

    def rebinding(host, *a):
        calls.append(host)
        return answer(PUBLIC if len(calls) == 1 else "127.0.0.1")
    conn = FakeConnection([FakeResponse(200)])
    r = fetcher.fetch("http://rebind.example/", resolve=rebinding, connect=conn)
    assert r.outcome == "ok" and calls == ["rebind.example"]
    assert conn.opened == [("http", "rebind.example", 80, PUBLIC)] and r.hops == [("http://rebind.example/", PUBLIC)]


def test_the_real_connection_uses_the_checked_address_and_never_resolves_again(server, monkeypatch):
    """With real sockets: DNS is switched off after the check, so a connection that looked
    the name up again would fail. The page still arrives, from the checked address, and
    the server sees the original name in the Host header."""
    Handler.routes["/page"] = send(b"<html>Department of Finance</html>")

    def no_dns(*a, **kw):
        raise AssertionError("the connection resolved the name again")
    resolve_once = lambda host, *a: answer("127.0.0.1")               # the check's one answer
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    r = fetcher.fetch(f"http://rebind.test:{server}/page", resolve=resolve_once)
    assert r.outcome == "ok" and b"Finance" in r.content
    assert Handler.seen_hosts == [f"rebind.test:{server}"]


def test_https_verifies_the_certificate_against_the_name_not_the_address(monkeypatch):
    seen = {}

    class Ctx:
        def wrap_socket(self, sock, server_hostname=None):
            seen["sni"] = server_hostname
            return types.SimpleNamespace()

    def connect_ip(ip, port, timeout, deadline=None):
        seen["addr"] = (ip, port)
    monkeypatch.setattr(fetcher, "_ssl_context", lambda: Ctx())
    monkeypatch.setattr(fetcher, "_connect_ip", connect_ip)
    conn = fetcher.open_connection("https", "www.stern.nyu.edu", 443, PUBLIC, 5)
    conn.connect()
    assert seen == {"addr": (PUBLIC, 443), "sni": "www.stern.nyu.edu"}


# ── rule 7: a refused URL is recorded as refused ────────────────────────

def test_a_refused_url_is_recorded_as_refused_with_its_reason(tmp_path):
    log = run_log.RunLog("fetch-test", results_dir=tmp_path, enabled=False, bucket=None)
    log.gcs_error = "no bucket in tests"                                # texts go to local files
    r = fetcher.fetch("http://169.254.169.254/latest/meta-data/", connect=never_connect)
    eid = fetcher.record(log, r, ["Finance"])
    row = log.pending["evidence"][0]
    assert row["evidence_id"] == eid and row["outcome"] == "refused" and "non-public" in row["detail"]
    assert row["http_status"] is None and row["snapshot_path"] is None and row["content_hash"] is None
    assert row["unit_names"] == []


def test_a_fetched_page_is_saved_with_status_final_url_hash_and_text(tmp_path):
    log = run_log.RunLog("fetch-test", results_dir=tmp_path, enabled=False)
    log.gcs_error = "no bucket in tests"
    conn = FakeConnection([FakeResponse(301, {"Location": "/departments"}),
                           FakeResponse(200, {"Content-Type": "text/html"},
                                        b"<html><script>x()</script><p>Department of Finance &amp; Banking</p></html>")])
    r = fetcher.fetch("http://stern.example/", resolve=lambda *a: answer(PUBLIC), connect=conn)
    fetcher.record(log, r, ["Finance & Banking", "Marketing"])
    row = log.pending["evidence"][0]
    assert (row["outcome"], row["http_status"], row["final_url"]) == ("ok", 200, "http://stern.example/departments")
    assert row["content_hash"] and row["snapshot_path"] and row["unit_names"] == ["Finance & Banking"]
    assert (tmp_path / "runs" / "fetch-test").exists()
    text = open(row["text_path"]).read()
    assert text == "Department of Finance & Banking"
