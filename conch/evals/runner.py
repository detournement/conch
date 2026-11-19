"""Run one pinned suite in disposable workers, separate from the chat session."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from ..config import local_only_enabled
from ..modelcheck import Candidate
from ..model_tuning import context_for, effort_for
from ..providers import (DEFAULT_API_KEY_ENVS, RAW_FNS, _billing_kind,
                         get_custom_base_url, get_ollama_base_url)
from .results import new_report, now, run_lock, save_report, scrub

_RUN_LOCK = threading.Lock()
_TRANSPORT_KEYS = (
    "base_url", "ollama_base_url", "ollama_num_ctx", "ollama_context_window",
    "ollama_temperature", "ollama_num_predict", "ollama_keep_alive", "ollama_think",
    "ollama_timeout", "custom_base_url", "custom_context_window", "custom_temperature",
    "custom_max_tokens", "custom_timeout", "seat_timeout", "bedrock_region",
)
_ENV_KEYS = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)


def select_target(config: dict, provider: str, model: str) -> tuple:
    """Resolve a route without startup recovery or any persisted setting changes."""
    provider = provider.strip().lower()
    model = model.strip()
    if not model or len(model) > 512 or any(ord(char) < 32 or ord(char) == 127 for char in model):
        raise ValueError("Select a non-empty model ID without control characters.")
    original_provider = str(config.get("provider", "")).lower()
    local_only = local_only_enabled(config, original_provider)
    values = {key: config[key] for key in _TRANSPORT_KEYS if key in config}
    if provider != original_provider:
        values.pop("base_url", None)
    overrides = {}
    if provider == "llamaidx":
        from ..llamaidx import resolve_llamaidx_model, llamaidx_selection_overrides
        entry = resolve_llamaidx_model(model, config)
        if entry is None:
            raise ValueError("The selected registry model is no longer available.")
        overrides = llamaidx_selection_overrides(entry)
        provider, model = overrides["provider"], overrides["model"]
    elif provider == original_provider and config.get("api_key_env"):
        overrides["api_key_env"] = config["api_key_env"]
    if provider not in RAW_FNS:
        raise ValueError("Unknown evaluation provider: " + provider)
    if local_only and provider not in ("ollama", "custom"):
        raise ValueError("local_only blocks cloud/seat evaluation; select a local route or explicitly change that policy.")
    values.update(Candidate(provider, model, "evaluation", overrides=overrides).config_updates())
    values["local_only"] = "true" if local_only else "false"
    if provider == "ollama":
        values["ollama_base_url"] = get_ollama_base_url(values)
    elif provider == "custom":
        values["custom_base_url"] = get_custom_base_url(values)
        if not values["custom_base_url"]:
            raise ValueError("Configure custom_base_url before evaluating a custom model.")
    effort = effort_for(config, provider, model)
    context = context_for(config, provider, model)
    settings = {}
    if effort:
        values["model_effort"] = f"{provider}/{model}={effort}"
        settings["reasoning_effort"] = effort
    elif provider == "ollama" and "ollama_think" in values:
        think = str(values["ollama_think"]).strip().lower()
        if think in ("true", "1", "yes", "on"):
            think = "on"
        elif think in ("false", "0", "no", "off"):
            think = "off"
        if think:
            settings["reasoning_effort"] = think
    if context:
        values["model_context"] = f"{provider}/{model}={context}"
        settings["context_length"] = context
    elif provider == "ollama" and values.get("ollama_num_ctx"):
        settings["context_length"] = int(values["ollama_num_ctx"])
    elif provider == "custom" and values.get("custom_context_window"):
        settings["context_length"] = int(values["custom_context_window"])
    values["max_output_tokens"] = "1024"
    billing = _billing_kind(provider, model, values)
    target = {"provider": provider, "requested_model": model, "observed_models": [],
              "identity": "unverified", "settings": settings,
              "billing": "subscription" if billing == "seat" else billing}
    return values, target


def worker_environment(config: dict) -> dict:
    """Only the selected transport's auth enters the child; never all loaded keys."""
    result = {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}
    provider = config["provider"]
    key = config.get("api_key_env") or DEFAULT_API_KEY_ENVS.get(provider, "")
    selected = [key] if key else []
    if provider == "bedrock":
        selected += ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                     "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION",
                     "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_CA_BUNDLE"]
    elif provider == "claude-code":
        selected += ["CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"]
    elif provider == "codex":
        selected += ["CODEX_HOME"]
    elif provider == "cerebras":
        selected += ["CEREBRAS_BASE_URL"]
    for name in selected:
        if name in os.environ:
            result[name] = os.environ[name]
    result["PYTHONUNBUFFERED"] = "1"
    return result


def _kill_group(process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _admission_snapshot(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            saved = handle.read(512 * 1024 + 1)
        if len(saved) <= 512 * 1024:
            snapshot = json.loads(saved)
            if isinstance(snapshot, dict):
                return snapshot
    except (OSError, ValueError, UnicodeError):
        pass
    return {}


def _recover_admissions(row: dict, path: Path) -> None:
    snapshot = _admission_snapshot(path)
    # A completed response is richer evidence than its pre-request checkpoint.
    if snapshot.get("calls", 0) > row["calls"]:
        row.update(snapshot)


def _worker_case(config: dict, case_id: str, root: Path, limits: dict,
                 deadline: float, progress) -> tuple:
    # -I ignores user site/PYTHONPATH. Explicitly locate this installed Conch,
    # not a package in the user's cwd or a second editable checkout.
    package_root = str(Path(__file__).resolve().parents[2])
    bootstrap = ("import sys; sys.path.insert(0, sys.argv[1]); "
                 "from conch.evals.worker import main; main()")
    checkpoint_path = root.parent / ("." + case_id + ".progress.json")
    payload = json.dumps({"config": config, "case_id": case_id, "root": str(root),
                          "limits": limits,
                          "deadline_at": time.time() + max(0.0, deadline - time.monotonic()),
                          "checkpoint_path": str(checkpoint_path)}, allow_nan=False).encode()
    if len(payload) > 65536:
        raise ValueError("Evaluation transport configuration is too large.")
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", bootstrap, package_root],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        cwd=str(root.parent), env=worker_environment(config), start_new_session=True,
    )
    interrupted = None
    output = b""
    started = time.monotonic()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                interrupted = "timeout"
                break
            try:
                output, _ = process.communicate(input=payload, timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                payload = None
                if progress is not None:
                    try:
                        progress({"type": "tick", "case_id": case_id,
                                  "elapsed_seconds": time.monotonic() - started})
                    except KeyboardInterrupt:
                        interrupted = "cancelled"
                        break
            except KeyboardInterrupt:
                interrupted = "cancelled"
                break
        if interrupted:
            try:
                process.send_signal(signal.SIGINT)
                output, _ = process.communicate(timeout=2)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                _kill_group(process)
                output, _ = process.communicate()
        if len(output) > 512 * 1024:
            raise ValueError("Evaluation worker exceeded its result limit.")
        try:
            result = json.loads(output) if output else None
        except (ValueError, UnicodeError):
            result = None
        if result is not None and (not isinstance(result, dict) or result.get("id") != case_id):
            result = None
        if result is None:
            result = dict(_admission_snapshot(checkpoint_path),
                          id=case_id, status="error", error_kind="harness",
                          failures=[f"Worker exited without a final result (exit {process.returncode})."])
        return result, interrupted
    finally:
        # The worker is its own process group. Nothing it owns may outlive a case.
        _kill_group(process)
        process.wait()
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()


def run_suite(config: dict, target: dict, *, deadline_seconds: int = 300,
              progress=None) -> dict:
    if isinstance(deadline_seconds, bool) or not isinstance(deadline_seconds, int) or not 1 <= deadline_seconds <= 3600:
        raise ValueError("Evaluation timeout must be between 1 and 3600 seconds.")
    if config.get("provider") != target.get("provider") or config.get("chat_model") != target.get("requested_model"):
        raise ValueError("Evaluation target does not match the selected transport.")
    if not _RUN_LOCK.acquire(blocking=False):
        raise RuntimeError("An evaluation is already running in this Conch process.")
    report = None
    try:
        report = new_report(target, deadline_seconds=deadline_seconds)
        deadline = time.monotonic() + deadline_seconds
        with run_lock(report["run_id"]):
            save_report(report)
            with tempfile.TemporaryDirectory(prefix="conch-eval-") as directory:
                for row in report["cases"]:
                    if time.monotonic() >= deadline:
                        report["status"] = "timed_out"
                        break
                    row["status"] = "running"
                    save_report(report)
                    if progress is not None:
                        progress({"type": "case_started", "case_id": row["id"]})
                    started = time.monotonic()
                    interruption = None
                    try:
                        result, interruption = _worker_case(
                            config, row["id"], Path(directory) / row["id"],
                            report["limits"], deadline, progress,
                        )
                        if result:
                            row.update(scrub(result))
                        if interruption:
                            row.update(status="error", error_kind=interruption,
                                       failures=["Evaluation cancelled." if interruption == "cancelled"
                                                 else "Evaluation deadline reached."])
                            row["usage"]["complete"] = False
                    except KeyboardInterrupt:
                        interruption = "cancelled"
                        _recover_admissions(row, Path(directory) / ("." + row["id"] + ".progress.json"))
                        row.update(status="error", error_kind="cancelled", failures=["Evaluation cancelled."])
                        row["usage"]["complete"] = False
                    except Exception as exc:
                        _recover_admissions(row, Path(directory) / ("." + row["id"] + ".progress.json"))
                        row.update(status="error", error_kind="harness",
                                   failures=[f"{type(exc).__name__}: {str(exc)[:1000]}"])
                        row["usage"]["complete"] = False
                    row["elapsed_seconds"] = round(time.monotonic() - started, 6)
                    for model in row["observed_models"]:
                        if model not in report["target"]["observed_models"]:
                            report["target"]["observed_models"].append(model)
                    attempted = [item for item in report["cases"] if item["status"] != "not_run"]
                    identities = {item["identity"] for item in attempted}
                    report["target"]["identity"] = ("fallback" if "fallback" in identities else
                                                         "observed" if identities == {"observed"} else "unverified")
                    if interruption:
                        report["status"] = "cancelled" if interruption == "cancelled" else "timed_out"
                    elif row["status"] == "error":
                        report["status"] = ("timed_out" if row["error_kind"] == "timeout"
                                            and time.monotonic() >= deadline else "error")
                    save_report(report)
                    if progress is not None:
                        progress({"type": "case_finished", "case_id": row["id"], "result": row})
                    if report["status"] != "running":
                        break
            if report["status"] == "running":
                report["status"] = "complete"
            report["finished_at"] = now()
            save_report(report)
        return report
    except KeyboardInterrupt:
        if report is None:
            raise
        report["status"] = "cancelled"
        report["finished_at"] = now()
        for row in report["cases"]:
            if row["status"] == "running":
                row.update(status="error", error_kind="cancelled",
                           failures=["Evaluation cancelled before this case completed."])
                row["usage"]["complete"] = False
        save_report(report)
        return report
    finally:
        _RUN_LOCK.release()
