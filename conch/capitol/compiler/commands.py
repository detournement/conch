"""``/compile`` — the ProcessCompiler command family (C1 review surface
+ C2 operate surface).

    /compile "<goal>"            run a compilation session, store card v1
    /compile list                compilations with status and card version
    /compile show <id> [vN]      the rendered card (or a version diff)
    /compile approve <id>        the authorization moment (origin-bound)
    /compile reject <id> [why]   record a rejection
    /compile revise <id> "<..>"  new card version via a fresh session
    /compile materialize <id>    provision + drill + supervising mission
    /compile rollback <id>       revert everything materialized so far
    /compile status <id>         receipts, drill evidence, mission

Interactive sessions only in v1: any non-local origin is refused with a
clear message (which also means no session or channel can ever approve a
card — approval is a user act at the shell). All state is the kernel's
event-sourced ``compilations`` aggregate; no daemon is required (the
session and materialization run in-process, the supervising mission runs
wherever missions run).
"""

from __future__ import annotations

import os
import re
import shlex
import time
from typing import Any, Dict, List, Optional

from ..errors import CapitolAuthError, CapitolError
from ..packs.templates import clean_text

USAGE = """
  \033[1;36m/compile — the ProcessCompiler (goal → reviewed card → governed process)\033[0m
    /compile "<goal>"              compile a goal into an Architecture Card
    /compile from-mission <id> ["goal"]   draft a card from a mission's journal (capture)
    /compile from-session [<conv-id>] ["goal"]   draft a card from a saved conversation (capture)
    /compile from-email [--rescan] ["goal"]   draft a card from the capture mailbox (capture)
    /compile from-history [N] "goal"      draft a card from recent shell history (capture)
    /compile list                  list compilations
    /compile show <id> [vN|--diff] render a card (or diff the last two versions)
    /compile approve <id>          approve the current card version (authorizes materialization)
    /compile reject <id> [reason]  reject the current card version
    /compile revise <id> "<guidance>"   recompile into a new card version
    /compile materialize <id>      provision the approved card, run the drill, bind the mission
    /compile rollback <id>         revert everything materialized so far
    /compile status <id>           status, receipts, drill evidence, mission
  \033[2mCards are kernel events (replay == live). Approval is origin-bound and local-only in v1;
  materialization requires capitol_admin=true and the local serving stack.\033[0m
"""

_STATUS_COLORS = {
    "compiled": "36", "approved": "32", "rejected": "31",
    "materialized": "33", "verified": "32", "operating": "1;32",
    "rolled_back": "35",
}


def _print(text: str = ""):
    print(text)


def _store():
    from ...kernel.store import MissionStore

    return MissionStore()


def _resolve(store, ref: str) -> Optional[Dict[str, Any]]:
    compilation = store.resolve_compilation(ref)
    if compilation is None:
        _print(f"\n  \033[31mNo compilation matching {ref!r} — reference"
               " by #N or id prefix (/compile list).\033[0m\n")
    return compilation


def _line(compilation: Dict[str, Any]) -> str:
    color = _STATUS_COLORS.get(compilation["status"], "0")
    goal = compilation["goal"]
    if len(goal) > 64:
        goal = goal[:61] + "..."
    return (
        f"\033[1m#{compilation.get('compilation_seq', '?')}\033[0m "
        f"\033[{color}m[{compilation['status']}]\033[0m {goal}  "
        f"\033[2mcard v{compilation['card_version']}  "
        f"{compilation['compilation_id'][:20]}…\033[0m"
    )


def _finish_compile(card: Dict[str, Any], *,
                    capture: Optional[Dict[str, Any]] = None):
    store = _store()
    try:
        compilation = store.create_compilation(
            card, actor=os.environ.get("USER", "user"), capture=capture,
        )
    finally:
        store.close()
    _print(f"\n  \033[1;32m✓ Compiled\033[0m {_line(compilation)}")
    questions = card.get("open_questions") or []
    if questions:
        _print(f"  \033[33m{len(questions)} open question(s) parked on"
               " the card:\033[0m")
        for question in questions[:6]:
            _print(f"    - {clean_text(question, 160)}")
    _print(
        f"  \033[2mreview: /compile show "
        f"{compilation['compilation_id'][:16]} — then approve/reject/"
        "revise\033[0m\n"
    )


def _cmd_compile(goal: str, config: dict):
    from .session import run_compile_session

    _print(f"\n  \033[2mcompiling: {clean_text(goal, 120)}\033[0m")
    _print("  \033[2mdiscovering the org and running the bounded design"
           " session …\033[0m")
    card = run_compile_session(config, goal)
    _finish_compile(card)


def _cmd_from_mission(tokens: List[str], config: dict):
    from .capture import capture_from_mission, compile_from_capture

    if not tokens:
        _print("\n  \033[2mUsage: /compile from-mission <mission-id> "
               "[\"goal\"]\033[0m\n")
        return
    store = _store()
    try:
        context = capture_from_mission(store, tokens[0])
    finally:
        store.close()
    goal = " ".join(tokens[1:]).strip().strip("\"'")
    _print(f"\n  \033[2mcapturing {context['label']} → drafting the "
           "card …\033[0m")
    card, provenance = compile_from_capture(config, context, goal=goal)
    _finish_compile(card, capture=provenance)


def _cmd_from_email(tokens: List[str], config: dict):
    from .capture import compile_from_capture
    from .capture_email import capture_from_email, reset_cursor

    tokens = list(tokens)
    if tokens and tokens[0] == "--rescan":
        tokens.pop(0)
        folder = str(config.get("capture_email_folder") or "").strip()
        if folder:
            reset_cursor(folder)
            _print(f"\n  \033[2mcursor reset for {folder!r} — "
                   "re-reading the folder.\033[0m")
    context, commit = capture_from_email(config)
    goal = " ".join(tokens).strip().strip("\"'")
    _print(f"\n  \033[2mcapturing {context['label']} → drafting the "
           "card …\033[0m")
    card, provenance = compile_from_capture(config, context, goal=goal)
    _finish_compile(card, capture=provenance)
    # Advance the UID cursor only now that the compilation is stored —
    # a failed synthesis re-reads the same window on retry.
    commit()


def _cmd_from_history(tokens: List[str], config: dict):
    from .capture import capture_from_history, compile_from_capture

    tokens = list(tokens)
    limit = 200
    if tokens and tokens[0].isdigit():
        limit = int(tokens.pop(0))
    goal = " ".join(tokens).strip().strip("\"'")
    if not goal:
        _print("\n  \033[2mUsage: /compile from-history [N] \"<goal>\""
               " — raw history is heterogeneous, so the goal must be"
               " explicit.\033[0m\n")
        return
    context = capture_from_history(limit)
    _print(f"\n  \033[2mcapturing {context['label']} → drafting the "
           "card …\033[0m")
    card, provenance = compile_from_capture(config, context, goal=goal)
    _finish_compile(card, capture=provenance)


def _cmd_from_session(tokens: List[str], config: dict):
    from .capture import (
        capture_from_conversation,
        compile_from_capture,
        resolve_conversation,
    )

    conv_ref = ""
    goal_tokens = list(tokens)
    # Conversation ids are short hex (uuid4().hex[:8]); a leading token
    # of 4–8 hex chars selects the conversation, everything else is the
    # goal. Quote the goal if its first word happens to be pure hex.
    if goal_tokens and re.fullmatch(r"[0-9a-f]{4,8}", goal_tokens[0]):
        conv_ref = goal_tokens.pop(0)
    conversation = resolve_conversation(conv_ref)
    context = capture_from_conversation(conversation)
    goal = " ".join(goal_tokens).strip().strip("\"'")
    _print(f"\n  \033[2mcapturing {context['label']} → drafting the "
           "card …\033[0m")
    card, provenance = compile_from_capture(config, context, goal=goal)
    _finish_compile(card, capture=provenance)


def _cmd_list(store):
    compilations = store.list_compilations()
    if not compilations:
        _print("\n  \033[2mNo compilations yet. Start one with "
               "/compile \"<goal>\".\033[0m\n")
        return
    _print(f"\n  \033[1;36mCompilations ({len(compilations)}):\033[0m")
    for compilation in compilations:
        _print("    " + _line(compilation))
    _print()


def _cmd_show(store, tokens: List[str]):
    from .card import diff_cards, render_card_markdown

    if not tokens:
        _print("\n  \033[2mUsage: /compile show <id> [vN|--diff]\033[0m\n")
        return
    compilation = _resolve(store, tokens[0])
    if compilation is None:
        return
    cid = compilation["compilation_id"]
    rest = tokens[1:]
    if rest and rest[0] == "--diff":
        latest = int(compilation["card_version"])
        if latest < 2:
            _print("\n  \033[2mOnly one card version — nothing to "
                   "diff.\033[0m\n")
            return
        old = store.compilation_card(cid, latest - 1)["card"]
        new = store.compilation_card(cid, latest)["card"]
        _print()
        _print(diff_cards(
            old, new, old_label=f"card v{latest - 1}",
            new_label=f"card v{latest}",
        ))
        _print()
        return
    version = None
    if rest and rest[0].lower().lstrip("v").isdigit():
        version = int(rest[0].lower().lstrip("v"))
    row = store.compilation_card(cid, version)
    if row is None:
        _print(f"\n  \033[31mNo card v{version} for {cid}.\033[0m\n")
        return
    _print()
    for line in render_card_markdown(
        row["card"], compilation=compilation,
        card_version=row["card_version"],
    ).splitlines():
        _print("  " + line)
    _print()


def _cmd_decide(store, verb: str, tokens: List[str], *,
                origin: str):
    from ...kernel.model import ApprovalError, KernelError

    if not tokens:
        _print(f"\n  \033[2mUsage: /compile {verb} <id>"
               + (" [reason]" if verb == "reject" else "") + "\033[0m\n")
        return
    compilation = _resolve(store, tokens[0])
    if compilation is None:
        return
    reason = " ".join(tokens[1:])
    try:
        result = store.decide_compilation(
            compilation["compilation_id"], verb,
            decided_by=os.environ.get("USER", "shell"),
            origin_channel=origin, reason=reason,
        )
    except (ApprovalError, KernelError) as exc:
        _print(f"\n  \033[31m{exc}\033[0m\n")
        return
    if verb == "approve":
        _print(
            f"\n  \033[1;32m✓ Approved\033[0m card "
            f"v{result['card_version']} \033[2m(digest "
            f"{result['digest'][:23]}… pinned — materialization honors "
            "exactly this version)\033[0m\n"
            f"  \033[2mnext: /compile materialize "
            f"{compilation['compilation_id'][:16]}\033[0m\n"
        )
    else:
        _print(
            f"\n  \033[1;32m✓ Rejected\033[0m card "
            f"v{result['card_version']}"
            + (f" \033[2m({clean_text(reason, 120)})\033[0m" if reason
               else "")
            + "\n  \033[2mrevise with /compile revise <id> "
            "\"<guidance>\".\033[0m\n"
        )


def _cmd_revise(store, tokens: List[str], config: dict):
    from .session import run_compile_session

    if len(tokens) < 2:
        _print("\n  \033[2mUsage: /compile revise <id> "
               "\"<guidance>\"\033[0m\n")
        return
    compilation = _resolve(store, tokens[0])
    if compilation is None:
        return
    cid = compilation["compilation_id"]
    guidance = " ".join(tokens[1:])
    prior = store.compilation_card(cid)["card"]
    _print("\n  \033[2mrecompiling with the prior card + guidance …"
           "\033[0m")
    card = run_compile_session(
        config, compilation["goal"], prior_card=prior, guidance=guidance,
    )
    from ...kernel.model import KernelError

    try:
        new_version = store.record_compilation_card(
            cid, card, actor=os.environ.get("USER", "user"),
            guidance=guidance,
        )
    except KernelError as exc:
        _print(f"\n  \033[31m{exc}\033[0m\n")
        return
    _print(
        f"\n  \033[1;32m✓ Card v{new_version} recorded\033[0m "
        "\033[2m(any prior approval is void; review with "
        f"/compile show {cid[:16]} --diff)\033[0m\n"
    )


def _cmd_materialize(store, tokens: List[str], config: dict):
    from .materialize import materialize_compilation, verify_compilation

    if not tokens:
        _print("\n  \033[2mUsage: /compile materialize <id>\033[0m\n")
        return
    compilation = _resolve(store, tokens[0])
    if compilation is None:
        return
    cid = compilation["compilation_id"]
    _print(f"\n  \033[1;36mmaterializing {cid}\033[0m")
    materialize_compilation(store, config, cid, log=_print)
    _print("  \033[1;32m✓ materialized\033[0m — running the validation "
           "gate …")
    outcome = verify_compilation(store, config, cid, log=_print)
    _print(
        f"\n  \033[1;32m✓ {outcome['status']}\033[0m — drill passed, "
        f"supervising mission {outcome['mission_id']} (dry-run)\n"
        "  \033[2mthe process is in shadow: schedules ship exactly as "
        "the card declared (disabled unless approved otherwise); "
        f"one-command revert: /compile rollback {cid[:16]}\033[0m\n"
    )


def _cmd_rollback(store, tokens: List[str], config: dict):
    from .materialize import rollback_compilation

    if not tokens:
        _print("\n  \033[2mUsage: /compile rollback <id>\033[0m\n")
        return
    compilation = _resolve(store, tokens[0])
    if compilation is None:
        return
    outcome = rollback_compilation(
        store, config, compilation["compilation_id"], log=_print,
    )
    _print(
        f"\n  \033[1;32m✓ rolled back\033[0m — "
        f"{len(outcome['reverted'])} step(s) reverted"
        + (f", {len(outcome['skipped'])} skipped "
           "(adopted/pre-existing assets are never deleted)"
           if outcome["skipped"] else "") + "\n"
    )


def _cmd_status(store, tokens: List[str]):
    if not tokens:
        _print("\n  \033[2mUsage: /compile status <id>\033[0m\n")
        return
    compilation = _resolve(store, tokens[0])
    if compilation is None:
        return
    _print("\n  " + _line(compilation))
    capture = store.compilation_capture(compilation["compilation_id"])
    if capture:
        from .capture import capture_provenance_line

        line = capture_provenance_line(capture)
        if line:
            _print(f"    \033[2m{line}\033[0m")
    if compilation.get("approved_version"):
        _print(
            f"    \033[2mapproved: card v{compilation['approved_version']}"
            f" by {compilation['decided_by']} "
            f"({compilation['decision_origin']})\033[0m"
        )
    versions = store.compilation_card_versions(
        compilation["compilation_id"]
    )
    for row in versions:
        stamp = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(float(row["created_at"]))
        )
        _print(f"    \033[2mcard v{row['card_version']}  "
               f"{row['digest'][:23]}…  {stamp}"
               + (f"  guidance: {clean_text(row['guidance'], 60)}"
                  if row.get("guidance") else "") + "\033[0m")
    materialization = compilation.get("materialization") or {}
    for step in materialization.get("steps") or []:
        marker = "adopted" if step.get("adopted") else "created"
        _print(f"    \033[2mmaterialized {step['step']} ({marker})\033[0m")
    if materialization.get("pack_dir"):
        _print(f"    \033[2mpack: {materialization['pack_dir']}\033[0m")
    if materialization.get("error"):
        _print(f"    \033[31mmaterialization error: "
               f"{clean_text(materialization['error'], 200)}\033[0m")
    drill = compilation.get("drill") or {}
    if drill:
        verdict = "PASSED" if drill.get("passed") else "FAILED"
        color = "32" if drill.get("passed") else "31"
        _print(f"    \033[{color}mdrill {verdict}\033[0m \033[2m"
               f"({len(drill.get('runs') or [])} run(s))"
               + (f" {clean_text(drill.get('error', ''), 160)}"
                  if drill.get("error") else "") + "\033[0m")
    if compilation.get("mission_id"):
        _print(f"    \033[2msupervising mission: "
               f"{compilation['mission_id']}\033[0m")
    _print()


def run_compile_command(arg: str, config: dict, *,
                        origin: str = "local") -> None:
    """Handle ``/compile …``. ``origin`` is the invoking surface; only
    the local interactive shell may drive the compiler in v1."""
    if origin != "local":
        _print(
            "\n  \033[31mThe process compiler is interactive-only in "
            f"v1: refusing /compile from origin {origin!r}. Run it at "
            "the local Conch shell.\033[0m\n"
        )
        return
    arg = (arg or "").strip()
    if not arg or arg.lower() in ("help", "-h", "--help"):
        _print(USAGE)
        return
    try:
        tokens = shlex.split(arg)
    except ValueError as exc:
        _print(f"\n  \033[31mInvalid arguments: {exc}\033[0m\n")
        return
    sub = tokens[0].lower()
    capture_verbs = {"from-mission", "from-session", "from-email",
                     "from-history"}
    known = {"list", "show", "approve", "reject", "revise",
             "materialize", "rollback", "status"} | capture_verbs
    try:
        if sub not in known:
            # The whole argument is the goal ("/compile \"<goal>\"").
            _cmd_compile(arg.strip("\"'"), config)
            return
        if sub in capture_verbs:
            # Capture is a discrete installed component: nothing
            # capture-related runs until /install capture enables it.
            from ...config import get_bool

            if not get_bool(config, "capture_enabled", False):
                _print(
                    "\n  \033[33mCapture is not installed.\033[0m "
                    "\033[2m/install capture enables capture→card "
                    "drafting (sessions, missions, email, shell "
                    "history).\033[0m\n"
                )
                return
        if sub == "from-mission":
            _cmd_from_mission(tokens[1:], config)
            return
        if sub == "from-session":
            _cmd_from_session(tokens[1:], config)
            return
        if sub == "from-email":
            _cmd_from_email(tokens[1:], config)
            return
        if sub == "from-history":
            _cmd_from_history(tokens[1:], config)
            return
        store = _store()
        try:
            rest = tokens[1:]
            if sub == "list":
                _cmd_list(store)
            elif sub == "show":
                _cmd_show(store, rest)
            elif sub == "approve":
                _cmd_decide(store, "approve", rest, origin=origin)
            elif sub == "reject":
                _cmd_decide(store, "reject", rest, origin=origin)
            elif sub == "revise":
                _cmd_revise(store, rest, config)
            elif sub == "materialize":
                _cmd_materialize(store, rest, config)
            elif sub == "rollback":
                _cmd_rollback(store, rest, config)
            elif sub == "status":
                _cmd_status(store, rest)
        finally:
            store.close()
    except CapitolAuthError as exc:
        _print(f"\n  \033[31mCapitol credential needed: "
               f"{clean_text(exc, 400)}\033[0m\n")
    except CapitolError as exc:
        _print(f"\n  \033[31mCompile: {clean_text(exc, 600)}\033[0m\n")
    except (KeyboardInterrupt, EOFError):
        _print("\n  \033[33mstopped.\033[0m\n")
