"""Proxy mode of the fetcher (Shuo, week 5): pages go through an HTTP CONNECT tunnel."""
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from wikidata_discover import fetcher


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/moved":
            self.send_response(302)
            self.send_header("Location", self.headers.get("X-Redirect-To", "http://127.0.0.1/"))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = f"<html><body>School of Art at {self.headers['Host']}</body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class FakeProxy:
    """Answers CONNECT with `status`; on 200 it relays the tunnel to a local page server,
    whatever host was asked for. `silent` never answers. Records each CONNECT target."""

    def __init__(self, status=200, silent=False):
        self.status, self.silent, self.targets = status, silent, []
        self.pages = ThreadingHTTPServer(("127.0.0.1", 0), _Page)
        threading.Thread(target=self.pages.serve_forever, daemon=True).start()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.address = ("127.0.0.1", self.sock.getsockname()[1])
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = client.recv(1024)
            if not chunk:
                return
            data += chunk
        self.targets.append(data.split(b"\r\n", 1)[0].decode())
        if self.silent:
            time.sleep(5)
            client.close()
            return
        if self.status != 200:
            client.sendall(f"HTTP/1.1 {self.status} No\r\nContent-Length: 0\r\n\r\n".encode())
            client.close()
            return
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        upstream = socket.create_connection(self.pages.server_address)

        def pipe(a, b):
            try:
                while True:
                    chunk = a.recv(65536)
                    if not chunk:
                        break
                    b.sendall(chunk)
            except OSError:
                pass
            finally:
                for s in (a, b):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
        threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()
        pipe(client, upstream)

    def close(self):
        self.sock.close()
        self.pages.shutdown()


@pytest.fixture
def proxy():
    p = FakeProxy()
    yield p
    p.close()


def no_dns(*a, **k):
    raise AssertionError("proxy mode must not resolve names locally")


def test_proxy_from_env_reads_the_setting_and_https_proxy():
    assert fetcher.proxy_from_env({}) is None
    assert fetcher.proxy_from_env({"HTTPS_PROXY": "http://127.0.0.1:34719"}) == ("127.0.0.1", 34719)
    assert fetcher.proxy_from_env({"https_proxy": "127.0.0.1:8080"}) == ("127.0.0.1", 8080)
    assert fetcher.proxy_from_env({"HTTPS_PROXY": "http://127.0.0.1:1", "ACADEMIABOT_FETCH_PROXY": "off"}) is None
    assert fetcher.proxy_from_env({"ACADEMIABOT_FETCH_PROXY": "http://10.0.0.5:3128"}) == ("10.0.0.5", 3128)
    assert fetcher.proxy_from_env({"HTTPS_PROXY": "socks5://127.0.0.1:1080"}) is None


def test_a_page_comes_through_the_tunnel_with_no_local_name_lookup(proxy):
    r = fetcher.fetch("http://www.uni.example.edu/schools", proxy=proxy.address, resolve=no_dns)
    assert r.outcome == "ok" and r.status == 200
    assert b"School of Art at www.uni.example.edu" in r.content
    assert proxy.targets == ["CONNECT www.uni.example.edu:80 HTTP/1.1"]
    assert r.hops == [("http://www.uni.example.edu/schools", f"proxy 127.0.0.1:{proxy.address[1]}")]


@pytest.mark.parametrize("url", ["http://localhost/", "http://LOCALHOST./x", "http://metadata.google.internal/",
                                 "http://printer.local/", "http://intranet/", "http://127.0.0.1/",
                                 "http://169.254.169.254/latest", "http://[::1]/", "http://10.1.2.3/"])
def test_local_names_and_non_public_addresses_are_refused_without_asking_the_proxy(proxy, url):
    r = fetcher.fetch(url, proxy=proxy.address, resolve=no_dns)
    assert r.outcome == "refused"
    assert proxy.targets == []


def test_a_redirect_to_a_non_public_address_is_refused_in_proxy_mode(proxy):
    r = fetcher.fetch("http://www.uni.example.edu/moved", proxy=proxy.address, resolve=no_dns)
    assert r.outcome == "refused" and r.detail.startswith("redirect 1: ")
    assert len(proxy.targets) == 1


@pytest.mark.parametrize("status,outcome", [(502, "error"), (504, "error"), (403, "refused"), (407, "refused")])
def test_a_proxy_that_cannot_reach_or_refuses_the_host(status, outcome):
    p = FakeProxy(status=status)
    try:
        r = fetcher.fetch("https://no-such-host.example.edu/", proxy=p.address, resolve=no_dns)
    finally:
        p.close()
    assert r.outcome == outcome
    assert f"CONNECT {status}" in r.detail
    assert r.content is None


def test_a_proxy_that_never_answers_is_stopped_at_the_time_limit():
    p = FakeProxy(silent=True)
    try:
        t0 = time.monotonic()
        r = fetcher.fetch("https://slow.example.edu/", proxy=p.address, resolve=no_dns, max_seconds=1.0)
        took = time.monotonic() - t0
    finally:
        p.close()
    assert r.outcome == "timeout"
    assert took < 3


def test_tests_fetch_directly_even_when_https_proxy_is_set():
    assert fetcher.proxy_from_env() is None
