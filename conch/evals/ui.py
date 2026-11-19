"""Interactive /eval flow. Sharing always requires a separate explicit consent."""

from __future__ import annotations

import argparse
import json
import shlex
import sys

from ..config import local_only_enabled
from ..model_catalog import REGISTRY
from ..model_picker import pick_model
from .cases import CASES
from .results import list_reports, load_report, one_line, save_report, summary
from .runner import run_suite, select_target


def _confirm(prompt, input_fn) -> bool:
    # Keystrokes queued during inference must never become publishing consent.
    if sys.stdin.isatty():
        try:
            import termios
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        except (OSError, ValueError):
            pass
    try:
        return input_fn(prompt + " [y/N]: ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _share(report, config, input_fn):
    if local_only_enabled(config, str(config.get("provider", ""))):
        print("Sharing blocked by local_only. Results remain private; set local_only=false in Conch's config to permit sharing.")
        return
    from .public import default_public_label, public_report
    from .sharing import github_identity, refresh_submission, submit_report

    previous = report.get("sharing")
    if previous and previous.get("issue_number"):
        updated = refresh_submission(previous)
        report["sharing"] = updated
        save_report(report)
        if updated["status"] == "accepted":
            print("Accepted: " + one_line(updated["dataset_url"]))
            return
        print("Previous submission: " + one_line(updated.get("reason") or updated["status"]))
    label = default_public_label(report)
    if label is None:
        print("This model ID may be private. Choose a public model label; the original ID will not be uploaded.")
        label = input_fn("Public model label (Enter cancels): ").strip()
        if not label:
            return
    payload = public_report(report, model_label=label)
    identity = github_identity()
    print("\nPublic GitHub submission to detournement/conch (eval-results data branch)")
    print("GitHub account: " + one_line(identity) + " — this is not anonymous.")
    print("Exact public payload (no conversation or tool transcript):")
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False))
    if not _confirm("Publish this report", input_fn):
        print("Not shared. Your local report is unchanged.")
        return
    try:
        report["sharing"] = submit_report(payload, identity=identity)
        save_report(report)
        report["sharing"] = refresh_submission(report["sharing"])
        save_report(report)
    except (OSError, ValueError, RuntimeError) as exc:
        # A submission may exist even if confirmation failed. Retain its handle.
        if report.get("sharing"):
            report["sharing"]["reason"] = one_line(exc)[:600]
        else:
            report["sharing"] = {"status": "failed", "reason": one_line(exc)[:600]}
        save_report(report)
        raise
    sharing = report["sharing"]
    if sharing["status"] == "accepted":
        print("Accepted: " + one_line(sharing["dataset_url"]))
    elif sharing["status"] == "failed":
        print("Not accepted: " + one_line(sharing.get("reason", "GitHub ingestion failed.")))
    else:
        print("Submitted; ingestion is not yet confirmed: " + one_line(sharing["issue_url"]))
        print(f"Check acceptance or retry with /eval share {report['run_id']}")


def _browse(run_id, config, input_fn):
    if run_id:
        report = load_report(run_id)
    else:
        reports = list_reports()
        if not reports:
            print("No saved evaluations. Run /eval to select a model.")
            return
        page = 0
        while True:
            start = page * 20
            print("\nSaved evaluations (newest first)")
            for index, item in enumerate(reports[start:start + 20], start=start + 1):
                target = item.get("target", {})
                model = target.get("requested_model", item.get("error", "unknown"))
                provider = target.get("provider", "")
                passed = sum(case["status"] == "passed" for case in item.get("cases", []))
                total = len(item.get("cases", []))
                print(f"  {index:>3}. {item['run_id']}  {item['status']:<11} {passed}/{total}  "
                      + one_line(f"{provider}/{model}"))
            choice = input_fn("Number or full run ID; n/p page; Enter returns: ").strip()
            if not choice:
                return
            if choice == "n":
                if start + 20 < len(reports):
                    page += 1
                continue
            if choice == "p":
                page = max(0, page - 1)
                continue
            if choice.isdigit():
                index = int(choice) - 1
                if not 0 <= index < len(reports):
                    print("Choose a listed result.")
                    continue
                choice = reports[index]["run_id"]
            report = load_report(choice)
            break
    print("\n" + summary(report) + "\n")
    if input_fn("s shares/checks GitHub acceptance; Enter returns: ").strip().lower() == "s":
        _share(report, config, input_fn)


def _arguments(argument):
    parser = argparse.ArgumentParser(prog="/eval", add_help=False)
    parser.add_argument("target", nargs="?")
    parser.add_argument("--timeout", type=int, default=300, metavar="SECONDS")
    parser.add_argument("--help", "-h", action="store_true")
    try:
        options = parser.parse_args(shlex.split(argument))
    except SystemExit:
        return None
    if options.help:
        print("/eval [provider/model] [--timeout SECONDS]\n"
              "/eval results [run-id]\n/eval share <run-id>\n"
              "Select a model without changing chat; 12 fixed cases; local results; optional public sharing.")
        return None
    if not 1 <= options.timeout <= 3600:
        raise ValueError("Timeout must be between 1 and 3600 seconds.")
    return options


def run_eval_command(argument: str, config: dict, provider: str, model: str, *, input_fn=input):
    """Called only at the foreground idle boundary, while the shell UI is handed off."""
    try:
        words = shlex.split(argument)
        if words and words[0] == "results":
            if len(words) > 2:
                raise ValueError("Usage: /eval results [run-id]")
            _browse(words[1] if len(words) == 2 else None, config, input_fn)
            return
        if words and words[0] == "share":
            if len(words) != 2:
                raise ValueError("Usage: /eval share <run-id>")
            _share(load_report(words[1]), config, input_fn)
            return
        options = _arguments(argument)
        if options is None:
            return
        if options.target:
            chosen_provider, slash, chosen_model = options.target.partition("/")
            if not slash:
                raise ValueError("Use provider/model, or /eval to open the model picker.")
            if chosen_provider == REGISTRY:
                chosen_model = options.target
        else:
            entry = pick_model(config, provider, model, purpose="evaluation", selection_only=True)
            if entry is None:
                return
            chosen_provider, chosen_model = entry.provider, entry.model
        selected, target = select_target(config, chosen_provider, chosen_model)
        if target["provider"] in ("claude-code", "codex"):
            from ..seat_common import reject_native_evaluation
            reject_native_evaluation(target["provider"])
        print(f"\nEvaluate {one_line(target['provider'])}/{one_line(target['requested_model'])}")
        print(f"{len(CASES)} synthetic cases · at most 4 inference calls/case · {options.timeout}s total deadline")
        print("Up to 1024 output tokens/call where supported; exact provider identity/usage may be unavailable.")
        billing = target["billing"]
        print("Billing: " + {"api": "API charges apply; total price is not known in advance.",
                             "subscription": "subscription quota applies.",
                             "local": "local inference; no API price estimate.",
                             "unknown": "price unknown; do not assume free execution."}[billing])
        print("Chat history, primary model and roles are unchanged. Ctrl+C cancels this run only.")
        if not _confirm("Run this evaluation", input_fn):
            print("Evaluation not started.")
            return
        number = {case.id: index for index, case in enumerate(CASES, 1)}

        def progress(event):
            case_id = event["case_id"]
            if event["type"] == "case_started":
                print(f"  [{number[case_id]:02}/{len(CASES)}] {case_id}", flush=True)
            elif event["type"] == "tick" and sys.stdout.isatty():
                print(f"\r    Running · {event['elapsed_seconds']:.0f}s", end="", flush=True)
            elif event["type"] == "case_finished":
                if sys.stdout.isatty():
                    print("\r\033[K", end="")
                print("    " + event["result"]["status"], flush=True)

        report = run_suite(selected, target, deadline_seconds=options.timeout, progress=progress)
        print("\n" + summary(report) + "\n")
        if report["status"] == "complete":
            if input_fn("s previews public GitHub sharing; Enter keeps this local: ").strip().lower() == "s":
                _share(report, config, input_fn)
        else:
            print(f"Results remain local. Share later with /eval share {report['run_id']}")
    except (EOFError, KeyboardInterrupt):
        print("\nEvaluation menu closed; any completed results remain saved.")
    except (OSError, ValueError, RuntimeError) as exc:
        print("\nEval: " + one_line(exc)[:1200] + "\n")
