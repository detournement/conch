"""Strict read-only client for Capitol Procedure Documents.

Procedure Documents are Capitol-owned projections of exact workflow
versions. This adapter never compiles, verifies, accredits, edits, or
otherwise mutates them. It uses the authenticated workflow REST API and
fails closed on contract drift so prose can never become an accidental
executable or authorization channel.
"""

from __future__ import annotations

import hashlib
import json
import struct
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from .credentials import (
    ensure_endpoint_allowed,
    redact_text,
    resolve_admin_token,
)
from .errors import CapitolAuthError, CapitolError, CapitolProtocolError

PROCEDURE_DOCUMENT_SCHEMA = "capitol.procedure_document.v1"
PROCEDURE_COLLECTION_SCHEMA = "capitol.procedure_collection.v1"
PROCEDURE_SEARCH_SCHEMA = "capitol.procedure_search.v1"
WORKFLOW_VERSION_SCHEMA = "capitol.workflow_version.v1"

MAX_QUERY_CHARS = 200
MAX_RESULTS = 25
MAX_MARKDOWN_CHARS = 64_000
MAX_STRUCTURED_CHARS = 128_000
DEFAULT_TIMEOUT = 60.0

_DOCUMENT_KEYS = {
    "schema_version",
    "id",
    "workflow_id",
    "workflow_name",
    "workflow_description",
    "workflow_version_id",
    "version_number",
    "content_digest",
    "compiler_version",
    "verification",
    "verified_by_id",
    "verified_at",
    "health",
    "publication",
    "exposure",
    "compiled_at",
    "markdown",
    "doc_json",
    "created_at",
    "updated_at",
}
_DOCUMENT_REQUIRED = _DOCUMENT_KEYS
_EXPOSURE_KEYS = {
    "basis",
    "version_specific",
    "publish_to_api",
    "publish_to_mcp",
    "publish_to_template",
    "workflow_updated_at",
}
_PUBLICATION_KEYS = {
    "basis",
    "workflow_version_type",
    "is_published",
    "published_at",
}
_COLLECTION_KEYS = {
    "schema_version",
    "limit",
    "offset",
    "total",
}
_SEARCH_KEYS = {
    "schema_version",
    "results",
    "limit",
}
_RESULT_KEYS = {
    "schema_version",
    "id",
    "workflow_id",
    "workflow_version_id",
    "workflow_name",
    "workflow_description",
    "version_number",
    "content_digest",
    "compiler_version",
    "verification",
    "verified_by_id",
    "verified_at",
    "publication",
    "exposure",
    "compiled_at",
    "created_at",
    "updated_at",
}
_VERSION_KEYS = {
    "schema_version",
    "id",
    "workflow_id",
    "version_number",
    "version_type",
    "payload",
    "payload_digest",
    "is_latest",
    "created_by_id",
    "created_at",
}
_VERIFICATIONS = frozenset({"draft", "reviewed", "accredited"})


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def procedure_content_digest(markdown: str, doc_json: Dict[str, Any]) -> str:
    """Digest the generated content bytes, excluding mutable attestations."""
    markdown_bytes = markdown.encode("utf-8")
    json_bytes = _canonical_bytes(doc_json)
    content = b"".join(
        (
            b"capitol-procedure-content-v1\0",
            struct.pack(">Q", len(markdown_bytes)),
            markdown_bytes,
            struct.pack(">Q", len(json_bytes)),
            json_bytes,
        )
    )
    return "sha256:" + hashlib.sha256(content).hexdigest()


def workflow_payload_digest(payload: Dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _strict_object(
    value: Any,
    *,
    where: str,
    keys: set,
    required: set,
) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise CapitolProtocolError(f"{where} must be a JSON object")
    unknown = set(value) - keys
    missing = required - set(value)
    if unknown or missing:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unknown " + ", ".join(sorted(unknown)))
        raise CapitolProtocolError(
            f"{where} contract drift (failing closed): " + "; ".join(details)
        )
    return value


def _positive_int(value: Any, where: str) -> int:
    if isinstance(value, bool):
        raise CapitolProtocolError(f"{where} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 0
    if parsed < 1:
        raise CapitolProtocolError(f"{where} must be a positive integer")
    return parsed


def _exposure(value: Any, where: str) -> Dict[str, Any]:
    row = _strict_object(
        value,
        where=where,
        keys=_EXPOSURE_KEYS,
        required=_EXPOSURE_KEYS,
    )
    if row["basis"] not in ("current_workflow_state", "unavailable"):
        raise CapitolProtocolError(f"{where}.basis is unknown")
    for key in ("publish_to_api", "publish_to_mcp", "publish_to_template"):
        if row[key] is not None and not isinstance(row[key], bool):
            raise CapitolProtocolError(f"{where}.{key} must be boolean/null")
    if row["version_specific"] is not None and not isinstance(
        row["version_specific"], bool
    ):
        raise CapitolProtocolError(
            f"{where}.version_specific must be boolean/null"
        )
    return dict(row)


def _publication(value: Any, where: str) -> Dict[str, Any]:
    row = _strict_object(
        value,
        where=where,
        keys=_PUBLICATION_KEYS,
        required=_PUBLICATION_KEYS,
    )
    if row["basis"] not in ("workflow_version", "unavailable"):
        raise CapitolProtocolError(f"{where}.basis is unknown")
    if row["is_published"] is not None and not isinstance(
        row["is_published"], bool
    ):
        raise CapitolProtocolError(
            f"{where}.is_published must be boolean/null"
        )
    return dict(row)


def _document(value: Any) -> Dict[str, Any]:
    row = _strict_object(
        value,
        where="Procedure document",
        keys=_DOCUMENT_KEYS,
        required=_DOCUMENT_REQUIRED,
    )
    if row["schema_version"] != PROCEDURE_DOCUMENT_SCHEMA:
        raise CapitolProtocolError(
            "unsupported Procedure schema "
            f"{row['schema_version']!r} (supported: "
            f"{PROCEDURE_DOCUMENT_SCHEMA}) — failing closed"
        )
    markdown = row["markdown"]
    doc_json = row["doc_json"]
    if not isinstance(markdown, str) or len(markdown) > MAX_MARKDOWN_CHARS:
        raise CapitolProtocolError(
            f"Procedure markdown must be text within {MAX_MARKDOWN_CHARS} characters"
        )
    if not isinstance(doc_json, dict):
        raise CapitolProtocolError("Procedure doc_json must be an object")
    if len(_canonical_bytes(doc_json)) > MAX_STRUCTURED_CHARS:
        raise CapitolProtocolError(
            f"Procedure doc_json exceeds {MAX_STRUCTURED_CHARS} bytes"
        )
    expected = procedure_content_digest(markdown, doc_json)
    if row["content_digest"] != expected:
        raise CapitolProtocolError(
            "Procedure content_digest does not match markdown/doc_json (failing closed)"
        )
    verification = str(row["verification"])
    if verification not in _VERIFICATIONS:
        raise CapitolProtocolError(f"unknown Procedure verification {verification!r}")
    clean = dict(row)
    clean["version_number"] = _positive_int(
        row["version_number"],
        "Procedure version_number",
    )
    clean["exposure"] = _exposure(
        row["exposure"],
        "Procedure exposure",
    )
    clean["publication"] = _publication(
        row["publication"],
        "Procedure publication",
    )
    return clean


def _result(value: Any, index: int) -> Dict[str, Any]:
    where = f"Procedure results[{index}]"
    row = _strict_object(
        value,
        where=where,
        keys=_RESULT_KEYS,
        required=_RESULT_KEYS,
    )
    if str(row["verification"]) not in _VERIFICATIONS:
        raise CapitolProtocolError(f"{where}.verification is unknown (failing closed)")
    clean = dict(row)
    clean["version_number"] = _positive_int(
        row["version_number"],
        f"{where}.version_number",
    )
    clean["exposure"] = _exposure(row["exposure"], f"{where}.exposure")
    clean["publication"] = _publication(
        row["publication"],
        f"{where}.publication",
    )
    return clean


def _collection(value: Any, *, rows_key: str) -> Dict[str, Any]:
    row = _strict_object(
        value,
        where="Procedure collection",
        keys=_COLLECTION_KEYS | {rows_key},
        required=_COLLECTION_KEYS | {rows_key},
    )
    if row["schema_version"] != PROCEDURE_COLLECTION_SCHEMA:
        raise CapitolProtocolError(
            "unsupported Procedure collection schema "
            f"{row['schema_version']!r} (supported: "
            f"{PROCEDURE_COLLECTION_SCHEMA}) — failing closed"
        )
    if not isinstance(row[rows_key], list):
        raise CapitolProtocolError(
            f"Procedure collection {rows_key} must be a list"
        )
    if len(row[rows_key]) > MAX_RESULTS:
        raise CapitolProtocolError("Procedure collection exceeds result bound")
    return {
        "schema_version": PROCEDURE_COLLECTION_SCHEMA,
        "results": [
            _result(item, index) for index, item in enumerate(row[rows_key])
        ],
        "limit": int(row["limit"]),
        "offset": int(row["offset"]),
        "total": int(row["total"]),
    }


def _search_collection(value: Any) -> Dict[str, Any]:
    row = _strict_object(
        value,
        where="Procedure search",
        keys=_SEARCH_KEYS,
        required=_SEARCH_KEYS,
    )
    if row["schema_version"] != PROCEDURE_SEARCH_SCHEMA:
        raise CapitolProtocolError(
            "unsupported Procedure search schema "
            f"{row['schema_version']!r} (supported: "
            f"{PROCEDURE_SEARCH_SCHEMA}) — failing closed"
        )
    if not isinstance(row["results"], list):
        raise CapitolProtocolError("Procedure search results must be a list")
    if len(row["results"]) > MAX_RESULTS:
        raise CapitolProtocolError("Procedure search exceeds result bound")
    return {
        "schema_version": PROCEDURE_SEARCH_SCHEMA,
        "results": [
            _result(item, index) for index, item in enumerate(row["results"])
        ],
        "limit": int(row["limit"]),
    }


def _workflow_version(value: Any) -> Dict[str, Any]:
    row = _strict_object(
        value,
        where="Workflow version",
        keys=_VERSION_KEYS,
        required=_VERSION_KEYS,
    )
    if row["schema_version"] != WORKFLOW_VERSION_SCHEMA:
        raise CapitolProtocolError(
            "unsupported workflow-version schema "
            f"{row['schema_version']!r} (supported: "
            f"{WORKFLOW_VERSION_SCHEMA}) — failing closed"
        )
    if not isinstance(row["payload"], dict):
        raise CapitolProtocolError("Workflow version payload must be an object")
    expected = workflow_payload_digest(row["payload"])
    if row["payload_digest"] != expected:
        raise CapitolProtocolError(
            "Workflow version payload_digest mismatch (failing closed)"
        )
    if not isinstance(row["is_latest"], bool):
        raise CapitolProtocolError("Workflow version is_latest must be boolean")
    clean = dict(row)
    clean["version_number"] = _positive_int(
        row["version_number"],
        "Workflow version_number",
    )
    return clean


class CapitolProcedureClient:
    """Read-only authenticated workflow-API adapter."""

    def __init__(
        self,
        workflow_url: str,
        org_id: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.workflow_url = str(workflow_url or "").rstrip("/")
        self.org_id = str(org_id or "").strip()
        self._token = str(token or "")
        self.timeout = float(timeout)
        if not self.workflow_url or not self.org_id:
            raise CapitolError("Procedure reads need capitol_base_url and capitol_org")
        if not self._token:
            raise CapitolAuthError(
                "Procedure reads need an authenticated Capitol user/API token"
            )

    def __repr__(self) -> str:
        return (
            f"CapitolProcedureClient(workflow_url={self.workflow_url!r}, "
            f"org_id={self.org_id!r})"
        )

    @classmethod
    def from_config(cls, config: dict, **kwargs) -> "CapitolProcedureClient":
        config = config or {}
        workflow_url = str(config.get("capitol_base_url") or "").strip()
        platform_url = str(config.get("capitol_platform_url") or workflow_url).strip()
        org_id = str(config.get("capitol_org") or "").strip()
        if not workflow_url or not org_id:
            raise CapitolError(
                "Procedure reads are not configured: set capitol_base_url "
                "and capitol_org"
            )
        ensure_endpoint_allowed(config, workflow_url)
        token, _source = resolve_admin_token(
            config,
            org_id,
            platform_url or workflow_url,
        )
        return cls(workflow_url, org_id, token, **kwargs)

    def _redact(self, value: Any) -> str:
        return redact_text(str(value), (self._token,))

    def _request(self, path: str) -> Any:
        url = f"{self.workflow_url}{path}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout,
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read()
            except Exception:
                pass
            finally:
                try:
                    exc.close()
                except Exception:
                    pass
            detail = self._redact(body.decode("utf-8", "replace")[:1000])
            if exc.code in (401, 403):
                raise CapitolAuthError(
                    self._redact(
                        f"Capitol Procedure auth failed (HTTP {exc.code}) "
                        f"at {url}: {detail}"
                    ),
                    http_status=exc.code,
                ) from None
            raise CapitolError(
                self._redact(f"Capitol Procedure HTTP {exc.code} at {url}: {detail}"),
                http_status=exc.code,
                retryable=exc.code in (429, 502, 503, 504),
            ) from None
        except OSError as exc:
            raise CapitolError(
                self._redact(f"Capitol Procedure endpoint unreachable at {url}: {exc}"),
                retryable=True,
                category="transport",
            ) from None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise CapitolProtocolError(
                self._redact(f"Capitol Procedure API returned non-JSON: {exc}")
            ) from None
        return payload

    def search(self, query: str, *, limit: int = 10) -> Dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            raise CapitolError("Procedure search needs a non-empty query")
        if len(query) > MAX_QUERY_CHARS:
            raise CapitolError(
                f"Procedure search query exceeds {MAX_QUERY_CHARS} characters"
            )
        limit = max(1, min(int(limit), MAX_RESULTS))
        params = urllib.parse.urlencode({"q": query, "limit": limit})
        return _search_collection(
            self._request(
                f"/api/v1/orgs/{urllib.parse.quote(self.org_id, safe='')}"
                f"/procedures/search?{params}"
            )
        )

    def list(self, *, limit: int = 10, offset: int = 0) -> Dict[str, Any]:
        limit = max(1, min(int(limit), MAX_RESULTS))
        offset = max(0, int(offset))
        params = urllib.parse.urlencode({"limit": limit, "offset": offset})
        return _collection(
            self._request(
                f"/api/v1/orgs/{urllib.parse.quote(self.org_id, safe='')}"
                f"/procedures?{params}"
            ),
            rows_key="procedures",
        )

    def get(
        self,
        workflow_id: str,
        *,
        version_number: Optional[int] = None,
        workflow_version_id: str = "",
    ) -> Dict[str, Any]:
        workflow_id = str(workflow_id or "").strip()
        if not workflow_id:
            raise CapitolError("Procedure show needs a workflow id")
        params: Dict[str, Any] = {}
        if version_number is not None:
            params["version_number"] = _positive_int(
                version_number,
                "Procedure version",
            )
        if workflow_version_id:
            params["workflow_version_id"] = str(workflow_version_id)
        query = "?" + urllib.parse.urlencode(params) if params else ""
        return _document(
            self._request(
                f"/api/v1/orgs/{urllib.parse.quote(self.org_id, safe='')}"
                f"/workflows/{urllib.parse.quote(workflow_id, safe='')}"
                f"/procedure{query}"
            )
        )

    def get_workflow_version(
        self,
        workflow_id: str,
        workflow_version_id: str,
    ) -> Dict[str, Any]:
        workflow_id = str(workflow_id or "").strip()
        workflow_version_id = str(workflow_version_id or "").strip()
        if not workflow_id or not workflow_version_id:
            raise CapitolError(
                "exact workflow-version read needs workflow and version ids"
            )
        return _workflow_version(
            self._request(
                f"/api/v1/orgs/{urllib.parse.quote(self.org_id, safe='')}"
                f"/workflows/{urllib.parse.quote(workflow_id, safe='')}"
                f"/versions/{urllib.parse.quote(workflow_version_id, safe='')}"
            )
        )
