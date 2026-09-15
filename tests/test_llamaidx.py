"""llama-idx registry discovery (plan 2.3): listing, selection routing
through the existing adapters, probe-on-select belt-and-braces, down/
degraded handling, namespacing, local_only policy, and fallback tiers."""

import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from conch.commands import handle_slash_command
from conch.config import load_config
from conch.llamaidx import (
    clear_llamaidx_cache,
    fetch_llamaidx_catalog,
    get_llamaidx_url,
    list_llamaidx_models,
    llamaidx_fallback_candidates,
    llamaidx_selection_overrides,
    resolve_llamaidx_model,
)
from conch.providers import (
    RAW_FNS,
    clear_local_model_caches,
    get_fallback_chain,
    is_local_inference_url,
)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _dispatch(self, method):
        fake = self.server.fake
        length = int(self.headers.get("Content-Length") or 0)
        body = None
        if length:
            try:
                body = json.loads(self.rfile.read(length).decode())
            except ValueError:
                body = None
        with fake.lock:
            fake.requests.append((method, self.path))
        result = fake.handle(method, self.path, body)
        if result is None:
            payload, code = {"error": "not found"}, 404
        else:
            code, payload = result
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def log_message(self, format, *args):  # noqa: A002
        pass


class _FakeServer:
    def __init__(self):
        self.lock = threading.RLock()
        self.requests = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.fake = self
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True,
        )

    @property
    def base_url(self):
        host, port = self.httpd.server_address[:2]
        return "http://{}:{}".format(host, port)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self._thread.join(timeout=5)
        self.httpd.server_close()

    def request_count(self, method, path_prefix):
        with self.lock:
            return sum(
                1 for m, p in self.requests
                if m == method and p.startswith(path_prefix)
            )


class FakeRegistry(_FakeServer):
    """Serves GET /v1/inference with a scripted provider list."""

    def __init__(self):
        super().__init__()
        self.providers = []

    def handle(self, method, path, body):
        if method == "GET" and path.startswith("/v1/inference"):
            return 200, {
                "generated_at": "2026-09-15T00:00:00Z",
                "registry_version": "0.1.0",
                "providers": self.providers,
            }
        return None

    def provider_entry(self, *, name, flavor, base_url, status="up",
                       models=None, auth_required=False, api_key_env=None):
        return {
            "id": "x" * 12,
            "name": name,
            "flavor": flavor,
            "base_url": base_url,
            "status": status,
            "labels": {},
            "auth_required": auth_required,
            "api_key_env": api_key_env,
            "models": models or [],
        }

    @staticmethod
    def model_entry(model_id, *, display_id=None, ctx=32768, tools=True,
                    loaded=True):
        return {
            "id": model_id,
            "display_id": display_id or model_id,
            "ctx": ctx,
            "tools": tools,
            "quant": "Q4_K_M",
            "loaded": loaded,
            "probed_at": "2026-09-15T00:00:00Z",
        }


class FakeLlamaCppBox(_FakeServer):
    """The provider itself: enough llama.cpp surface for conch's custom
    adapter (models list, props, probe + chat completions)."""

    def __init__(self, models=("qwen3-32b",), tool_capable=True):
        super().__init__()
        self.models = list(models)
        self.tool_capable = tool_capable
        self.chat_reply = "Hello from the fake llama.cpp box."

    def handle(self, method, path, body):
        if method == "GET" and (path == "/v1/models" or path == "/models"):
            return 200, {
                "object": "list",
                "data": [{"id": m, "meta": {"n_ctx_train": 32768}}
                         for m in self.models],
            }
        if method == "GET" and path in ("/props", "/v1/props"):
            return 200, {"default_generation_settings": {"n_ctx": 32768}}
        if method == "POST" and path == "/v1/chat/completions":
            model = (body or {}).get("model", "")
            if model not in self.models:
                return 404, {"error": {"message": "no such model"}}
            tools = body.get("tools") or []
            is_probe = any(
                (t.get("function") or {}).get("name") == "conch_tool_probe"
                for t in tools
            )
            if is_probe:
                if not self.tool_capable:
                    return 200, {
                        "choices": [{"message": {"content": "conch-ok"}}]
                    }
                return 200, {
                    "choices": [{
                        "message": {
                            "tool_calls": [{
                                "id": "probe-1",
                                "type": "function",
                                "function": {
                                    "name": "conch_tool_probe",
                                    "arguments": '{"token":"conch-ok"}',
                                },
                            }]
                        }
                    }]
                }
            return 200, {
                "choices": [{"message": {"content": self.chat_reply}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 6},
            }
        return None


class FakeOllamaBox(_FakeServer):
    def __init__(self, models=("qwen3:8b",), tools=True):
        super().__init__()
        self.models = list(models)
        self.tools = tools

    def handle(self, method, path, body):
        if method == "GET" and path == "/api/version":
            return 200, {"version": "0.34.0-fake"}
        if method == "GET" and path == "/api/tags":
            return 200, {
                "models": [
                    {"name": m, "model": m, "digest": "sha256:d-" + m}
                    for m in self.models
                ]
            }
        if method == "POST" and path == "/api/show":
            name = (body or {}).get("model") or (body or {}).get("name")
            if name not in self.models:
                return 404, {"error": "unknown"}
            caps = ["completion"] + (["tools"] if self.tools else [])
            return 200, {
                "capabilities": caps,
                "model_info": {"qwen3.context_length": 40960},
            }
        if method == "GET" and path == "/api/ps":
            return 200, {"models": []}
        return None


def _quiet():
    return patch("sys.stdout", new_callable=io.StringIO)


class ConfigKeyTests(unittest.TestCase):
    def test_env_keys_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "XDG_CONFIG_HOME": tmp,
                "CONCH_LLAMAIDX_URL": "http://registry.example:8642",
                "CONCH_LLAMAIDX_TOKEN_ENV": "MY_REGISTRY_TOKEN",
            }
            with patch.dict(os.environ, env, clear=True), patch.object(
                Path, "home", return_value=Path(tmp) / "home"
            ):
                config = load_config()
        self.assertEqual(config["llamaidx_url"], "http://registry.example:8642")
        self.assertEqual(config["llamaidx_token_env"], "MY_REGISTRY_TOKEN")

    def test_unset_means_off(self):
        self.assertEqual(get_llamaidx_url({}), "")
        self.assertIsNone(fetch_llamaidx_catalog({}))
        self.assertIsNone(list_llamaidx_models({}))


class ListingTests(unittest.TestCase):
    def setUp(self):
        clear_local_model_caches()
        self.registry = FakeRegistry().start()
        self.addCleanup(self.registry.stop)
        self.config = {
            "provider": "anthropic",
            "llamaidx_url": self.registry.base_url,
        }

    def test_namespaced_entries_from_catalog(self):
        self.registry.providers = [
            self.registry.provider_entry(
                name="gpubox", flavor="llamacpp",
                base_url="http://192.0.2.50:8080",
                models=[self.registry.model_entry("qwen3-32b", ctx=40960)],
            )
        ]
        entries = list_llamaidx_models(self.config)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["name"], "llamaidx/gpubox/qwen3-32b")
        self.assertEqual(entry["flavor"], "llamacpp")
        self.assertEqual(entry["base_url"], "http://192.0.2.50:8080")
        self.assertEqual(entry["ctx"], 40960)
        self.assertFalse(entry["degraded"])

    def test_registry_verdict_gates_listing(self):
        self.registry.providers = [
            self.registry.provider_entry(
                name="gpubox", flavor="llamacpp",
                base_url="http://192.0.2.50:8080",
                models=[
                    self.registry.model_entry("good"),
                    self.registry.model_entry("unprobed", tools=None),
                    self.registry.model_entry("bad", tools=False),
                ],
            )
        ]
        names = [e["name"] for e in list_llamaidx_models(self.config)]
        self.assertEqual(names, ["llamaidx/gpubox/good"])

    def test_down_providers_dropped_even_if_registry_leaks_them(self):
        self.registry.providers = [
            self.registry.provider_entry(
                name="deadbox", flavor="llamacpp",
                base_url="http://192.0.2.51:8080", status="down",
                models=[self.registry.model_entry("m")],
            ),
            self.registry.provider_entry(
                name="livebox", flavor="ollama",
                base_url="http://192.0.2.52:11434",
                models=[self.registry.model_entry("qwen3:8b")],
            ),
        ]
        names = [e["name"] for e in list_llamaidx_models(self.config)]
        self.assertEqual(names, ["llamaidx/livebox/qwen3:8b"])

    def test_degraded_provider_listed_with_marker(self):
        self.registry.providers = [
            self.registry.provider_entry(
                name="warmup", flavor="llamacpp",
                base_url="http://192.0.2.53:8080", status="degraded",
                models=[self.registry.model_entry("m")],
            )
        ]
        entries = list_llamaidx_models(self.config)
        self.assertTrue(entries[0]["degraded"])
        with _quiet() as out:
            handle_slash_command(
                "/models", dict(self.config), "anthropic",
                "claude-sonnet-5", lambda v: None,
            )
        rendered = out.getvalue()
        self.assertIn("llamaidx/warmup/m", rendered)
        self.assertIn("degraded", rendered)

    def test_models_command_shows_unreachable_registry(self):
        self.registry.stop()
        # Replace with a closed port; nothing listens there anymore.
        with _quiet() as out:
            handle_slash_command(
                "/models", dict(self.config), "anthropic",
                "claude-sonnet-5", lambda v: None,
            )
        self.assertIn("registry unreachable", out.getvalue())
        self.registry._thread = threading.Thread(target=lambda: None)
        self.registry._thread.start()
        self.registry.stop = lambda: None  # already stopped

    def test_unconfigured_registry_makes_zero_requests(self):
        config = {"provider": "anthropic"}
        with _quiet() as out:
            handle_slash_command(
                "/models", config, "anthropic", "claude-sonnet-5",
                lambda v: None,
            )
        self.assertNotIn("llamaidx", out.getvalue())
        self.assertEqual(len(self.registry.requests), 0)

    def test_catalog_fetch_is_cached_and_cleared(self):
        self.registry.providers = []
        fetch_llamaidx_catalog(self.config)
        fetch_llamaidx_catalog(self.config)
        self.assertEqual(self.registry.request_count("GET", "/v1/inference"), 1)
        clear_llamaidx_cache()
        fetch_llamaidx_catalog(self.config)
        self.assertEqual(self.registry.request_count("GET", "/v1/inference"), 2)

    def test_read_auth_token_env_is_sent_never_stored(self):
        captured = {}
        original = self.registry.handle

        def spy(method, path, body):
            return original(method, path, body)

        self.registry.handle = spy
        with patch.dict(os.environ, {"REG_TOKEN": "sekret-token-value"}):
            config = dict(self.config, llamaidx_token_env="REG_TOKEN")
            fetch_llamaidx_catalog(config, force_refresh=True)
        self.assertNotIn("sekret-token-value", json.dumps(config))
        del captured


class SelectionTests(unittest.TestCase):
    def setUp(self):
        clear_local_model_caches()
        self.registry = FakeRegistry().start()
        self.addCleanup(self.registry.stop)

    def _config(self):
        return {
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "chat_model": "claude-sonnet-5",
            "llamaidx_url": self.registry.base_url,
        }

    def test_llamacpp_flavor_routes_through_custom_adapter(self):
        box = FakeLlamaCppBox().start()
        self.addCleanup(box.stop)
        self.registry.providers = [
            self.registry.provider_entry(
                name="gpubox", flavor="llamacpp", base_url=box.base_url,
                models=[self.registry.model_entry("qwen3-32b")],
            )
        ]
        config = self._config()
        with _quiet():
            result = handle_slash_command(
                "/model llamaidx/gpubox/qwen3-32b", config, "anthropic",
                "claude-sonnet-5", lambda v: None,
            )
        self.assertEqual(result, ("custom", "qwen3-32b", RAW_FNS["custom"]))
        self.assertEqual(config["provider"], "custom")
        self.assertEqual(config["custom_base_url"], box.base_url + "/v1")
        self.assertEqual(config["custom_model"], "qwen3-32b")
        # Probe-on-select ran against the box itself (belt and braces).
        self.assertGreaterEqual(
            box.request_count("POST", "/v1/chat/completions"), 1
        )

    def test_ollama_flavor_routes_through_ollama_adapter(self):
        box = FakeOllamaBox().start()
        self.addCleanup(box.stop)
        self.registry.providers = [
            self.registry.provider_entry(
                name="minibox", flavor="ollama", base_url=box.base_url,
                models=[self.registry.model_entry("qwen3:8b")],
            )
        ]
        config = self._config()
        with _quiet():
            result = handle_slash_command(
                "/model llamaidx/minibox/qwen3:8b", config, "anthropic",
                "claude-sonnet-5", lambda v: None,
            )
        self.assertEqual(result, ("ollama", "qwen3:8b", RAW_FNS["ollama"]))
        self.assertEqual(config["ollama_base_url"], box.base_url)
        # conch's own capability check consulted /api/show.
        self.assertGreaterEqual(box.request_count("POST", "/api/show"), 1)

    def test_probe_on_select_failure_refuses_despite_registry_verdict(self):
        box = FakeLlamaCppBox(tool_capable=False).start()
        self.addCleanup(box.stop)
        self.registry.providers = [
            self.registry.provider_entry(
                name="gpubox", flavor="llamacpp", base_url=box.base_url,
                models=[self.registry.model_entry("qwen3-32b")],  # stale yes
            )
        ]
        config = self._config()
        with _quiet() as out:
            result = handle_slash_command(
                "/model llamaidx/gpubox/qwen3-32b", config, "anthropic",
                "claude-sonnet-5", lambda v: None,
            )
        self.assertIsNone(result)
        self.assertEqual(config["provider"], "anthropic")  # nothing committed
        self.assertIn("trust the probe", out.getvalue())

    def test_unknown_model_selection_fails_with_catalog_hint(self):
        self.registry.providers = [
            self.registry.provider_entry(
                name="gpubox", flavor="llamacpp",
                base_url="http://192.0.2.50:8080",
                models=[self.registry.model_entry("qwen3-32b")],
            )
        ]
        config = self._config()
        with _quiet() as out:
            result = handle_slash_command(
                "/model llamaidx/gpubox/nope", config, "anthropic",
                "claude-sonnet-5", lambda v: None,
            )
        self.assertIsNone(result)
        self.assertIn("not in the registry catalog", out.getvalue())
        self.assertIn("llamaidx/gpubox/qwen3-32b", out.getvalue())

    def test_down_provider_model_cannot_be_selected(self):
        self.registry.providers = [
            self.registry.provider_entry(
                name="deadbox", flavor="llamacpp",
                base_url="http://192.0.2.51:8080", status="down",
                models=[self.registry.model_entry("m")],
            )
        ]
        config = self._config()
        with _quiet() as out:
            result = handle_slash_command(
                "/model llamaidx/deadbox/m", config, "anthropic",
                "claude-sonnet-5", lambda v: None,
            )
        self.assertIsNone(result)
        self.assertIn("not in the registry catalog", out.getvalue())

    def test_verbatim_model_id_also_resolves(self):
        self.registry.providers = [
            self.registry.provider_entry(
                name="gpubox", flavor="llamacpp",
                base_url="http://192.0.2.50:8080",
                models=[self.registry.model_entry(
                    "../models/qwen3-32b-Q4_K_M.gguf",
                    display_id="qwen3-32b-Q4_K_M",
                )],
            )
        ]
        entry = resolve_llamaidx_model(
            "llamaidx/gpubox/qwen3-32b-Q4_K_M", {"llamaidx_url": self.registry.base_url,
                                                 "provider": "anthropic"},
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry["model_id"], "../models/qwen3-32b-Q4_K_M.gguf")
        overrides = llamaidx_selection_overrides(entry)
        self.assertEqual(overrides["custom_model"],
                         "../models/qwen3-32b-Q4_K_M.gguf")

    def test_auth_required_provider_carries_api_key_env_name(self):
        entry = {
            "flavor": "llamacpp", "base_url": "http://192.0.2.50:8080",
            "model_id": "m", "auth_required": True,
            "api_key_env": "GPUBOX_API_KEY",
        }
        overrides = llamaidx_selection_overrides(entry)
        self.assertEqual(overrides["api_key_env"], "GPUBOX_API_KEY")


class LocalOnlyTests(unittest.TestCase):
    def setUp(self):
        clear_local_model_caches()

    def test_ts_net_and_cgnat_addresses_are_local(self):
        self.assertTrue(
            is_local_inference_url("http://gpubox.tail1234.ts.net:8080")
        )
        self.assertTrue(is_local_inference_url("http://100.64.31.5:8080"))
        self.assertFalse(
            is_local_inference_url("https://inference.example.com:8080")
        )

    def test_nonlocal_providers_excluded_under_local_only(self):
        registry = FakeRegistry().start()
        self.addCleanup(registry.stop)
        registry.providers = [
            registry.provider_entry(
                name="cloudbox", flavor="openai",
                base_url="https://inference.example.com",
                models=[registry.model_entry("m1")],
            ),
            registry.provider_entry(
                name="lanbox", flavor="llamacpp",
                base_url="http://192.0.2.50:8080",
                models=[registry.model_entry("m2")],
            ),
        ]
        config = {
            "provider": "ollama",  # local_only auto-on
            "llamaidx_url": registry.base_url,
        }
        names = [e["name"] for e in list_llamaidx_models(config)]
        self.assertEqual(names, ["llamaidx/lanbox/m2"])
        # Explicit opt-out restores the full view.
        clear_llamaidx_cache()
        config["local_only"] = "false"
        names = [e["name"] for e in list_llamaidx_models(config)]
        self.assertEqual(len(names), 2)

    def test_nonlocal_registry_url_blocked_without_a_request(self):
        config = {
            "provider": "ollama",
            "llamaidx_url": "https://registry.example.com:8642",
        }
        self.assertIsNone(fetch_llamaidx_catalog(config))


class FallbackTests(unittest.TestCase):
    def setUp(self):
        clear_local_model_caches()
        self.registry = FakeRegistry().start()
        self.addCleanup(self.registry.stop)

    def test_registry_tier_sits_between_local_and_cloud(self):
        box = FakeLlamaCppBox(models=["current-model"]).start()
        self.addCleanup(box.stop)
        self.registry.providers = [
            self.registry.provider_entry(
                name="otherbox", flavor="llamacpp",
                base_url="http://192.0.2.60:8080",
                models=[self.registry.model_entry("rescue-32b", ctx=65536)],
            ),
            self.registry.provider_entry(
                name="ollamabox", flavor="ollama",
                base_url="http://192.0.2.61:11434",
                models=[self.registry.model_entry("qwen3:8b", ctx=32768)],
            ),
        ]
        config = {
            "provider": "custom",
            "custom_base_url": box.base_url + "/v1",
            "custom_model": "current-model",
            "local_only": "false",
            "llamaidx_url": self.registry.base_url,
        }
        with patch.dict(os.environ, {"OPENAI_API_KEY": "set"}, clear=False):
            chain = get_fallback_chain("custom", "current-model", config)
        names = [model for _, model, _ in chain]
        self.assertIn("llamaidx/otherbox/rescue-32b", names)
        self.assertIn("llamaidx/ollamabox/qwen3:8b", names)
        cloud_index = names.index("gpt-4o-mini")
        self.assertLess(names.index("llamaidx/otherbox/rescue-32b"), cloud_index)
        self.assertLess(names.index("llamaidx/ollamabox/qwen3:8b"), cloud_index)
        # Same-flavor (custom) candidates come before cross-flavor ones.
        self.assertLess(
            names.index("llamaidx/otherbox/rescue-32b"),
            names.index("llamaidx/ollamabox/qwen3:8b"),
        )
        # Mapped providers are real adapters.
        by_name = {model: provider for provider, model, _ in chain}
        self.assertEqual(by_name["llamaidx/otherbox/rescue-32b"], "custom")
        self.assertEqual(by_name["llamaidx/ollamabox/qwen3:8b"], "ollama")

    def test_current_box_and_degraded_providers_are_skipped(self):
        box = FakeLlamaCppBox(models=["current-model"]).start()
        self.addCleanup(box.stop)
        self.registry.providers = [
            self.registry.provider_entry(
                name="samebox", flavor="llamacpp", base_url=box.base_url,
                models=[self.registry.model_entry("current-model")],
            ),
            self.registry.provider_entry(
                name="warmup", flavor="llamacpp",
                base_url="http://192.0.2.62:8080", status="degraded",
                models=[self.registry.model_entry("loading-model")],
            ),
        ]
        config = {
            "provider": "custom",
            "custom_base_url": box.base_url + "/v1",
            "custom_model": "current-model",
            "llamaidx_url": self.registry.base_url,
        }
        candidates = llamaidx_fallback_candidates(config, "custom", "current-model")
        self.assertEqual(candidates, [])

    def test_no_registry_means_no_tier_and_no_error(self):
        config = {"provider": "ollama"}
        with patch(
            "conch.providers.list_ollama_models", return_value=["llama:latest"]
        ):
            chain = get_fallback_chain("ollama", "llama:latest", config)
        self.assertFalse(any(m.startswith("llamaidx/") for _, m, _ in chain))


class RuntimeFallbackTests(unittest.TestCase):
    """The runtime resolves namespaced candidates through the adapters."""

    def setUp(self):
        clear_local_model_caches()

    def test_failed_custom_turn_falls_back_to_registry_box(self):
        from conch.runtime import chat_turn
        from conch.providers import raw_custom

        registry = FakeRegistry().start()
        self.addCleanup(registry.stop)
        rescue_box = FakeLlamaCppBox(models=["rescue-32b"]).start()
        self.addCleanup(rescue_box.stop)
        rescue_box.chat_reply = "rescued by the registry box"
        registry.providers = [
            registry.provider_entry(
                name="rescuebox", flavor="llamacpp",
                base_url=rescue_box.base_url,
                models=[registry.model_entry("rescue-32b")],
            )
        ]
        config = {
            "provider": "custom",
            # Nothing listens here: the primary box is gone.
            "custom_base_url": "http://127.0.0.1:9/v1",
            "custom_model": "gone-model",
            "chat_model": "gone-model",
            "model": "gone-model",
            "llamaidx_url": registry.base_url,
        }
        with patch("conch.runtime.time.sleep"), \
             patch("sys.stderr", io.StringIO()), \
             patch("sys.stdin", io.StringIO("")):
            reply, usage = chat_turn(
                config=config,
                provider="custom",
                raw_fn=raw_custom,
                messages=[{"role": "user", "content": "hi"}],
                tools=None,
                tool_map={},
                builtin_clients={},
                max_tool_rounds=2,
            )
        self.assertIn("rescued by the registry box", reply)
        self.assertEqual(config["provider"], "custom")
        self.assertEqual(config["custom_base_url"], rescue_box.base_url + "/v1")
        self.assertEqual(config["chat_model"], "rescue-32b")


if __name__ == "__main__":
    unittest.main()
