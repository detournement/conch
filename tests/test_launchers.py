"""Offline launcher repair tests: all writable state belongs to a temporary directory."""
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys

import contextlib
import io
import tempfile
import unittest
from unittest.mock import patch

from conch.launchers import main, repair_launcher


def executable(path, label="selected"):
    path.parent.mkdir(parents=True, exist_ok=True)
    # The static signature is intentionally unreachable: no installed package,
    # credential or network is needed to exercise a real fresh shell.
    path.write_text(f"#!{sys.executable}\nimport sys\nprint({label!r})\nsys.exit(0)\nfrom conch.app import main\n")
    path.chmod(0o755)
    return path


def checkout(home):
    root = home / "old conch"
    executable(root / "bin/conch", "old")
    (root / "conch").mkdir()
    (root / "conch/__init__.py").write_text('__version__ = "old"\n')
    (root / "pyproject.toml").write_text('[project]\nname="conch-shell"\n')
    (root / "shell").mkdir()
    for shell in ("zsh", "bash"):
        (root / f"shell/conch.{shell}").write_text('conch-setup() { :; }\nask() { printf "helper-preserved"; }\n')
    (root / ".env").write_bytes(b'export SECRET_INTEGRATION="sensitive-value"\r\n# keep byte-for-byte\r\n')
    (root / ".venv/bin").mkdir(parents=True)
    executable(root / ".venv/bin/conch", "old-venv")
    executable(root / ".venv/bin/python", "wrong-python")
    return root


def environment(home, **extra):
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "SHELL": "/bin/zsh", "TERM": "dumb"}
    env.update(extra)
    return env


class LauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp_path = Path(temporary.name).resolve()

    def _case_fresh_shell_after_repeated_upgrade_preserves_source_helpers_and_secrets(self, shell, login):
        tmp_path = self.tmp_path
        binary = shutil.which(shell)
        if not binary:
            self.skipTest(f'{shell} unavailable')
        home = tmp_path / "home ' quoted"
        home.mkdir()
        old = checkout(home)
        selected = executable(home / ".local/bin/conch")
        rc = home / (".zshrc" if shell == "zsh" else ".bashrc")
        original = (f'export CONCH_DIR={shlex.quote(str(old))}\n'
                    'export PATH="${CONCH_DIR}/bin:${CONCH_DIR}/.venv/bin:$PATH"\n'
                    '[[ -f "${CONCH_DIR}/.env" ]] && source "${CONCH_DIR}/.env"\n'
                    f'source "${{CONCH_DIR}}/shell/conch.{shell}"\nconch-setup\n'
                    'export CUSTOM_SENSITIVE="do-not-print-this"\n'
                    f'alias conch={shlex.quote(shlex.quote(str(old / "bin/conch")))}\n')
        rc.write_text(original)
        if shell == "bash":
            (home / ".bash_profile").write_text('source "$HOME/.bashrc"\n')
        env_bytes = (old / ".env").read_bytes()
        env = environment(home, PATH=f"{old / 'bin'}:{old / '.venv/bin'}:/usr/bin:/bin", CONCH_DIR=str(old), SHELL=binary)
        first = repair_launcher(selected, environ=env)
        self.assertTrue(first.usable, first.conflicts)
        self.assertTrue(first.backups)
        self.assertTrue(all((stat.S_IMODE(p.stat().st_mode) == 384 for p in first.backups)))
        self.assertTrue(any((p.read_text() == original for p in first.backups)))
        second = repair_launcher(selected, environ=env)
        self.assertTrue(second.usable, second.conflicts)
        self.assertFalse(second.changed)
        self.assertFalse(second.backups)
        self.assertEqual((old / '.env').read_bytes(), env_bytes)
        self.assertIn('source "${CONCH_DIR}/.env"', rc.read_text())
        self.assertIn(f'shell/conch.{shell}', rc.read_text())
        self.assertIn('CUSTOM_SENSITIVE', rc.read_text())
        self.assertNotIn('.venv/bin', rc.read_text())
        command = 'command -v conch; conch; printf "\\n%s\\n" "$CUSTOM_SENSITIVE"; ask; command -v python'
        args = [binary, "-l", "-i", "-c", command] if login else [binary, "-i", "-c", command]
        result = subprocess.run(args, env=environment(home, SHELL=binary), capture_output=True, text=True, timeout=15)
        self.assertIn(result.returncode, (0, 1), result.stderr)  # system python need not exist
        self.assertIn(str(selected), result.stdout)
        self.assertIn('selected\n', result.stdout)
        self.assertIn('do-not-print-this', result.stdout)
        self.assertIn('helper-preserved', result.stdout)
        self.assertNotIn('wrong-python', result.stdout)
        self.assertNotIn(str(old / '.venv/bin/python'), result.stdout)

    def test_fresh_shell_after_repeated_upgrade_preserves_source_helpers_and_secrets_bash_False(self):
        self._case_fresh_shell_after_repeated_upgrade_preserves_source_helpers_and_secrets(shell='bash', login=False)

    def test_fresh_shell_after_repeated_upgrade_preserves_source_helpers_and_secrets_bash_True(self):
        self._case_fresh_shell_after_repeated_upgrade_preserves_source_helpers_and_secrets(shell='bash', login=True)

    def test_fresh_shell_after_repeated_upgrade_preserves_source_helpers_and_secrets_zsh_False(self):
        self._case_fresh_shell_after_repeated_upgrade_preserves_source_helpers_and_secrets(shell='zsh', login=False)

    def test_fresh_shell_after_repeated_upgrade_preserves_source_helpers_and_secrets_zsh_True(self):
        self._case_fresh_shell_after_repeated_upgrade_preserves_source_helpers_and_secrets(shell='zsh', login=True)

    def test_check_is_read_only_including_missing_profiles_and_backups(self):
        tmp_path = self.tmp_path
        selected = executable(tmp_path / ".local/bin/conch")
        rc = tmp_path / ".zshrc"
        rc.write_text('export UNRELATED="secret"\n')
        before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        report = repair_launcher(selected, check=True, environ=environment(tmp_path))
        after = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertTrue(report.pending)
        self.assertFalse(report.usable)
        self.assertFalse(report.changed)
        self.assertFalse(report.backups)

    def test_zdotdir_sources_login_profiles_and_common_env_are_inspected(self):
        tmp_path = self.tmp_path
        selected = executable(tmp_path / ".local/bin/conch")
        old = checkout(tmp_path)
        zdot = tmp_path / ".config/zsh"
        zdot.mkdir(parents=True)
        (tmp_path / ".zshenv").write_text('export ZDOTDIR="$HOME/.config/zsh"\n')
        (zdot / ".zprofile").write_text('source "$HOME/.config/shell/init"\n')
        include = tmp_path / ".config/shell/init"
        include.parent.mkdir()
        include.write_text(f'alias conch={shlex.quote(shlex.quote(str(old / "bin/conch")))}\nexport CUSTOM=kept\n')
        (tmp_path / ".bash_login").write_text('export LOGIN_CUSTOM=kept\n')
        report = repair_launcher(selected, environ=environment(tmp_path))
        self.assertTrue(report.usable, report.conflicts)
        self.assertIn(include, report.scanned)
        self.assertIn('export CUSTOM=kept\n', include.read_text())
        self.assertTrue((zdot / '.zshrc').exists())
        self.assertFalse((tmp_path / '.zshrc').exists())
        self.assertIn('conch launcher (managed)', (tmp_path / '.bash_login').read_text())
        self.assertFalse((tmp_path / '.bash_profile').exists())

    def test_public_symlink_is_not_resolved_into_private_venv_path(self):
        tmp_path = self.tmp_path
        target = executable(tmp_path / "private/bin/conch")
        (tmp_path / "private/pyvenv.cfg").write_text("home=/usr/bin\n")
        public = tmp_path / ".local/bin/conch"
        public.parent.mkdir(parents=True)
        public.symlink_to(target)
        report = repair_launcher(public, environ=environment(tmp_path))
        self.assertTrue(report.usable)
        rc = (tmp_path / ".zshrc").read_text()
        self.assertIn(str(public.parent), rc)
        self.assertNotIn(str(target.parent), rc)
        self.assertTrue(public.is_symlink())

    def test_direct_venv_launcher_exposes_only_conch_tools(self):
        tmp_path = self.tmp_path
        selected = executable(tmp_path / "private/bin/conch")
        helper = executable(tmp_path / "private/bin/conch-ask")
        executable(tmp_path / "private/bin/python", "do-not-expose")
        (tmp_path / "private/pyvenv.cfg").write_text("home=/usr/bin\n")
        env = environment(tmp_path)
        check = repair_launcher(selected, check=True, environ=env)
        self.assertTrue(check.pending)
        self.assertFalse((tmp_path / '.local/share/conch/launchers').exists())
        report = repair_launcher(selected, environ=env)
        self.assertTrue(report.usable, report.conflicts)
        dedicated = tmp_path / ".local/share/conch/launchers"
        self.assertEqual((dedicated / 'conch').resolve(), selected)
        self.assertEqual((dedicated / 'conch-ask').resolve(), helper)
        self.assertFalse((dedicated / 'python').exists())
        self.assertNotIn(str(selected.parent), (tmp_path / '.bashrc').read_text())

    def test_ambiguous_overrides_and_active_environments_are_not_false_fixed(self):
        tmp_path = self.tmp_path
        selected = executable(tmp_path / ".local/bin/conch")
        other = executable(tmp_path / "another/bin/conch", "other")
        rc = tmp_path / ".zshrc"
        rc.write_text('alias conch="printf super-secret-value"\nconch() { printf other-secret; }\n')
        captured = io.StringIO()
        with patch.dict(os.environ, environment(tmp_path, VIRTUAL_ENV=str(other.parent.parent)), clear=True), contextlib.redirect_stdout(captured):
            self.assertEqual(main(["--launcher", str(selected)]), 1)
        output = captured.getvalue()
        self.assertIn('Unresolved:', output)
        self.assertIn('VIRTUAL_ENV', output)
        self.assertNotIn('super-secret-value', output)
        self.assertNotIn('other-secret', output)
        self.assertIn('super-secret-value', rc.read_text())

    def test_profiles_never_execute_during_inspection_and_protected_env_is_unchanged(self):
        tmp_path = self.tmp_path
        selected = executable(tmp_path / ".local/bin/conch")
        old = checkout(tmp_path)
        marker = tmp_path / "executed"
        protected = tmp_path / ".config/custom.env"
        protected.parent.mkdir(parents=True)
        original = f'export SECRET="keep"\nalias conch={shlex.quote(shlex.quote(str(old / "bin/conch")))}\n'
        protected.write_text(original)
        (tmp_path / ".zshrc").write_text(f'touch {shlex.quote(str(marker))}\nsource "$HOME/.config/custom.env"\n')
        report = repair_launcher(selected, environ=environment(tmp_path))
        self.assertFalse(report.usable)
        self.assertTrue(any(('protected env file' in c for c in report.conflicts)))
        self.assertEqual(protected.read_text(), original)
        self.assertFalse(marker.exists())

    def test_single_line_command_substitution_does_not_block_launcher_repair(self):
        home = self.tmp_path
        selected = executable(home / ".local/bin/conch")
        old = executable(home / "old/bin/conch", "old")
        marker = home / "profile-executed"
        rc = home / ".bashrc"
        initialization = (f'eval "$(touch {shlex.quote(str(marker))}; '
                          "printf '%s' 'export CUSTOM=kept')\"\n"
                          f'eval "`touch {shlex.quote(str(marker))}; '
                          "printf '%s' 'export LEGACY=kept'`\"\n")
        rc.write_text(initialization + f"alias conch={shlex.quote(str(old))}\n")
        report = repair_launcher(selected, environ=environment(home))
        self.assertTrue(report.usable, report.conflicts)
        self.assertIn(rc, report.changed)
        self.assertFalse(marker.exists())
        self.assertTrue(repair_launcher(selected, check=True, environ=environment(home)).usable)

    def test_literal_startup_data_is_not_scanned_or_rewritten(self):
        containers = (
            ("quoted-heredoc", "cat <<'SCRIPT'\n", "SCRIPT\n"),
            ("unquoted-heredoc", "cat <<SCRIPT\n", "SCRIPT\n"),
            ("indented-heredoc", "cat <<-SCRIPT\n", "\tSCRIPT\n"),
            ("single-quoted", "generated='\n", "'\n"),
            ("ansi-quoted", "generated=$'escaped\\'\n", "'\n"),
            ("double-quoted", 'generated="\n', '"\n'),
            ("nested-substitution", "generated=\"$(printf '%s' \"\n", "\")\"\n"),
            ("nested-backtick", "generated=\"`printf '%s' \"\n", "\"`\"\n"),
        )
        for name, opening, closing in containers:
            for profile in (".zshenv", ".bashrc"):
                with self.subTest(container=name, profile=profile):
                    home = self.tmp_path / f"{name}-{profile}"
                    home.mkdir()
                    selected = executable(home / ".local/bin/conch")
                    old = executable(home / "old/bin/conch", "old")
                    fake_source = home / "literal-source"
                    alias = f"alias conch={old}\n"
                    fake_source.write_text(alias)
                    redirected = home / "literal-zdotdir"
                    redirected.mkdir()
                    redirected_rc = redirected / ".zshrc"
                    redirected_rc.write_text(alias)
                    marker = home / "profile-executed"
                    payload = (f"export ZDOTDIR={redirected}\n"
                               f"source {fake_source}\n"
                               + alias
                               + "# >>> conch launcher (managed) >>>\n"
                               + f'export PATH={old.parent}:"$PATH"\n'
                               + "# <<< conch launcher (managed) <<<\n"
                               + "# added by the conch installer\n"
                               + f'export PATH="{old.parent}:$PATH"\n'
                               + "literal-sensitive-value\n")
                    rc = home / profile
                    original = (f"touch {marker}\n" + opening + payload + closing).replace("\n", "\r\n").encode()
                    rc.write_bytes(original)
                    for check in (True, False, False):
                        report = repair_launcher(selected, check=check, environ=environment(home))
                        self.assertFalse(report.usable)
                        self.assertTrue(any(str(rc) in conflict for conflict in report.conflicts))
                        self.assertNotIn("literal-sensitive-value", "\n".join(report.conflicts))
                        self.assertEqual(rc.read_bytes(), original)
                        self.assertNotIn(rc, report.changed)
                        self.assertNotIn(rc, report.pending)
                        self.assertNotIn(fake_source, report.scanned)
                        self.assertNotIn(redirected_rc, report.scanned)
                        self.assertEqual(fake_source.read_text(), alias)
                        self.assertEqual(redirected_rc.read_text(), alias)
                        self.assertFalse(marker.exists())

    def test_outside_home_symlinked_profile_and_foreign_launcher_are_preserved(self):
        tmp_path = self.tmp_path
        home = tmp_path / "home"
        home.mkdir()
        selected = executable(home / ".local/bin/conch")
        outside = tmp_path / "external-profile"
        outside.write_text("export CUSTOM=keep\n")
        (home / ".zshrc").symlink_to(outside)
        report = repair_launcher(selected, environ=environment(home))
        self.assertFalse(report.usable)
        self.assertTrue(any(('outside your home' in c for c in report.conflicts)))
        self.assertEqual(outside.read_text(), 'export CUSTOM=keep\n')
        foreign = home / "third-party/bin/conch"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("#!/bin/sh\nprintf third-party\n")
        foreign.chmod(0o755)
        denied = repair_launcher(foreign, environ=environment(home))
        self.assertFalse(denied.usable)
        self.assertFalse(denied.changed)
        self.assertEqual(foreign.read_text(), '#!/bin/sh\nprintf third-party\n')

    def test_legacy_public_marker_migrates_once_without_dropping_custom_settings(self):
        tmp_path = self.tmp_path
        selected = executable(tmp_path / ".local/bin/conch")
        rc = tmp_path / ".zshrc"
        rc.write_text('# added by the conch installer\nexport PATH="/old/conch/bin:$PATH"\nexport KEEP=unchanged\n')
        first = repair_launcher(selected, environ=environment(tmp_path))
        self.assertTrue(first.usable)
        self.assertNotIn('# added by the conch installer', rc.read_text())
        self.assertIn('export KEEP=unchanged', rc.read_text())
        second = repair_launcher(selected, environ=environment(tmp_path))
        self.assertTrue(second.usable)
        self.assertFalse(second.changed)
        self.assertEqual(rc.read_text().count('# >>> conch launcher (managed) >>>'), 1)

    def test_customized_legacy_path_keeps_unrelated_command(self):
        shells = [(name, shutil.which(name)) for name in ("bash", "zsh")]
        shells = [(name, binary) for name, binary in shells if binary]
        if not shells:
            self.skipTest("bash and zsh unavailable")
        for shell, binary in shells:
            with self.subTest(shell=shell):
                home = self.tmp_path / shell
                home.mkdir()
                selected = executable(home / ".local/bin/conch")
                old = executable(home / "old/bin/conch", "old")
                tool = executable(home / "tools/bin/unrelated-tool", "tool-preserved")
                rc = home / (".bashrc" if shell == "bash" else ".zshrc")
                customized = f'export PATH="{old.parent}:{tool.parent}:$PATH"\n'
                rc.write_text('# added by the conch installer\n' + customized
                              + 'export CUSTOM_SECRET=preserved\n')
                env = environment(home, SHELL=binary)
                first = repair_launcher(selected, environ=env)
                self.assertTrue(first.usable, first.conflicts)
                self.assertIn(customized, rc.read_text())
                second = repair_launcher(selected, environ=env)
                self.assertTrue(second.usable, second.conflicts)
                self.assertFalse(second.changed)
                self.assertFalse(second.backups)
                parsed = subprocess.run([binary, "-n", str(rc)], env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(parsed.returncode, 0, parsed.stderr)
                command = (f'source {shlex.quote(str(rc))}\n'
                           'command -v conch; conch; unrelated-tool; '
                           'printf "%s\\n" "$CUSTOM_SECRET"')
                result = subprocess.run([binary, "-c", command], env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(str(selected), result.stdout)
                self.assertIn("selected\n", result.stdout)
                self.assertIn("tool-preserved\n", result.stdout)
                self.assertIn("preserved\n", result.stdout)

    def test_removed_launchers_keep_conditional_and_function_bodies_valid(self):
        shells = [(name, shutil.which(name)) for name in ("bash", "zsh", "fish")]
        shells = [(name, binary) for name, binary in shells if binary]
        if not shells:
            self.skipTest("bash, zsh and fish unavailable")
        for shell, binary in shells:
            for kind in ("alias", "managed", "legacy"):
                if shell == "fish" and kind == "legacy":
                    continue  # The legacy public installer generated sh syntax.
                for body in ("conditional", "function"):
                    with self.subTest(shell=shell, kind=kind, body=body):
                        home = self.tmp_path / f"{shell}-{kind}-{body}"
                        home.mkdir()
                        selected = executable(home / ".local/bin/conch")
                        old = executable(home / "old/bin/conch", "old")
                        fish = shell == "fish"
                        rc = home / (".config/fish/config.fish" if fish else
                                     ".bashrc" if shell == "bash" else ".zshrc")
                        rc.parent.mkdir(parents=True, exist_ok=True)
                        if kind == "alias":
                            separator = " " if fish else "="
                            statement = f"alias conch{separator}{shlex.quote(str(old))}\n"
                        elif kind == "managed":
                            assignment = (f"set -gx PATH '{old.parent}' $PATH\n" if fish else
                                          f'export PATH={shlex.quote(str(old.parent))}:"$PATH"\n')
                            statement = ("# >>> conch launcher (managed) >>>\n"
                                         + assignment + "# <<< conch launcher (managed) <<<\n")
                        else:
                            statement = ('# added by the conch installer\n'
                                         f'export PATH="{old.parent}:$PATH"\n')
                        if body == "conditional":
                            wrapped = ("if true\n" + statement + "end\n" if fish else
                                       "if true; then\n" + statement + "fi\n")
                        else:
                            wrapped = ("function configure_launcher\n" + statement + "end\n" if fish else
                                       "configure_launcher() {\n" + statement + "}\n")
                            wrapped += "configure_launcher\n"
                        marker = home / "profile-executed"
                        rc.write_text(f"printf executed > {shlex.quote(str(marker))}\n" + wrapped)
                        env = environment(home, SHELL=binary)
                        first = repair_launcher(selected, environ=env)
                        self.assertTrue(first.usable, first.conflicts)
                        self.assertFalse(marker.exists())
                        second = repair_launcher(selected, environ=env)
                        self.assertTrue(second.usable, second.conflicts)
                        self.assertFalse(second.changed)
                        self.assertFalse(second.backups)
                        self.assertFalse(marker.exists())
                        parsed = subprocess.run([binary, "-n", str(rc)], env=env,
                                                capture_output=True, text=True, timeout=10)
                        self.assertEqual(parsed.returncode, 0, parsed.stderr)
                        self.assertFalse(marker.exists())
                        flags = (["--no-config"] if fish else
                                 ["--noprofile", "--norc", "-O", "expand_aliases"] if shell == "bash" else ["-f"])
                        lookup = "command -s conch" if fish else "command -v conch"
                        command = f"source {shlex.quote(str(rc))}\n{lookup}; conch"
                        result = subprocess.run([binary, *flags, "-c", command], env=env,
                                                capture_output=True, text=True, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertTrue(marker.exists())
                        self.assertIn(str(selected), result.stdout)
                        self.assertIn("selected\n", result.stdout)

    def test_fish_xdg_config_and_conf_d_overrides_are_inspected(self):
        tmp_path = self.tmp_path
        selected = executable(tmp_path / ".local/bin/conch")
        old = executable(tmp_path / "old/bin/conch", "old")
        config = tmp_path / "xdg/fish"
        (config / "conf.d").mkdir(parents=True)
        snippet = config / "conf.d/legacy.fish"
        snippet.write_text(f"alias conch {shlex.quote(shlex.quote(str(old)))}\nset -gx CUSTOM keep\n")
        env = environment(tmp_path, XDG_CONFIG_HOME=str(config.parent), SHELL="/usr/bin/fish")
        report = repair_launcher(selected, environ=env)
        self.assertTrue(report.usable, report.conflicts)
        self.assertIn('set -gx CUSTOM keep\n', snippet.read_text())
        self.assertIn('set -gx PATH', (config / 'config.fish').read_text())
        self.assertFalse(repair_launcher(selected, environ=env).changed)

    def test_custom_managed_block_is_not_deleted(self):
        tmp_path = self.tmp_path
        selected = executable(tmp_path / ".local/bin/conch")
        rc = tmp_path / ".zshrc"
        rc.write_text('# >>> conch launcher (managed) >>>\nexport MY_SECRET=kept\n# <<< conch launcher (managed) <<<\n')
        report = repair_launcher(selected, environ=environment(tmp_path))
        self.assertFalse(report.usable)
        self.assertIn('export MY_SECRET=kept', rc.read_text())
        self.assertTrue(any(('customized' in c for c in report.conflicts)))

    def test_similar_helper_names_do_not_hide_real_launcher_overrides(self):
        for shell in ("bash", "zsh", "fish"):
            with self.subTest(shell=shell):
                home = self.tmp_path / shell
                home.mkdir()
                selected = executable(home / ".local/bin/conch")
                rc = home / (".config/fish/config.fish" if shell == "fish" else f".{shell}rc")
                rc.parent.mkdir(parents=True, exist_ok=True)
                original = ("function conch-helper\n    true\nend\nalias old-conch 'echo preserved'\n"
                            if shell == "fish" else
                            "function conch-helper() { :; }\nalias old-conch='printf preserved'\n")
                rc.write_text(original)
                env = environment(home, SHELL=f"/bin/{shell}")
                report = repair_launcher(selected, environ=env)
                self.assertTrue(report.usable, report.conflicts)
                self.assertIn(original, rc.read_text())
                override = ("function conch\n    true\nend\n" if shell == "fish"
                            else "function conch() { :; }\n")
                rc.write_text(override)
                self.assertFalse(repair_launcher(selected, environ=env).usable)
                self.assertIn(override, rc.read_text())
                if shell != "fish":
                    compound = "alias keep=true conch='printf shadow'\n"
                    rc.write_text(compound)
                    self.assertFalse(repair_launcher(selected, environ=env).usable)
                    self.assertIn(compound, rc.read_text())

    def test_uninspectable_helpers_are_preserved(self):
        for kind in ("unreadable", "non_utf8", "oversized", "dangling_symlink"):
            with self.subTest(kind=kind):
                home = self.tmp_path / kind
                home.mkdir()
                selected = executable(home / ".local/bin/conch")
                executable(home / ".local/bin/conch-ask", "selected-ask")
                old = checkout(home)
                (old / "shell/conch.zsh").write_text("# CONCH_ASK_CMD\nconch-setup() { :; }\n")
                (home / ".zshrc").write_text(
                    f'export CONCH_DIR={shlex.quote(str(old))}\n'
                    'source "${CONCH_DIR}/shell/conch.zsh"\nconch-setup\n')
                helper = home / ".local/share/conch/shell-helpers/conch-ask"
                helper.parent.mkdir(parents=True)
                missing = home / "missing-helper"
                content = {"unreadable": b"keep this helper", "non_utf8": b"\xff",
                           "oversized": b"x" * 262145}.get(kind)
                if kind == "dangling_symlink":
                    helper.symlink_to(missing)
                else:
                    helper.write_bytes(content)
                    helper.chmod(0o640)
                original_open = Path.open

                def denied(path, *args, **kwargs):
                    if path == helper:
                        raise PermissionError("inspection denied")
                    return original_open(path, *args, **kwargs)

                inspection = (patch.object(Path, "open", denied) if kind == "unreadable"
                              else contextlib.nullcontext())
                with inspection:
                    report = repair_launcher(selected, environ=environment(home))
                self.assertFalse(report.usable)
                self.assertTrue(any(str(helper) in conflict for conflict in report.conflicts))
                self.assertNotIn(helper, report.changed)
                if kind == "dangling_symlink":
                    self.assertTrue(helper.is_symlink())
                    self.assertEqual(helper.readlink(), missing)
                    self.assertFalse(missing.exists())
                else:
                    self.assertEqual(helper.read_bytes(), content)
                    self.assertEqual(stat.S_IMODE(helper.stat().st_mode), 0o640)

    def test_legacy_widgets_forward_helpers_without_promoting_private_python(self):
        tmp_path = self.tmp_path
        selected = executable(tmp_path / ".local/bin/conch")
        executable(tmp_path / ".local/bin/conch-ask", "selected-ask")
        old = checkout(tmp_path)
        (old / "shell/conch.zsh").write_text('# CONCH_ASK_CMD\nconch-setup() { :; }\n')
        (tmp_path / ".zshrc").write_text(
            f'export CONCH_DIR={shlex.quote(str(old))}\nsource "${{CONCH_DIR}}/shell/conch.zsh"\nconch-setup\n')
        env = environment(tmp_path)
        report = repair_launcher(selected, environ=env)
        self.assertTrue(report.usable, report.conflicts)
        helpers = tmp_path / ".local/share/conch/shell-helpers"
        self.assertIn(f"export CONCH_ASK_CMD={shlex.quote(str(helpers / 'conch-ask'))}", (tmp_path / '.zshrc').read_text())
        for name, args, expected in (
            ("conch-ask", ["request"], "selected-ask"),
            ("conch-chat", ["request"], "selected"),
            ("conch-run-with-timeout", ["5", "request"], "selected-ask"),
        ):
            result = subprocess.run([sys.executable, str(helpers / name), *args], env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), expected)
        self.assertFalse(repair_launcher(selected, environ=env).changed)
        self.assertNotIn(str(old / '.venv/bin'), (tmp_path / '.zshrc').read_text())

    def test_inherited_zdotdir_does_not_read_unused_home_zshenv(self):
        tmp_path = self.tmp_path
        selected = executable(tmp_path / ".local/bin/conch")
        (tmp_path / ".zshenv").write_text('export ZDOTDIR="$(do-not-execute)"\n')
        zdot = tmp_path / "shell/zsh"
        zdot.mkdir(parents=True)
        (zdot / ".zshenv").write_text("export CUSTOM=kept\n")
        report = repair_launcher(selected, environ=environment(tmp_path, ZDOTDIR=str(zdot)))
        self.assertTrue(report.usable, report.conflicts)
        self.assertNotIn(tmp_path / '.zshenv', report.scanned)
        self.assertTrue((zdot / '.zshrc').exists())

    def test_fish_autoload_function_is_reported_without_overwrite(self):
        tmp_path = self.tmp_path
        selected = executable(tmp_path / ".local/bin/conch")
        function = tmp_path / ".config/fish/functions/conch.fish"
        function.parent.mkdir(parents=True)
        original = "function conch\n    printf secret-value\nend\n"
        function.write_text(original)
        report = repair_launcher(selected, environ=environment(tmp_path, SHELL="/usr/bin/fish"))
        self.assertFalse(report.usable)
        self.assertEqual(function.read_text(), original)
        self.assertTrue(any(('fish conch function' in c for c in report.conflicts)))

    def test_startup_virtualenv_activation_is_reported_and_not_modified(self):
        tmp_path = self.tmp_path
        selected = executable(tmp_path / ".local/bin/conch")
        old = checkout(tmp_path)
        activation = old / ".venv/bin/activate"
        original = f'VIRTUAL_ENV={shlex.quote(str(old / ".venv"))}\nexport PATH="$VIRTUAL_ENV/bin:$PATH"\n'
        activation.write_text(original)
        (tmp_path / ".zshrc").write_text(f"source {shlex.quote(str(activation))}\n")
        report = repair_launcher(selected, environ=environment(tmp_path))
        self.assertFalse(report.usable)
        self.assertTrue(any(('startup activates another' in c for c in report.conflicts)))
        self.assertEqual(activation.read_text(), original)

    def test_selected_checkout_own_active_venv_is_not_a_conflict(self):
        tmp_path = self.tmp_path
        root = checkout(tmp_path)
        report = repair_launcher(root / "bin/conch",
                                 environ=environment(tmp_path, VIRTUAL_ENV=str(root / ".venv")))
        self.assertTrue(report.usable, report.conflicts)

    def test_literal_bash_env_source_is_followed_without_running_it(self):
        tmp_path = self.tmp_path
        selected = executable(tmp_path / ".local/bin/conch")
        source = tmp_path / "bash startup"
        source.write_text("export CUSTOM=kept\n")
        report = repair_launcher(selected, environ=environment(tmp_path, BASH_ENV="$HOME/bash startup"))
        self.assertTrue(report.usable, report.conflicts)
        self.assertIn(source, report.scanned)
        self.assertEqual(source.read_text(), 'export CUSTOM=kept\n')

    def _case_disabled_or_terminated_startup_is_not_reported_fixed(self, startup):
        tmp_path = self.tmp_path
        selected = executable(tmp_path / ".local/bin/conch")
        (tmp_path / ".zshrc").write_text(startup)
        report = repair_launcher(selected, environ=environment(tmp_path))
        self.assertFalse(report.usable)
        self.assertTrue(report.conflicts)

    def test_disabled_or_terminated_startup_is_not_reported_fixed_return(self):
        self._case_disabled_or_terminated_startup_is_not_reported_fixed(startup='return\n')

    def test_disabled_or_terminated_startup_is_not_reported_fixed_unsetopt_rcs(self):
        self._case_disabled_or_terminated_startup_is_not_reported_fixed(startup='unsetopt rcs\n')

    def test_fish_managed_quoting_is_idempotent_with_apostrophe_paths(self):
        tmp_path = self.tmp_path
        home = tmp_path / "quoted ' home"
        home.mkdir()
        selected = executable(home / ".local/bin/conch")
        env = environment(home, SHELL="/usr/bin/fish")
        first = repair_launcher(selected, environ=env)
        self.assertTrue(first.usable, first.conflicts)
        second = repair_launcher(selected, environ=env)
        self.assertTrue(second.usable, second.conflicts)
        self.assertFalse(second.changed)
        fish = shutil.which("fish")
        if fish:
            result = subprocess.run([fish, "-c", "command -s conch; conch"], env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(selected), result.stdout)
            self.assertIn('selected\n', result.stdout)


if __name__ == "__main__":
    unittest.main()
