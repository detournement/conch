"""Keep-alive HTTP transport for LLM provider calls.

``urllib.request.urlopen`` sends ``Connection: close``, so every model call
pays a fresh TCP + TLS handshake (~60 ms against the hosted APIs). This
module keeps idle connections per ``(scheme, host, port)`` and reuses them.

Design notes:

* Callers use :func:`urlopen` with exactly the same arguments they would pass
  to ``urllib.request.urlopen``. At call time it checks whether
  ``urllib.request.urlopen`` is still the stock function; if anything has
  replaced it (``mock.patch("urllib.request.urlopen", ...)`` in tests, an
  instrumentation shim) the call is delegated to that replacement unchanged,
  so patches keep intercepting every request.
* The stock urllib path is also used when keep-alive is disabled
  (``http_keepalive=false``), when a proxy applies to the host, and for anything that is not a plain
  http/https request with a bytes body.
* A connection is checked out exclusively for one request and returns to
  the pool only once its response has been read to the end (or drained on
  close). A streaming response holds its connection until it is closed.
  Idle connections per host are bounded and expire.
* A reused connection that fails before any response bytes arrive (peer
  closed it while idle) is retried once on a fresh connection. Nothing is
  retried once a response has started.
* TLS uses ``http.client.HTTPSConnection(context=None)``, which is what
  urllib's default ``HTTPSHandler`` does, so certificate verification is
  identical. Connection errors raise ``URLError``; non-2xx responses raise
  ``HTTPError`` whose ``read()``/``code``/``headers`` behave as before;
  redirects are handed to stock urllib so they follow the same rules.
"""

from __future__ import annotations

import http.client
import io
import os
import select
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["urlopen", "keepalive_enabled", "clear_pool", "pool_stats"]

_STOCK_URLOPEN = urllib.request.urlopen

#: Idle connections kept per (scheme, host, port).
MAX_IDLE_PER_HOST = 8
#: Seconds an idle connection may sit in the pool before it is discarded.
IDLE_TIMEOUT = 60.0
#: Bounds for draining an unread response tail on close so the connection
#: can be reused (e.g. the chunked terminator after an SSE ``[DONE]``).
_DRAIN_MAX_BYTES = 64 * 1024
_DRAIN_SECONDS = 0.1

_REDIRECT_CODES = (301, 302, 303, 307, 308)

# Failures that mean "the idle connection was already dead" when they occur
# while sending on a reused connection.
_STALE_SEND_ERRORS: Tuple[type, ...] = (
    ConnectionError,  # BrokenPipe, ConnectionReset, ConnectionAborted
    ssl.SSLEOFError,
    http.client.ImproperConnectionState,
)

Key = Tuple[str, str, int]

_lock = threading.Lock()
_idle: Dict[Key, List[Tuple[http.client.HTTPConnection, float]]] = {}
_stats = {"new": 0, "reused": 0, "stale_retries": 0, "fallback": 0}


def keepalive_enabled(config: Optional[dict] = None) -> bool:
    """``http_keepalive`` defaults to on; false/0/no/off opt out."""
    if not config:
        return True
    value = config.get("http_keepalive", True)
    return str(value).strip().lower() not in ("false", "0", "no", "off")


def clear_pool() -> None:
    """Close every idle pooled connection."""
    with _lock:
        conns = [conn for entries in _idle.values() for conn, _ in entries]
        _idle.clear()
    for conn in conns:
        _close(conn)


def pool_stats() -> dict:
    with _lock:
        stats = dict(_stats)
        stats["idle"] = sum(len(v) for v in _idle.values())
    return stats


def _close(conn: http.client.HTTPConnection) -> None:
    try:
        conn.close()
    except Exception:
        pass


def _after_fork_in_child() -> None:
    # The child must never share sockets with the parent's pool. Closing the
    # child's descriptors does not affect the parent's connections.
    global _lock
    _lock = threading.Lock()
    conns = [conn for entries in _idle.values() for conn, _ in entries]
    _idle.clear()
    for conn in conns:
        sock = getattr(conn, "sock", None)
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


def _is_alive(conn: http.client.HTTPConnection) -> bool:
    """An idle keep-alive socket must have nothing to read: readable means
    the peer closed it (EOF) or sent something unexpected."""
    sock = getattr(conn, "sock", None)
    if sock is None:
        return False
    try:
        if isinstance(sock, ssl.SSLSocket) and sock.pending():
            return False
        readable, _, _ = select.select([sock], [], [], 0)
    except (OSError, ValueError):
        return False
    return not readable


def _checkout(key: Key) -> Optional[http.client.HTTPConnection]:
    now = time.monotonic()
    expired: List[http.client.HTTPConnection] = []
    found = None
    with _lock:
        entries = _idle.get(key)
        while entries:
            conn, since = entries.pop()  # most recently used first
            if now - since > IDLE_TIMEOUT:
                expired.append(conn)
                continue
            found = conn
            break
        # Anything older than the chosen one that has expired goes too.
        if entries:
            keep = []
            for conn, since in entries:
                (expired.append(conn) if now - since > IDLE_TIMEOUT
                 else keep.append((conn, since)))
            entries[:] = keep
    for conn in expired:
        _close(conn)
    if found is not None and not _is_alive(found):
        _close(found)
        return _checkout(key)
    return found


def _checkin(key: Key, conn: http.client.HTTPConnection) -> None:
    evicted = None
    with _lock:
        entries = _idle.setdefault(key, [])
        if len(entries) >= MAX_IDLE_PER_HOST:
            evicted = entries.pop(0)[0]
        entries.append((conn, time.monotonic()))
    if evicted is not None:
        _close(evicted)


class _PooledResponse:
    """File-like view of an ``http.client.HTTPResponse`` that hands its
    connection back to the pool once the body is fully consumed."""

    def __init__(self, resp: http.client.HTTPResponse,
                 conn: http.client.HTTPConnection, key: Key, url: str):
        self._resp = resp
        self._conn: Optional[http.client.HTTPConnection] = conn
        self._key = key
        self.url = url
        self.status = resp.status
        self.code = resp.status
        self.reason = resp.reason
        self.msg = resp.reason  # urllib sets r.msg = r.reason
        self.headers = resp.headers
        self.will_close = resp.will_close
        self._check_eof()

    # -- connection lifecycle -------------------------------------------
    def _release(self, reusable: bool) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        if reusable and not self.will_close:
            _checkin(self._key, conn)
        else:
            _close(conn)

    def _check_eof(self) -> None:
        if self._conn is not None and self._resp.isclosed():
            self._release(True)

    def _drain(self) -> bool:
        """Read a short unread tail so the connection can be reused."""
        resp = self._resp
        if resp.will_close:
            return False
        length = resp.length
        if length is not None and length > _DRAIN_MAX_BYTES:
            return False
        sock = getattr(self._conn, "sock", None)
        try:
            if sock is not None:
                sock.settimeout(_DRAIN_SECONDS)
            deadline = time.monotonic() + _DRAIN_SECONDS
            total = 0
            while not resp.isclosed():
                chunk = resp.read(8192)
                if not chunk:
                    break
                total += len(chunk)
                if total > _DRAIN_MAX_BYTES or time.monotonic() > deadline:
                    return False
            return resp.isclosed()
        except Exception:
            return False

    def close(self) -> None:
        if self._conn is not None:
            fully_read = self._resp.isclosed() or self._drain()
            try:
                self._resp.close()
            finally:
                self._release(fully_read)
        else:
            self._resp.close()

    @property
    def closed(self) -> bool:
        return self._resp.closed

    def isclosed(self) -> bool:
        return self._resp.isclosed()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def __del__(self):
        conn = getattr(self, "_conn", None)
        if conn is not None:
            self._conn = None
            _close(conn)

    # -- reading --------------------------------------------------------
    def read(self, amt: Optional[int] = None) -> bytes:
        try:
            return self._resp.read(amt) if amt is not None else self._resp.read()
        finally:
            self._check_eof()

    def read1(self, n: int = -1) -> bytes:
        try:
            return self._resp.read1(n)
        finally:
            self._check_eof()

    def readinto(self, b) -> int:
        try:
            return self._resp.readinto(b)
        finally:
            self._check_eof()

    def readline(self, limit: int = -1) -> bytes:
        try:
            return self._resp.readline(limit)
        finally:
            self._check_eof()

    def readlines(self, hint: int = -1) -> List[bytes]:
        try:
            return self._resp.readlines(hint)
        finally:
            self._check_eof()

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def readable(self) -> bool:
        return True

    # -- urllib response API -------------------------------------------
    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str:
        return self.url

    def info(self):
        return self.headers

    def getheader(self, name: str, default: Any = None):
        return self._resp.getheader(name, default)

    def getheaders(self):
        return self._resp.getheaders()

    def __getattr__(self, name: str):
        if name.startswith("__") or name in ("_resp", "_conn"):
            raise AttributeError(name)
        return getattr(self._resp, name)


def _target(req: urllib.request.Request) -> Optional[Key]:
    scheme = (req.type or "").lower()
    if scheme not in ("http", "https"):
        return None
    if req.data is not None and not isinstance(req.data, (bytes, bytearray)):
        return None
    if req.has_header("Transfer-encoding"):
        return None
    host = req.host
    if not host:
        return None
    try:
        proxies = urllib.request.getproxies()
        if scheme in proxies and not urllib.request.proxy_bypass(host):
            return None
    except Exception:
        return None
    try:
        parsed = urllib.parse.urlsplit(f"{scheme}://{host}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not hostname:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return (scheme, hostname, port)


def _new_connection(key: Key, timeout: Any) -> http.client.HTTPConnection:
    scheme, host, port = key
    if scheme == "https":
        # context=None -> ssl default context, exactly as urllib's default
        # HTTPSHandler builds its connections.
        return http.client.HTTPSConnection(host, port, timeout=timeout)
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _apply_timeout(conn: http.client.HTTPConnection, timeout: Any) -> None:
    conn.timeout = timeout
    if conn.sock is not None:
        value = (socket.getdefaulttimeout()
                 if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout)
        conn.sock.settimeout(value)


def _headers_for(req: urllib.request.Request) -> Dict[str, str]:
    headers = dict(req.unredirected_hdrs)
    headers.update(req.headers)
    if req.data is not None and "Content-type" not in headers:
        headers["Content-type"] = "application/x-www-form-urlencoded"
    if "User-agent" not in headers:
        headers["User-agent"] = "Python-urllib/%s" % urllib.request.__version__
    headers.pop("Connection", None)
    headers.pop("Host", None)  # http.client derives it from host:port
    return {name.title(): value for name, value in headers.items()}


_RETRY = object()


class _CountingReader(io.RawIOBase):
    """Raw socket reader that counts received bytes, so a failure while
    reading the status line can tell "nothing arrived" from "some did"."""

    def __init__(self, raw):
        super().__init__()
        self._raw = raw
        self.count = 0

    def readinto(self, b) -> int:
        n = self._raw.readinto(b)
        if n:
            self.count += n
        return n

    def readable(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._raw.fileno()

    def close(self) -> None:
        try:
            self._raw.close()
        finally:
            super().close()


class _CountingResponse(http.client.HTTPResponse):
    def __init__(self, sock, *args, **kwargs):
        super().__init__(sock, *args, **kwargs)
        self.counter = _CountingReader(self.fp.detach())
        self.fp = io.BufferedReader(self.counter)


def _attempt(req, key: Key, timeout: Any, headers: Dict[str, str]):
    conn = _checkout(key)
    reused = conn is not None
    if conn is None:
        conn = _new_connection(key, timeout)
    else:
        _apply_timeout(conn, timeout)
    responses: List[_CountingResponse] = []

    def _response_factory(*args, **kwargs):
        response = _CountingResponse(*args, **kwargs)
        responses.append(response)
        return response

    conn.response_class = _response_factory
    try:
        try:
            conn.request(req.get_method(), req.selector, req.data, headers)
        except _STALE_SEND_ERRORS as err:
            if reused:
                _close(conn)
                return _RETRY
            raise urllib.error.URLError(err)
        except OSError as err:  # connect failure, timeout, TLS error
            raise urllib.error.URLError(err)
        try:
            resp = conn.getresponse()
        except (ConnectionError, ssl.SSLEOFError):
            # Closed (or reset) before a single response byte arrived: on a
            # reused connection that is the stale keep-alive race. Once any
            # byte has been received the error propagates unretried.
            received = responses[-1].counter.count if responses else 0
            if reused and received == 0:
                _close(conn)
                return _RETRY
            raise
    except BaseException:
        _close(conn)
        raise
    with _lock:
        _stats["reused" if reused else "new"] += 1
    return _PooledResponse(resp, conn, key, req.full_url)


def urlopen(url, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, *,
            config: Optional[dict] = None):
    """Drop-in for ``urllib.request.urlopen(url, data, timeout)`` that reuses
    connections. *config* only feeds the ``http_keepalive`` opt-out."""
    current = urllib.request.urlopen
    if current is not _STOCK_URLOPEN or not keepalive_enabled(config):
        return _delegate(current, url, data, timeout)

    req = url if isinstance(url, urllib.request.Request) else urllib.request.Request(url)
    if data is not None:
        req.data = data
    key = _target(req)
    if key is None:
        with _lock:
            _stats["fallback"] += 1
        return _delegate(current, url, data, timeout)

    headers = _headers_for(req)
    result = _attempt(req, key, timeout, headers)
    if result is _RETRY:
        with _lock:
            _stats["stale_retries"] += 1
        # Fresh connection only: drop every idle connection for this host,
        # they are likely as stale as the one that just failed.
        with _lock:
            stale = _idle.pop(key, [])
        for conn, _ in stale:
            _close(conn)
        result = _attempt(req, key, timeout, headers)
        if result is _RETRY:  # cannot happen on a fresh connection
            raise urllib.error.URLError("connection closed by peer")

    if 200 <= result.status < 300:
        return result
    if result.status in _REDIRECT_CODES:
        result.close()
        return _delegate(current, req, None, timeout)
    raise urllib.error.HTTPError(
        req.full_url, result.status, result.reason, result.headers, result
    )


def _delegate(fn, url, data, timeout):
    # Keep the call shape of the original call sites so mocks see
    # ``urlopen(req, timeout=...)`` exactly as before.
    if data is None:
        if timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
            return fn(url)
        return fn(url, timeout=timeout)
    return fn(url, data, timeout=timeout)

