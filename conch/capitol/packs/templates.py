"""The flow-pack template language (engine feature E1).

Two deterministic, bounded mini-languages — no eval, no callables, no
attribute access on arbitrary objects — per the pack boundary ("packs are
data, not code"):

1. **Expressions** — full-string ``${...}`` values inside manifest fields.
   Grammar (everything the ``conch.flow_pack.v1`` schema admits)::

       ${root.path}                          value reference
       ${root.path:default}                  default when the value is falsy
                                             (the default may itself be an
                                             expression: ``${a.b:${c.d:}}``)
       ${config.key!required}                fail closed when unset (missing
                                             keys are collected per render so
                                             one error names all of them)
       ${contract.field + 1}                 integer increment
       ${contract.items[].{a, b} +order}     list projection: pick the named
                                             fields per item, skip items
                                             missing the first field, add the
                                             item's original index as "order"
       ${... !nonempty}                      fail closed on an empty result
       ${expr | fill_missing(config: field=config_key, ...) !complete}
                                             dict filter: back-fill each
                                             listed field from config when
                                             absent; the result carries
                                             exactly the listed fields in
                                             order; ``!complete`` fails
                                             closed naming missing fields
       ${engine.unique_id('prefix')}         a fresh "{prefix}-{10 hex}" id

   Roots: ``config`` (conch config; values are str()-ed and stripped),
   ``session`` (the engine's session view), ``intake`` (inbound text),
   ``contract`` (the pinned typed contract; ``${contract}`` alone is the
   whole object, returned by reference so identity is preserved), and
   ``engine`` (the whitelisted helpers above).

2. **Formulas** — plain strings containing ``{...}`` tokens, resolved
   against a flat context (the rendered request, a contract, or message
   variables). Token grammar::

       {path.to.field}           str(value)  (missing → "None", matching the
                                 f-string behavior the drivers had)
       {#path}                   len(list value)
       {path[-12:]}              the last 12 characters of str(value)
       {path:80}                 clean_text(value, 80) — control bytes
                                 stripped, bounded
       {path:80=fallback}        fallback when the cleaned value is empty
       {path=fallback}           fallback without a bound

   Formulas power idempotency-key templates, challenge strings, approval
   descriptions, pack message wording, and presentation lines.

Line specs (presentation sections) are lists whose items are either a
formula string, ``{"if": path, "line"|"append": formula}`` (conditional on
a truthy value), or ``{"each": path, "line": formula}`` (one line per list
item, exposed as ``item``).
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Callable, Dict, List, Optional

from ..errors import CapitolError

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean_text(value: Any, limit: int = 4000) -> str:
    """Workflow/model text is untrusted: strip control bytes, bound size."""
    text = _CONTROL_RE.sub("", str(value if value is not None else ""))
    return text[:limit]


def default_unique_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class TemplateError(CapitolError):
    """A manifest template failed to parse or resolve (fail closed)."""


# ---------------------------------------------------------------------------
# Expression parsing (load-time) — returns a plain-tuple AST
# ---------------------------------------------------------------------------

_ROOTS = ("config", "session", "intake", "contract", "engine")
_UNIQUE_RE = re.compile(r"^engine\.unique_id\('([A-Za-z0-9_.:-]+)'\)$")
_PROJECTION_RE = re.compile(r"^(.*)\[\]\.\{([^{}]+)\}(.*)$")
_ARITH_RE = re.compile(r"^(.*?)\s*\+\s*(\d+)$")
_FILL_RE = re.compile(r"^fill_missing\(\s*config:\s*([^)]*)\)$")


def is_expression(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("${")
        and value.endswith("}")
    )


def _split_default(text: str) -> tuple:
    """Split ``path:default`` at the first top-level colon; the default may
    contain nested ``${...}`` expressions."""
    depth = 0
    for index, char in enumerate(text):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == ":" and depth == 0:
            return text[:index], text[index + 1:]
    return text, None


def parse_expression(text: str) -> Dict[str, Any]:
    """Parse one ``${...}`` expression into an AST dict (fail closed)."""
    if not is_expression(text):
        raise TemplateError(f"not a template expression: {text!r}")
    inner = text[2:-1]

    spec: Dict[str, Any] = {
        "required": False, "nonempty": False, "complete": False,
        "order": False, "fill": None, "default": None, "arith": 0,
        "projection": None, "unique_prefix": None,
    }

    # filter: `expr | fill_missing(...)` at top level
    depth = 0
    pipe_at = -1
    for index, char in enumerate(inner):
        if char in "{(":
            depth += 1
        elif char in "})":
            depth -= 1
        elif char == "|" and depth == 0:
            pipe_at = index
            break
    filter_text = ""
    if pipe_at >= 0:
        filter_text = inner[pipe_at + 1:].strip()
        inner = inner[:pipe_at].strip()

    # trailing modifiers (either side of the pipe)
    def strip_modifiers(text: str) -> str:
        changed = True
        while changed:
            changed = False
            for token, key in ((" !complete", "complete"),
                               ("!complete", "complete"),
                               (" !nonempty", "nonempty"),
                               ("!nonempty", "nonempty"),
                               (" !required", "required"),
                               ("!required", "required"),
                               (" +order", "order"),
                               ("+order", "order")):
                if text.endswith(token):
                    spec[key] = True
                    text = text[: -len(token)]
                    changed = True
        return text.strip()

    if filter_text:
        filter_text = strip_modifiers(filter_text)
        match = _FILL_RE.match(filter_text)
        if not match:
            raise TemplateError(f"unknown template filter: {filter_text!r}")
        pairs = []
        for chunk in match.group(1).split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            field, _, config_key = chunk.partition("=")
            if not field.strip() or not config_key.strip():
                raise TemplateError(
                    "fill_missing pairs must be field=config_key: "
                    f"{chunk!r}"
                )
            pairs.append((field.strip(), config_key.strip()))
        if not pairs:
            raise TemplateError("fill_missing needs at least one pair")
        spec["fill"] = pairs

    inner = strip_modifiers(inner)

    unique = _UNIQUE_RE.match(inner)
    if unique:
        spec["unique_prefix"] = unique.group(1)
        return spec

    path_text, default = _split_default(inner)
    path_text = strip_modifiers(path_text)
    if default is not None:
        spec["default"] = (
            parse_expression(default) if is_expression(default)
            else default
        )

    projection = _PROJECTION_RE.match(path_text)
    if projection:
        if projection.group(3).strip():
            raise TemplateError(
                f"projection must end the expression: {text!r}"
            )
        fields = [
            field.strip() for field in projection.group(2).split(",")
            if field.strip()
        ]
        if not fields:
            raise TemplateError(f"projection names no fields: {text!r}")
        spec["projection"] = fields
        path_text = projection.group(1)

    arith = _ARITH_RE.match(path_text)
    if arith and not spec["projection"]:
        spec["arith"] = int(arith.group(2))
        path_text = arith.group(1)

    parts = [part for part in path_text.strip().split(".") if part]
    if not parts or parts[0] not in _ROOTS:
        raise TemplateError(
            f"template path must start with one of {_ROOTS}: {text!r}"
        )
    if parts[0] == "engine":
        raise TemplateError(
            f"unknown engine template function: {text!r}"
        )
    spec["root"] = parts[0]
    spec["path"] = parts[1:]
    return spec


# ---------------------------------------------------------------------------
# Expression evaluation (render-time)
# ---------------------------------------------------------------------------

class RenderContext:
    """The value roots one render sees; ``missing_required`` collects
    ``!required`` config keys so one error can name all of them."""

    def __init__(
        self,
        *,
        config: Optional[dict] = None,
        session: Optional[dict] = None,
        intake: Optional[dict] = None,
        contract: Optional[dict] = None,
        unique_id: Optional[Callable[[str], str]] = None,
    ):
        self.config = config or {}
        self.session = session or {}
        self.intake = intake or {}
        self.contract = contract
        self.unique_id = unique_id or default_unique_id
        self.missing_required: List[str] = []


def _dig(node: Any, path: List[str]) -> Any:
    for part in path:
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def evaluate_expression(spec: Dict[str, Any], ctx: RenderContext,
                        *, where: str = "") -> Any:
    if spec.get("unique_prefix"):
        return ctx.unique_id(spec["unique_prefix"])

    root_name = spec["root"]
    path = spec["path"]
    if root_name == "config":
        raw = ctx.config.get(path[0]) if len(path) == 1 else _dig(
            ctx.config, path
        )
        value: Any = "" if raw is None else str(raw).strip()
    elif root_name == "contract":
        value = ctx.contract if not path else _dig(ctx.contract or {}, path)
    elif root_name == "session":
        value = ctx.session if not path else _dig(ctx.session, path)
    else:  # intake
        value = ctx.intake if not path else _dig(ctx.intake, path)

    if spec["projection"] is not None:
        fields = spec["projection"]
        items = value if isinstance(value, list) else []
        projected = []
        for index, item in enumerate(items):
            if not isinstance(item, dict) or not item.get(fields[0]):
                continue
            row = {field: item.get(field) for field in fields}
            if spec["order"]:
                row["order"] = index
            projected.append(row)
        value = projected

    if spec["arith"]:
        try:
            value = int(value) + spec["arith"]
        except (TypeError, ValueError):
            raise TemplateError(
                f"{where or 'template'}: cannot increment non-integer "
                f"{'.'.join([root_name] + path)}"
            ) from None

    if spec["fill"] is not None:
        source = dict(value) if isinstance(value, dict) else {}
        for field, config_key in spec["fill"]:
            if not source.get(field):
                fallback = str(ctx.config.get(config_key) or "").strip()
                if fallback:
                    source[field] = fallback
        value = {field: source.get(field) for field, _ in spec["fill"]}
        if spec["complete"]:
            missing = [
                field for field, _ in spec["fill"] if not value.get(field)
            ]
            if missing:
                raise TemplateError(
                    f"{where or 'template'}: required fields are missing "
                    "after fill_missing (absent from the contract and "
                    "config): " + ", ".join(missing)
                )

    if spec["required"] and root_name == "config" and (
        value in (None, "")
    ):
        ctx.missing_required.append(".".join(path))
        return ""

    if not value and spec["default"] is not None:
        default = spec["default"]
        if isinstance(default, dict) and "root" in default or (
            isinstance(default, dict) and default.get("unique_prefix")
        ):
            value = evaluate_expression(default, ctx, where=where)
        else:
            value = default

    if spec["nonempty"] and not value:
        raise TemplateError(
            f"{where or 'template'}: "
            f"{'.'.join([root_name] + path)} resolved empty (!nonempty)"
        )
    return value


# ---------------------------------------------------------------------------
# Formula strings
# ---------------------------------------------------------------------------

_FORMULA_TOKEN_RE = re.compile(
    r"\{(#)?([A-Za-z_][A-Za-z0-9_.]*)"
    r"(\[-(\d+):\])?"
    r"(?::(\d+))?"
    r"(?:=((?:[^{}])*?))?\}"
)


def is_formula(value: Any) -> bool:
    return isinstance(value, str) and bool(_FORMULA_TOKEN_RE.search(value))


def render_formula(template: str, values: Dict[str, Any]) -> str:
    """Resolve ``{...}`` tokens against a flat/nested value dict."""

    def replace(match: re.Match) -> str:
        count, path, _slice_full, slice_n, bound, fallback = match.groups()
        value = _dig(values, path.split("."))
        if count:
            return str(len(value) if isinstance(value, (list, tuple, dict))
                       else 0)
        if slice_n:
            return str(value)[-int(slice_n):]
        if bound is not None:
            text = clean_text(value, int(bound))
            if not text and fallback is not None:
                return fallback
            return text
        if (value is None or value == "") and fallback is not None:
            return fallback
        return str(value)

    return _FORMULA_TOKEN_RE.sub(replace, template)


def validate_formula(template: str, where: str = "") -> None:
    """Load-time syntax check: every brace must belong to a valid token."""
    stripped = _FORMULA_TOKEN_RE.sub("", template)
    if "{" in stripped or "}" in stripped:
        raise TemplateError(
            f"{where or 'formula'}: malformed {{...}} token in "
            f"{template!r}"
        )


# ---------------------------------------------------------------------------
# Line specs (presentation sections)
# ---------------------------------------------------------------------------

def render_lines(spec: List[Any], values: Dict[str, Any]) -> List[str]:
    """Render a presentation line-spec list into text lines."""
    lines: List[str] = []
    for entry in spec:
        if isinstance(entry, str):
            lines.append(render_formula(entry, values))
            continue
        if not isinstance(entry, dict):
            raise TemplateError(f"invalid line spec entry: {entry!r}")
        if "each" in entry:
            items = _dig(values, str(entry["each"]).split("."))
            for item in items if isinstance(items, list) else []:
                lines.append(render_formula(
                    entry["line"], dict(values, item=item)
                ))
            continue
        condition = _dig(values, str(entry.get("if", "")).split("."))
        if not condition:
            continue
        if "append" in entry:
            appended = render_formula(entry["append"], values)
            if lines:
                lines[-1] += appended
            else:
                lines.append(appended)
        else:
            lines.append(render_formula(entry["line"], values))
    return lines


def render_inline(spec: List[Any], values: Dict[str, Any]) -> str:
    """Render an append-style line spec into one string."""
    text = ""
    for entry in spec:
        if isinstance(entry, str):
            text += render_formula(entry, values)
            continue
        if not isinstance(entry, dict):
            raise TemplateError(f"invalid inline spec entry: {entry!r}")
        condition = _dig(values, str(entry.get("if", "")).split("."))
        if not condition:
            continue
        key = "append" if "append" in entry else "line"
        text += render_formula(entry[key], values)
    return text


def validate_line_spec(spec: Any, where: str = "") -> None:
    if not isinstance(spec, list):
        raise TemplateError(f"{where}: line spec must be a list")
    for entry in spec:
        if isinstance(entry, str):
            validate_formula(entry, where)
            continue
        if not isinstance(entry, dict):
            raise TemplateError(f"{where}: invalid entry {entry!r}")
        keys = set(entry)
        if "each" in entry:
            if keys - {"each", "line"} or "line" not in entry:
                raise TemplateError(f"{where}: bad each-entry {entry!r}")
            validate_formula(entry["line"], where)
        elif "if" in entry:
            body = keys - {"if"}
            if body not in ({"line"}, {"append"}):
                raise TemplateError(f"{where}: bad if-entry {entry!r}")
            validate_formula(entry.get("line") or entry.get("append"),
                             where)
        else:
            raise TemplateError(f"{where}: entry needs if/each: {entry!r}")
