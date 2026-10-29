"""Page fetcher for URLs that an LLM cited (Anya, week 4). The URLs are untrusted input.

Rules (SPECS.md, week 4; AGENTS.md, "Run log"):
- Only http and https.
- Every host is resolved once per request, and every address it resolves to must be
  public. Loopback, private, link-local (cloud metadata at 169.254.169.254), shared,
  multicast, reserved, and unspecified addresses are refused, on the first request
  and on every redirect.
- The connection goes to the exact address that was checked. The fetcher never
  resolves the name again, so a name that answers public at the check and private a
  moment later (DNS rebinding) cannot reach a private address. HTTPS still verifies
  the certificate against the host name.
- At most 5 redirects, 5 MB, and 20 seconds for the whole fetch, redirects included.
- A refused URL is recorded as refused, with the reason, and nothing is fetched from it.

fetch(url) returns a FetchResult; it never raises for a bad URL or a bad server.
"""
import http.client
import ipaddress
import os
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

MAX_REDIRECTS = 5
MAX_BYTES = 5 * 1024 * 1024
MAX_SECONDS = 20.0
REDIRECTS = (301, 302, 303, 307, 308)
USER_AGENT = "AcademiaBot-evidence/1.0 (research; https://github.com/ipeirotis-org/academiabot)"


@dataclass
class FetchResult:
    url: str                                # the URL as cited
    outcome: str                            # ok, http_error, refused, too_many_redirects, too_large, timeout, error
    detail: str = ""                        # why, for anything but ok
    final_url: Optional[str] = None         # the last URL asked for, after redirects
    status: Optional[int] = None            # HTTP status of the last response
    content: Optional[bytes] = None         # the page, only when the whole body arrived
    content_type: str = ""
    hops: List[Tuple[str, str]] = field(default_factory=list)   # (url, address connected to)


class Refused(Exception):
    """The URL breaks a rule; nothing is sent to it."""


def is_public(ip: str) -> bool:
    """True only for a global unicast address. An IPv4 address inside IPv6 (::ffff:a.b.c.d,
    6to4, Teredo) is judged by the IPv4 address it carries. Pure."""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        inner = addr.ipv4_mapped or addr.sixtofour or (addr.teredo[1] if addr.teredo else None)
        if inner is not None:
            return is_public(str(inner))
    return bool(addr.is_global and not addr.is_multicast and not addr.is_reserved
                and not addr.is_unspecified and not addr.is_loopback and not addr.is_link_local
                and not addr.is_private)


def check_url(url: str) -> Tuple[str, str, int, str]:
    """(scheme, host, port, path with query) for an http or https URL. Raises Refused. Pure."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as e:
        raise Refused(f"not a valid URL ({e})")
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise Refused(f"scheme {scheme or '(none)'!r} is not http or https")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise Refused("no host in the URL")
    if parts.username or parts.password:
        raise Refused("the URL carries a user name or password")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return scheme, host, port or (443 if scheme == "https" else 80), path


def resolve_public(host: str, port: int, resolve: Callable = socket.getaddrinfo) -> str:
    """Resolve `host` once and return the address to connect to. Every address the name
    resolves to must be public, so a name with one private answer is refused even
    when another answer is public. An address in the URL itself is checked as it is,
    never passed to a resolver. Raises Refused."""
    try:
        literal = str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        literal = None
    if literal is not None:
        if not is_public(literal):
            raise Refused(f"{host} is a non-public address")
        return literal
    try:
        infos = resolve(host, port, 0, socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as e:
        raise Refused(f"the host name does not resolve ({type(e).__name__})")
    ips = list(dict.fromkeys(info[4][0] for info in infos))
    if not ips:
        raise Refused("the host name has no address")
    bad = [ip for ip in ips if not is_public(ip)]
    if bad:
        raise Refused(f"{host} resolves to a non-public address ({', '.join(bad)})")
    return ips[0]


def _ssl_context() -> ssl.SSLContext:
    cafile = os.environ.get("REQUESTS_CA_BUNDLE")
    if not cafile:
        try:
            import certifi
            cafile = certifi.where()
        except ImportError:
            cafile = None
    return ssl.create_default_context(cafile=cafile)


def _connect_ip(ip: str, port: int, timeout: float) -> socket.socket:
    """A TCP connection to one address, opened directly: no name lookup of any kind."""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect((ip, port))
    except BaseException:
        sock.close()
        raise
    return sock


class _PinnedHTTP(http.client.HTTPConnection):
    """An HTTP connection to one fixed address. The Host header keeps the name."""

    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = _connect_ip(self._ip, self.port, self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    """An HTTPS connection to one fixed address. SNI and the certificate check use the name."""

    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout, context=_ssl_context())
        self._ip = ip

    def connect(self):
        sock = _connect_ip(self._ip, self.port, self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def open_connection(scheme: str, host: str, port: int, ip: str, timeout: float):
    """The connection the fetcher uses. Tests replace it to see which address is used."""
    cls = _PinnedHTTPS if scheme == "https" else _PinnedHTTP
    return cls(host, port, ip, timeout)


def fetch(url: str, *, resolve: Callable = socket.getaddrinfo, connect: Callable = None,
          max_redirects: int = MAX_REDIRECTS, max_bytes: int = MAX_BYTES,
          max_seconds: float = MAX_SECONDS, clock: Callable[[], float] = time.monotonic) -> FetchResult:
    """Fetch one cited URL under the rules above. Never raises for a bad URL or server."""
    connect = connect or open_connection
    start = clock()
    result = FetchResult(url=url, outcome="error")
    current, redirects = url, 0
    while True:
        try:
            scheme, host, port, path = check_url(current)
            ip = resolve_public(host, port, resolve)
        except Refused as e:
            result.outcome, result.detail = "refused", (f"redirect {redirects}: " if redirects else "") + str(e)
            result.final_url = current
            return result
        left = max_seconds - (clock() - start)
        if left <= 0:
            result.outcome, result.detail = "timeout", f"over {max_seconds:g} s"
            return result
        result.hops.append((current, ip))
        result.final_url = current
        conn = None
        try:
            conn = connect(scheme, host, port, ip, left)
            conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8",
                                               "Accept-Encoding": "identity", "Connection": "close"})
            resp = conn.getresponse()
            result.status = resp.status
            result.content_type = resp.getheader("Content-Type", "") or ""
            if resp.status in REDIRECTS and resp.getheader("Location"):
                if redirects >= max_redirects:
                    result.outcome, result.detail = "too_many_redirects", f"more than {max_redirects} redirects"
                    return result
                redirects += 1
                current = urljoin(current, resp.getheader("Location"))
                continue
            body = b""
            while True:
                if clock() - start > max_seconds:
                    result.outcome, result.detail = "timeout", f"over {max_seconds:g} s"
                    return result
                # read1 returns what has arrived, and the socket may wait only for the
                # time that is left, so a server that drips bytes cannot pass 20 seconds.
                if conn.sock is not None:
                    conn.sock.settimeout(max(0.01, max_seconds - (clock() - start)))
                chunk = resp.read1(min(65536, max_bytes + 1 - len(body)))
                if not chunk:
                    break
                body += chunk
                if len(body) > max_bytes:
                    result.outcome, result.detail = "too_large", f"over {max_bytes} bytes"
                    return result
            result.content = body
            result.outcome = "ok" if 200 <= resp.status < 300 else "http_error"
            result.detail = "" if result.outcome == "ok" else f"HTTP {resp.status}"
            return result
        except socket.timeout:
            result.outcome, result.detail = "timeout", f"over {max_seconds:g} s"
            return result
        except (OSError, http.client.HTTPException, ssl.SSLError, ValueError) as e:
            result.outcome, result.detail = "error", f"{type(e).__name__}: {str(e)[:200]}"
            return result
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass


def page_text(content: Optional[bytes]) -> str:
    """The visible text of an HTML page, one space between words. Pure."""
    if not content:
        return ""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(content, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split())


def names_on_page(names, text: str) -> List[str]:
    """The unit names that appear in the page text, ignoring case, spacing, and "&" against
    "and". Pure."""
    def norm(s):
        return " ".join(s.lower().replace("&", " and ").split())
    t = norm(text)
    return [n for n in names if n and norm(n) in t]


def record(log, result: FetchResult, unit_names) -> str:
    """Save one fetch as an evidence row: the page, its text, and the names of the citing
    units that appear in it when the page arrived; the outcome and the reason always.
    Returns the evidence id."""
    text = page_text(result.content) if result.content is not None else None
    found = names_on_page(unit_names, text) if text else []
    return log.add_evidence(url=result.url, http_status=result.status, content=result.content,
                            unit_names=found, final_url=result.final_url, outcome=result.outcome,
                            detail=result.detail, content_type=result.content_type, text=text)
