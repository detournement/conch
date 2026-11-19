"""Per-case admission limits and transport provenance for isolated evaluations.

This is a runtime policy, not an OS sandbox. The caller supplies guarded fixture
clients and owns worker isolation and cancellation.
"""
from __future__ import annotations

import copy
import json
import math
import time


class EvaluationError(RuntimeError):
    def __init__(self, kind: str, detail: str):
        self.kind = kind
        self.detail = detail
        super().__init__(detail)


class EvaluationPolicy:
    def __init__(self, provider: str, model: str, *, max_calls=4,
                 max_tool_calls=12, deadline=0.0, output_tokens=1024,
                 on_admission=None):
        if not provider or not model:
            raise ValueError("evaluation requires an explicit provider and model")
        for value in (max_calls, max_tool_calls, output_tokens):
            if type(value) is not int or value < 1:
                raise ValueError("evaluation limits must be positive integers")
        if not math.isfinite(deadline) or deadline < 0:
            raise ValueError("evaluation deadline must be finite and nonnegative")
        self.provider = provider.lower()
        self.model = model
        self.max_calls = max_calls
        self.max_tool_calls = max_tool_calls
        self.deadline = deadline
        self.output_tokens = output_tokens
        self.on_admission = on_admission
        self.calls = 0
        self.tool_calls = 0
        self.attempts = []
        self.observed_models = []
        self._identity_complete = True
        self._fallback = False
        self._responses = 0
        self._usage_complete = True
        self._tokens = {"input_tokens": None, "output_tokens": None}
        self._context_lengths = []

    def check_deadline(self):
        if self.deadline and time.monotonic() >= self.deadline:
            raise EvaluationError("timeout", "Evaluation deadline exceeded")

    def _check_route(self, config, provider):
        self.check_deadline()
        if (provider.lower() != self.provider
                or config.get("provider", provider).lower() != self.provider
                or (config.get("chat_model") or config.get("model")) != self.model):
            self.reject_fallback()
        if self.provider in ("claude-code", "codex"):
            from .seat_common import reject_native_evaluation
            reject_native_evaluation(self.provider)

    def configure(self, config, provider):
        self._check_route(config, provider)
        copied = dict(config)
        copied["provider"] = self.provider
        copied["chat_model"] = copied["model"] = self.model
        copied["_evaluation_policy"] = self
        copied["max_output_tokens"] = self.output_tokens
        copied["custom_max_tokens"] = self.output_tokens
        copied["ollama_num_predict"] = self.output_tokens
        return copied

    def begin_call(self, config, provider):
        self._check_route(config, provider)
        if self.calls >= self.max_calls:
            raise EvaluationError("limit", "Evaluation model-call budget exhausted")
        self.calls += 1
        if self.on_admission is not None:
            self.on_admission(self.snapshot())

    def reject_fallback(self):
        self._fallback = True
        raise EvaluationError("model_fallback", "Evaluation route changed or native model fallback occurred")

    def record_attempts(self, calls):
        """Capture requests before normalization, schema rejection or dispatch.

        Argument payloads and hostile oversized batches have explicit storage
        bounds. Overflow aborts the case rather than dispatching a partial batch.
        """
        if calls is None:
            return
        if not isinstance(calls, list):
            calls = [calls]
        overflow = False
        for call in calls:
            self.tool_calls += 1
            if len(self.attempts) >= 1024:
                overflow = True
                continue
            fn = call.get("function", call) if isinstance(call, dict) else {}
            if not isinstance(fn, dict):
                fn = {}
            arguments = fn.get("arguments", fn.get("input"))
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (ValueError, TypeError):
                    pass
            try:
                encoded = json.dumps(arguments, ensure_ascii=False, allow_nan=False)
                arguments = json.loads(encoded) if len(encoded) <= 8192 else encoded[:8192]
            except (ValueError, TypeError, RecursionError):
                arguments = "[invalid JSON arguments]"
            self.attempts.append({"name": str(fn.get("name") or "")[:512],
                                  "arguments": arguments})
        if overflow or self.tool_calls > self.max_tool_calls:
            raise EvaluationError("limit", "Evaluation tool-attempt budget exhausted; batch not executed")
        self.check_deadline()

    def record_context_length(self, value):
        """Record each request's context; None means server-managed."""
        self._context_lengths.append(value)

    def observe(self, response):
        self._responses += 1
        metadata = response.get("_evaluation", {})
        models = metadata.get("observed_models", [])
        if not models:
            self._identity_complete = False
        for model in models:
            if isinstance(model, str) and model and model not in self.observed_models:
                self.observed_models.append(model)
        usage = metadata.get("usage", {})
        for key in self._tokens:
            value = usage.get(key)
            if type(value) is int and value >= 0:
                self._tokens[key] = (self._tokens[key] or 0) + value
            else:
                self._usage_complete = False
        if metadata.get("complete") is not True:
            self._usage_complete = False
        if metadata.get("fallback"):
            self.reject_fallback()

    def provider_error(self, detail):
        """Classify locally; never persist an arbitrary credential-bearing error."""
        text = str(detail).lower()
        if "native tool" in text or "native execution" in text or "native subagent" in text:
            kind = "native_execution"
        elif "unsupported" in text or "not installed" in text:
            kind = "unsupported"
        elif any(word in text for word in ("429", "rate limit", "quota")):
            kind = "rate_limit"
        elif any(word in text for word in ("401", "403", "api key", "api_key", "login", "authentication", "unauthorized")):
            kind = "auth"
        elif "timed out" in text or "timeout" in text:
            kind = "timeout"
        elif any(word in text for word in ("connection", "unreachable", "urlopen", "name resolution", "refused")):
            kind = "unreachable"
        else:
            kind = "provider"
        raise EvaluationError(kind, "Selected evaluation transport failed (" + kind + ")")

    def snapshot(self):
        complete = self._usage_complete and self.calls > 0 and self._responses == self.calls
        identity = ("fallback" if self._fallback else "observed"
                    if self.observed_models and self._identity_complete and self._responses == self.calls
                    else "unverified")
        return {"calls": self.calls,
                "usage": dict(self._tokens, complete=complete),
                "observed_models": list(self.observed_models),
                "identity": identity,
                "context_lengths": self._context_lengths + [None] * (self.calls - len(self._context_lengths)),
                "attempts": copy.deepcopy(self.attempts)}
