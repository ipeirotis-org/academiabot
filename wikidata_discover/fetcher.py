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
  The 20 seconds cover every step: each DNS lookup (it may use only the time that is
  left, and a late answer is never used), the connection, the TLS handshake, the
  headers, and the body. Every socket read and write waits only for the time that is
  left, so a server that sends its headers a byte at a time cannot pass it.
- A refused URL is recorded as refused, with the reason, and nothing is fetched from it.

Proxy mode (Shuo, week 5). A Claude Code cloud session has no DNS of its own: it reaches
the web only through a local HTTPS proxy (HTTPS_PROXY). There the fetcher opens a tunnel
through that proxy (HTTP CONNECT host:port) and speaks HTTP or HTTPS inside it. The proxy,
not this machine, resolves the name, so no local address check is possible. The URL rules
still hold: an address written in the URL must be public, and names such as "localhost"
are refused, on the first request and on every redirect. Limits are the same. Each hop
records "proxy <host>:<port>" in place of the address. A proxy that cannot reach the host
(CONNECT 502 or 504) gives outcome "error"; a proxy that refuses it (403, 407) gives
"refused". ACADEMIABOT_FETCH_PROXY sets the proxy (http://host:port), or "off" turns proxy
mode off; without it, HTTPS_PROXY is used when it is set.

fetch(url) returns a FetchResult; it never raises for a bad URL or a bad server.
"""
import codecs
import http.client
import ipaddress
import os
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

MAX_REDIRECTS = 5
MAX_BYTES = 5 * 1024 * 1024
MAX_SECONDS = 20.0
REDIRECTS = (301, 302, 303, 307, 308)
HTML_TYPES = ("text/html", "application/xhtml+xml")
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


class LookupTimeout(Exception):
    """The DNS lookup did not answer within the time that is left; nothing is sent."""


class ProxyAnswer(Exception):
    """The proxy answered the CONNECT with a status other than 200."""

    def __init__(self, status: int, reason: str = ""):
        super().__init__(f"CONNECT {status} {reason}".strip())
        self.status = status


# Names that always mean this machine or its private network. In proxy mode no address
# is known, so these are refused by name.
LOCAL_NAMES = ("localhost", "localhost.localdomain", "metadata", "metadata.google.internal")
LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa")


def proxy_from_env(env=None) -> Optional[Tuple[str, int]]:
    """(host, port) of the proxy to tunnel through, or None for direct connections.
    ACADEMIABOT_FETCH_PROXY wins ("off", "none", or "" turns proxy mode off); else
    HTTPS_PROXY or https_proxy. Only an http:// proxy is used. Pure."""
    env = os.environ if env is None else env
    value = env.get("ACADEMIABOT_FETCH_PROXY")
    if value is None:
        value = env.get("HTTPS_PROXY") or env.get("https_proxy") or ""
    value = value.strip()
    if value.lower() in ("", "off", "none", "0"):
        return None
    parts = urlsplit(value if "://" in value else "http://" + value)
    if parts.scheme.lower() != "http" or not parts.hostname:
        return None
    return parts.hostname, parts.port or 80


def check_host_for_proxy(host: str) -> str:
    """In proxy mode: the host may not be a non-public address or a local name. Returns
    the label recorded in place of an address. Raises Refused. Pure."""
    try:
        literal = str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        literal = None
    if literal is not None:
        if not is_public(literal):
            raise Refused(f"{host} is a non-public address")
        return literal
    name = host.lower().rstrip(".")
    # Other spellings of an IPv4 address ("127.1", "127.0.0.01", "0x7f.1", "2130706433")
    # are not addresses to ipaddress, but a resolver may read them as one. A real host
    # name never has an all-numeric last label, so refuse both.
    try:
        socket.inet_aton(name)
        numeric = True
    except (OSError, ValueError):
        numeric = False
    last = name.rsplit(".", 1)[-1]
    if numeric or re.fullmatch(r"(0x[0-9a-f]*|[0-9]+)", last):
        raise Refused(f"{host} is a numeric address in a non-standard form")
    if name in LOCAL_NAMES or name.endswith(LOCAL_SUFFIXES) or "." not in name:
        raise Refused(f"{host} is a local name")
    return name


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


def lookup_within(resolve: Callable, host: str, port: int, seconds: float):
    """Call resolve(host, port, 0, SOCK_STREAM) in a worker thread and wait at most
    `seconds`. A lookup that is still running then is abandoned: the thread ends on its
    own, and its answer is never used. Raises LookupTimeout, or what resolve raised."""
    box = {}
    done = threading.Event()

    def run():
        try:
            box["infos"] = resolve(host, port, 0, socket.SOCK_STREAM)
        except BaseException as e:  # noqa: BLE001 (handed to the caller below)
            box["error"] = e
        finally:
            done.set()
    threading.Thread(target=run, name=f"dns {host}", daemon=True).start()
    if not done.wait(max(0.0, seconds)):
        raise LookupTimeout(f"the DNS lookup for {host} took over {max(0.0, seconds):.1f} s")
    if "error" in box:
        raise box["error"]
    return box["infos"]


def resolve_public(host: str, port: int, resolve: Callable = socket.getaddrinfo,
                   timeout: Optional[float] = None) -> str:
    """Resolve `host` once and return the address to connect to. Every address the name
    resolves to must be public, so a name with one private answer is refused even
    when another answer is public. An address in the URL itself is checked as it is,
    never passed to a resolver. With `timeout`, the lookup may take at most that many
    seconds. Raises Refused or LookupTimeout."""
    try:
        literal = str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        literal = None
    if literal is not None:
        if not is_public(literal):
            raise Refused(f"{host} is a non-public address")
        return literal
    try:
        if timeout is None:
            infos = resolve(host, port, 0, socket.SOCK_STREAM)
        else:
            infos = lookup_within(resolve, host, port, timeout)
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
    ctx = ssl.create_default_context(cafile=cafile)
    ctx.sslsocket_class = _DeadlineSSLSocket
    return ctx


def _time_left(sock) -> float:
    """Seconds until the socket's deadline. Raises socket.timeout when none are left."""
    deadline = getattr(sock, "deadline", None)
    if deadline is None:
        return sock.gettimeout()
    left = deadline - time.monotonic()
    if left <= 0:
        raise socket.timeout("the time budget is used up")
    return left


class _DeadlineSocket(socket.socket):
    """A socket whose every read and write waits only for the time left until `deadline`,
    so many small reads (headers sent a byte at a time) cannot add up past it."""
    deadline: Optional[float] = None

    def recv(self, *a):
        self.settimeout(_time_left(self))
        return super().recv(*a)

    def recv_into(self, *a):
        self.settimeout(_time_left(self))
        return super().recv_into(*a)

    def send(self, *a):
        self.settimeout(_time_left(self))
        return super().send(*a)

    def sendall(self, *a):
        self.settimeout(_time_left(self))
        return super().sendall(*a)


class _DeadlineSSLSocket(ssl.SSLSocket):
    """The same rule for an HTTPS socket."""
    deadline: Optional[float] = None

    def recv(self, *a):
        self.settimeout(_time_left(self))
        return super().recv(*a)

    def recv_into(self, *a):
        self.settimeout(_time_left(self))
        return super().recv_into(*a)

    def send(self, *a):
        self.settimeout(_time_left(self))
        return super().send(*a)

    def sendall(self, *a):
        self.settimeout(_time_left(self))
        return super().sendall(*a)


def _connect_ip(ip: str, port: int, timeout: float, deadline: Optional[float] = None) -> socket.socket:
    """A TCP connection to one address, opened directly: no name lookup of any kind.
    Every later read and write on it waits only for the time left until `deadline`.
    The socket's timeout is then the time left, which also bounds a TLS handshake: Python
    runs the whole handshake as one call under one timeout."""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sock = _DeadlineSocket(family, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect((ip, port))
        sock.deadline = deadline
        sock.settimeout(_time_left(sock))
    except BaseException:
        sock.close()
        raise
    return sock


class _PinnedHTTP(http.client.HTTPConnection):
    """An HTTP connection to one fixed address. The Host header keeps the name."""

    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip
        self._deadline = time.monotonic() + timeout

    def connect(self):
        self.sock = _connect_ip(self._ip, self.port, self.timeout, deadline=self._deadline)


class _PinnedHTTPS(http.client.HTTPSConnection):
    """An HTTPS connection to one fixed address. SNI and the certificate check use the name."""

    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout, context=_ssl_context())
        self._ip = ip
        self._deadline = time.monotonic() + timeout

    def connect(self):
        sock = _connect_ip(self._ip, self.port, self.timeout, deadline=self._deadline)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        self.sock.deadline = self._deadline


def _read_connect_reply(sock, limit: int = 16384) -> Tuple[int, str]:
    """Read the proxy's answer to CONNECT up to the blank line, one byte at a time so
    nothing of the tunnel is read past it. Returns (status, reason)."""
    data = b""
    while not data.endswith(b"\r\n\r\n"):
        chunk = sock.recv(1)
        if not chunk:
            raise http.client.RemoteDisconnected("the proxy closed the connection during CONNECT")
        data += chunk
        if len(data) > limit:
            raise http.client.HTTPException("the proxy answer to CONNECT is too long")
    first = data.split(b"\r\n", 1)[0].decode("latin-1")
    parts = first.split(" ", 2)
    try:
        return int(parts[1]), (parts[2] if len(parts) > 2 else "")
    except (IndexError, ValueError):
        raise http.client.HTTPException(f"not an HTTP answer from the proxy: {first[:80]!r}")


def _tunnel(proxy: Tuple[str, int], host: str, port: int, timeout: float, deadline: float) -> socket.socket:
    """A socket to the proxy with an open tunnel to host:port. Raises ProxyAnswer when the
    proxy does not answer 200."""
    proxy_host, proxy_port = proxy
    try:
        proxy_ip = str(ipaddress.ip_address(proxy_host.strip("[]")))
    except ValueError:
        # The proxy's own name: the lookup may use only the time left, like any other.
        infos = lookup_within(socket.getaddrinfo, proxy_host, proxy_port, deadline - time.monotonic())
        proxy_ip = infos[0][4][0]
    left = deadline - time.monotonic()
    if left <= 0:
        raise socket.timeout("the time budget is used up")
    sock = _connect_ip(proxy_ip, proxy_port, min(timeout, left), deadline=deadline)
    try:
        target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        sock.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode("ascii"))
        status, reason = _read_connect_reply(sock)
        if status != 200:
            raise ProxyAnswer(status, reason)
    except BaseException:
        sock.close()
        raise
    return sock


class _ProxiedHTTP(http.client.HTTPConnection):
    """An HTTP connection inside a CONNECT tunnel through the proxy."""

    def __init__(self, host, port, proxy, timeout):
        super().__init__(host, port, timeout=timeout)
        self._proxy = proxy
        self._deadline = time.monotonic() + timeout

    def connect(self):
        self.sock = _tunnel(self._proxy, self.host, self.port, self.timeout, self._deadline)


class _ProxiedHTTPS(http.client.HTTPSConnection):
    """An HTTPS connection inside a CONNECT tunnel. SNI and the certificate check use the name."""

    def __init__(self, host, port, proxy, timeout):
        super().__init__(host, port, timeout=timeout, context=_ssl_context())
        self._proxy = proxy
        self._deadline = time.monotonic() + timeout

    def connect(self):
        sock = _tunnel(self._proxy, self.host, self.port, self.timeout, self._deadline)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        self.sock.deadline = self._deadline


def open_proxied(scheme: str, host: str, port: int, proxy: Tuple[str, int], timeout: float):
    """The connection the fetcher uses in proxy mode."""
    cls = _ProxiedHTTPS if scheme == "https" else _ProxiedHTTP
    return cls(host, port, proxy, timeout)


def open_connection(scheme: str, host: str, port: int, ip: str, timeout: float):
    """The connection the fetcher uses. Tests replace it to see which address is used."""
    cls = _PinnedHTTPS if scheme == "https" else _PinnedHTTP
    return cls(host, port, ip, timeout)


_FROM_ENV = object()


def fetch(url: str, *, resolve: Callable = socket.getaddrinfo, connect: Callable = None,
          max_redirects: int = MAX_REDIRECTS, max_bytes: int = MAX_BYTES,
          max_seconds: float = MAX_SECONDS, clock: Callable[[], float] = time.monotonic,
          proxy=_FROM_ENV) -> FetchResult:
    """Fetch one cited URL under the rules above. Never raises for a bad URL or server.
    `proxy` is (host, port), None for direct connections, or by default proxy_from_env()."""
    if proxy is _FROM_ENV:
        proxy = proxy_from_env()
    if proxy is not None:
        connect = connect or (lambda scheme, host, port, _label, timeout:
                              open_proxied(scheme, host, port, proxy, timeout))
    connect = connect or open_connection
    start = clock()
    result = FetchResult(url=url, outcome="error")
    current, redirects = url, 0
    while True:
        where = f"redirect {redirects}: " if redirects else ""
        left = max_seconds - (clock() - start)
        if left <= 0:
            result.outcome, result.detail = "timeout", f"over {max_seconds:g} s"
            return result
        try:
            scheme, host, port, path = check_url(current)
            if proxy is not None:
                check_host_for_proxy(host)
                ip = f"proxy {proxy[0]}:{proxy[1]}"
            else:
                ip = resolve_public(host, port, resolve, timeout=left)
        except Refused as e:
            result.outcome, result.detail = "refused", where + str(e)
            result.final_url = current
            return result
        except LookupTimeout as e:
            result.outcome, result.detail = "timeout", where + str(e)
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
                # read1 returns what has arrived. The socket waits only for the time left
                # (see _DeadlineSocket), so a server that drips bytes cannot pass 20 seconds.
                chunk = resp.read1(min(65536, max_bytes + 1 - len(body)))
                if not chunk:
                    break
                body += chunk
                if len(body) > max_bytes:
                    result.outcome, result.detail = "too_large", f"over {max_bytes} bytes"
                    return result
            # read1 returns an empty chunk, and raises nothing, when the server closes the
            # connection before the Content-Length it declared. A part of a page is not the
            # page: names in the missing part would wrongly count as "not on the page".
            missing = getattr(resp, "length", None)
            if missing:
                result.outcome = "error"
                result.detail = (f"connection closed after {len(body)} of "
                                 f"{len(body) + missing} bytes (Content-Length)")
                return result
            result.content = body
            result.outcome = "ok" if 200 <= resp.status < 300 else "http_error"
            result.detail = "" if result.outcome == "ok" else f"HTTP {resp.status}"
            return result
        except socket.timeout:
            result.outcome, result.detail = "timeout", f"over {max_seconds:g} s"
            return result
        except LookupTimeout as e:
            result.outcome, result.detail = "timeout", where + str(e)
            return result
        except ProxyAnswer as e:
            if e.status in (403, 407):
                result.outcome = "refused"
                result.detail = where + f"the proxy refused the host ({e})"
            else:
                result.outcome = "error"
                result.detail = where + f"the proxy could not reach the host ({e})"
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


def media_type(content_type: str) -> str:
    """The media type of a Content-Type header, lower case, without parameters. Pure."""
    return (content_type or "").split(";", 1)[0].strip().lower()


def declared_charset(content: bytes, content_type: str) -> Optional[str]:
    """The charset the page declares: the Content-Type header first, then a <meta> tag in
    the first 2 KB of an HTML page. None when it declares none or names an unknown one. Pure."""
    found = None
    m = re.search(r"charset\s*=\s*[\"']?([\w.:-]+)", content_type or "", re.I)
    if m:
        found = m.group(1)
    elif media_type(content_type) in HTML_TYPES or not content_type:
        m = re.search(rb"<meta[^>]+charset\s*=\s*[\"']?([\w.:-]+)", content[:2048], re.I)
        found = m.group(1).decode("ascii") if m else None
    if not found:
        return None
    try:
        name = codecs.lookup(found).name
    except LookupError:
        return None
    # The HTML standard reads a page labeled ISO-8859-1 or ASCII as windows-1252.
    return "cp1252" if name in ("iso8859-1", "latin-1", "ascii") else name


def decode_page(content: bytes, content_type: str) -> str:
    """The page as text, decoded with its declared charset, else UTF-8. A charset that
    Python knows but cannot decode text with (base64, hex, rot13, or idna, which refuses
    "replace") is read as UTF-8 too, so one odd page never stops a run. Pure."""
    charset = declared_charset(content, content_type) or "utf-8"
    try:
        return content.decode(charset, "replace")
    except (LookupError, UnicodeError):
        return content.decode("utf-8", "replace")


def page_text(content: Optional[bytes], content_type: str = "") -> str:
    """The visible text of an HTML page, one space between words. Pure."""
    if not content:
        return ""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(decode_page(content, content_type), "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split())


def page_text_or_skip(content: bytes, content_type: str) -> Tuple[str, str]:
    """(text, name_check) for a page that arrived. HTML, plain text, and a page without a
    Content-Type are read ("done"). A PDF or any other type is not read: the text is
    empty and the name check is "skipped: ...", so it is never "name not found". Pure."""
    media = media_type(content_type)
    if media in HTML_TYPES or not media:
        return page_text(content, content_type), "done"
    if media == "text/plain":
        return " ".join(decode_page(content, content_type).split()), "done"
    if media == "application/pdf":
        return "", "skipped: PDF text is not extracted"
    return "", f"skipped: content type {media} is not read"


def _norm_words(s: str) -> str:
    return " ".join(s.lower().replace("&", " and ").split())


def name_in_text(name: str, text: str) -> bool:
    """True when the whole name appears in the text as a run of whole words, ignoring case,
    spacing, and "&" against "and". "School of Art" is found in "the School of Art and
    Design"; "Art" is not found in "Department". Pure."""
    n = _norm_words(name or "")
    if not n:
        return False
    return re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", _norm_words(text or "")) is not None


def names_on_page(names, text: str) -> List[str]:
    """The unit names that appear in the page text as whole words (see name_in_text). Pure."""
    t = _norm_words(text or "")
    return [n for n in names if n and name_in_text(n, t)]


def read_result(result: FetchResult) -> Tuple[Optional[str], Optional[str]]:
    """(text, name_check) of a fetch: both None when no page arrived. Pure."""
    if result.content is None:
        return None, None
    return page_text_or_skip(result.content, result.content_type)


def record(log, result: FetchResult, unit_names, read: Optional[Tuple[Optional[str], Optional[str]]] = None) -> str:
    """Save one fetch as an evidence row: the page bytes, its text, and the names of the
    citing units that appear in it when the page arrived; the outcome and the reason always.
    `read` is read_result(result) when the caller already has it. Returns the evidence id."""
    text, name_check = read if read is not None else read_result(result)
    found: List[str] = []
    if result.content is not None:
        found = names_on_page(unit_names, text) if name_check == "done" else []
    return log.add_evidence(url=result.url, http_status=result.status, content=result.content,
                            unit_names=found, final_url=result.final_url, outcome=result.outcome,
                            detail=result.detail, content_type=result.content_type, text=text,
                            name_check=name_check)
