import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TestDockerfileContract(unittest.TestCase):
    def test_runtime_is_non_root_and_installs_a_wheel(self):
        text = (ROOT / "Dockerfile").read_text()
        self.assertIn("pip wheel", text)
        self.assertIn("USER conch", text)
        self.assertNotIn("USER root\n\nENTRYPOINT", text)

    def test_minimal_and_dev_targets_exist(self):
        text = (ROOT / "Dockerfile").read_text()
        self.assertIn("FROM runtime AS minimal", text)
        self.assertIn("FROM dev-base AS dev", text)


class TestEntrypointContract(unittest.TestCase):
    def test_never_deletes_or_rewrites_persisted_data(self):
        text = (ROOT / "docker-entrypoint.sh").read_text()
        self.assertNotIn("rm -", text)
        self.assertNotIn("sed -", text)
        self.assertNotIn("search.db", text)

    def test_chat_ask_health_and_shell_dispatch_are_explicit(self):
        text = (ROOT / "docker-entrypoint.sh").read_text()
        for command in ("chat)", "ask)", "health)", "shell)"):
            self.assertIn(command, text)
        self.assertIn("exec conch-ask", text)


class TestComposeContract(unittest.TestCase):
    def test_bridge_networking_and_narrow_workspace_are_defaults(self):
        text = (ROOT / "docker-compose.yml").read_text()
        self.assertNotIn("network_mode: host", text)
        self.assertIn("${CONCH_WORKSPACE:-.}:/workspace", text)
        self.assertNotIn("${HOME}:/workspace", text)

    def test_state_config_and_security_controls_are_declared(self):
        text = (ROOT / "docker-compose.yml").read_text()
        self.assertIn("conch-state:", text)
        self.assertIn("conch-config:", text)
        self.assertIn("read_only: true", text)
        self.assertIn("no-new-privileges:true", text)
        self.assertIn("cap_drop:", text)

    def test_local_inference_topologies_are_present(self):
        text = (ROOT / "docker-compose.yml").read_text()
        self.assertIn("host.docker.internal:host-gateway", text)
        self.assertIn("ollama-sidecar", text)
        self.assertIn("llamacpp-sidecar", text)
        self.assertIn("CONCH_LOCAL_ONLY", text)


if __name__ == "__main__":
    unittest.main()
