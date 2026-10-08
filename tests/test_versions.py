"""The version manager: installs are checked, failures leave nothing behind, switching is atomic.

Environment creation (venv + pip) is replaced by a fake reader, so these tests
need no network and use only the standard library.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.server
import importlib.util
import io
import json
import os
import shlex
import stat
import sys
import tempfile
import threading
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("canvas_reader_versions", ROOT / "scripts" / "versions.py")
versions = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(versions)
START_SH = (ROOT / "scripts" / "start.sh").read_bytes()


class FakeGitHub:
    """A local stand-in for the GitHub releases API and its downloads."""

    def __init__(self):
        self.routes = {}
        self.requests = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server's naming
                fake.requests.append(self.path)
                body = fake.routes.get(self.path)
                self.send_response(404 if body is None else 200)
                self.end_headers()
                if body is not None:
                    self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def publish(self, archive, *, checksum=None, sums=True, scheme_base=None):
        name = archive.name
        data = archive.read_bytes()
        base = scheme_base or self.base
        assets = [{"name": name, "browser_download_url": f"{base}/download/{name}"}]
        self.routes[f"/download/{name}"] = data
        if sums:
            digest = checksum or hashlib.sha256(data).hexdigest()
            self.routes["/download/SHA256SUMS"] = f"{digest}  {name}\n{'0' * 64}  other.zip\n".encode()
            assets.append({"name": "SHA256SUMS", "browser_download_url": f"{base}/download/SHA256SUMS"})
        release = {"tag_name": "v" + name.split("-")[-1][:-4], "assets": assets}
        self.routes["/repos/example/canvas-reader/releases/latest"] = json.dumps(release).encode()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class VersionManagerTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.tmp = Path(scratch.name)
        self.user_home = self.tmp / "home"
        self.user_home.mkdir()
        self.checkout = self.tmp / "checkout"
        self.dist = self.checkout / "dist"
        self.dist.mkdir(parents=True)
        self.log = self.tmp / "reader.log"
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.user_home),
            "TUNNEL_CLIENT_PROFILE_DIR": str(self.user_home / ".config" / "tunnel-client"),
            "FAKE_READER_LOG": str(self.log),
            "CANVAS_API_TOKEN": "real-canvas-secret",
            "CONTROL_PLANE_API_KEY": "openai-secret",
            # Never let a test reach the real Keychain.
            "CANVAS_READER_SECURITY_BIN": str(self.tmp / "no-keychain-in-tests"),
        }
        for patcher in (patch.dict(os.environ, environment, clear=True),
                        patch.object(versions, "SCRIPT_ROOT", self.checkout),
                        patch.object(versions, "list_processes", lambda: self.processes)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.processes = []
        self.home = versions.Home(versions.home_dir())
        self.environment("ok")

    # ---- helpers ------------------------------------------------------------

    def environment(self, outcome):
        """Replace venv + pip with a fake reader whose --check reports ``outcome``."""
        def create(folder, python, say=print):
            if outcome == "interrupt":
                raise KeyboardInterrupt
            if outcome == "pip_fails":
                raise versions.StepFailed(
                    "installing dependencies", "exit status 1",
                    "Collecting canvas-mcp==1.13.0\nERROR: No matching distribution found via "
                    "https://user:hunter2@pypi.example/simple")
            bin_dir = folder / ".venv" / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "python").symlink_to(sys.executable)
            report = {"ok": {"status": "ok", "version": folder.name, "tools": ["a", "b"]},
                      "wrong_version": {"status": "ok", "version": "9.9.9", "tools": ["a"]}}.get(outcome)
            body = f"echo '{json.dumps(report)}'\n" if report else "echo 'Traceback: boom' >&2\nexit 1\n"
            reader = bin_dir / "canvas-reader"
            reader.write_text('#!/bin/sh\nprintf "%s %s\\n" "$CANVAS_API_TOKEN" "$HOME" >> "$FAKE_READER_LOG"\n'
                              + body)
            reader.chmod(0o755)
            (folder / "build").mkdir()
            (folder / "src" / "canvas_reader.egg-info").mkdir()

        patcher = patch.object(versions, "create_environment", create)
        patcher.start()
        self.addCleanup(patcher.stop)

    def bundle(self, version, *, init=None, wrap=False, extra=None, name=None):
        files = {
            "plugin.json": json.dumps({"name": "canvas-reader", "version": version}),
            "pyproject.toml": '[project]\nname = "canvas-reader"\nrequires-python = ">=3.9"\n',
            "constraints.txt": "",
            "src/canvas_reader/__init__.py": f'__version__ = "{init or version}"\n',
            "src/canvas_reader/server.py": "",
            "scripts/start.sh": START_SH,
            "scripts/versions.py": "",
        }
        files.update(extra or {})
        path = self.dist / (name or f"canvas-reader-{version}.zip")
        with zipfile.ZipFile(path, "w") as archive:
            for relative, data in files.items():
                info = zipfile.ZipInfo((f"canvas-reader-{version}/" if wrap else "") + relative)
                info.external_attr = (stat.S_IFREG | (0o755 if relative.endswith(".sh") else 0o644)) << 16
                archive.writestr(info, data)
        return path

    def cli(self, *args, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch.object(sys, "stdin", io.StringIO(stdin)):
            code = versions.main([str(arg) for arg in args])
        return code, out.getvalue(), err.getvalue()

    def ok(self, *args):
        code, out, err = self.cli(*args)
        self.assertEqual(code, 0, out + err)
        return out

    def active(self):
        return versions.active_version(self.home)

    def entries(self):
        return sorted(child.name for child in self.home.versions.iterdir()) if self.home.versions.exists() else []

    def profile(self, start, env_file="/example/it's/canvas.env"):
        command = " ".join(shlex.quote(word) for word in (start, "--env-file", env_file))
        path = Path(os.environ["TUNNEL_CLIENT_PROFILE_DIR"]) / "canvas-reader.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("config_version: 1\ncontrol_plane:\n  base_url: https://api.openai.com\n"
                        "  tunnel_id: tunnel_123\n  api_key: env:CONTROL_PLANE_API_KEY\n"
                        f"mcp:\n  commands:\n    - channel: main\n      command: {json.dumps(command)}\n")
        path.chmod(0o600)
        return path

    def profile_command(self, path):
        line = next(line for line in path.read_text().splitlines() if "command:" in line)
        return shlex.split(json.loads(line.split("command:", 1)[1].strip()))

    def make_checkout(self):
        (self.checkout / "scripts").mkdir(exist_ok=True)
        (self.checkout / "src" / "canvas_reader").mkdir(parents=True, exist_ok=True)
        (self.checkout / "scripts" / "build_plugin.py").write_text("")
        start = self.checkout / "scripts" / "start.sh"
        start.write_bytes(START_SH)
        start.chmod(0o755)
        reader = self.checkout / ".venv" / "bin" / "canvas-reader"
        reader.parent.mkdir(parents=True, exist_ok=True)
        reader.write_text("#!/bin/sh\n")
        reader.chmod(0o755)
        return start

    def fake_checkout(self, version):
        """A source checkout (git clone) at ``version`` with a minimal build allowlist."""
        files = {
            "plugin.json": json.dumps({"name": "canvas-reader", "version": version}),
            "pyproject.toml": '[project]\nname = "canvas-reader"\nrequires-python = ">=3.9"\n',
            "src/canvas_reader/__init__.py": f'__version__ = "{version}"\n',
            "src/canvas_reader/server.py": "",
            "scripts/start.sh": START_SH.decode(),
            "scripts/versions.py": "",
            "scripts/build_plugin.py": (
                "from pathlib import Path\n"
                "FILES = ('plugin.json', 'pyproject.toml', 'src/canvas_reader/__init__.py',\n"
                "         'src/canvas_reader/server.py', 'scripts/start.sh', 'scripts/versions.py')\n"
                "def package_files(root, target='unified'):\n"
                "    return {name: (Path(root) / name).read_bytes() for name in FILES}, None\n"),
            "tests/test_only_in_checkout.py": "",
        }
        for relative, text in files.items():
            path = self.checkout / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    # ---- installing ---------------------------------------------------------

    def test_install_checks_and_activates_without_leaving_build_files(self):
        out = self.ok("install", self.bundle("0.3.0"))
        self.assertIn("Installed and checked 0.3.0 (2 tools). Active: 0.3.0.", out)
        self.assertIn("Next: connect your assistants (docs/SETUP.md, step 4).", out)  # no tunnel profile yet
        folder = self.home.versions / "0.3.0"
        self.assertEqual(os.readlink(self.home.current), os.path.join("versions", "0.3.0"))
        self.assertEqual(self.entries(), ["0.3.0"])
        for leftover in (".installing", "build", "src/canvas_reader.egg-info"):
            self.assertFalse((folder / leftover).exists(), leftover)
        self.assertTrue(os.access(folder / "scripts" / "start.sh", os.X_OK))
        self.assertEqual(json.loads((folder / ".install.json").read_text())["tools"], 2)
        # The start check used dummy credentials and a throwaway home, never the user's.
        token, home = self.log.read_text().split()
        self.assertEqual(token, "check-only")
        self.assertNotEqual(home, str(self.user_home))
        self.assertFalse(Path(home).exists())

    def test_update_picks_newest_release_and_rollback_switches_back(self):
        self.ok("install", self.bundle("0.3.0"))
        self.bundle("0.9.0")
        self.bundle("0.10.0")
        self.bundle("0.11.0", name="canvas-reader-0.11.0-source.zip")  # not a release bundle
        out = self.ok("update")
        self.assertIn("Active: 0.10.0 (was 0.3.0)", out)
        self.assertIn("Already up to date", self.ok("update"))
        self.assertIn("Active: 0.3.0", self.ok("rollback"))
        self.assertEqual(self.active(), "0.3.0")
        self.ok("rollback")
        self.assertEqual(self.active(), "0.10.0")
        listing = self.ok("list")
        self.assertRegex(listing, r"\* 0\.10\.0 .*active")
        self.assertRegex(listing, r"  0\.3\.0 .*previous")
        history = json.loads(self.home.state_file.read_text())["history"]
        self.assertEqual([entry["action"] for entry in history], ["install", "update", "rollback", "rollback"])

    def test_update_from_a_clone_installs_its_newer_source(self):
        self.ok("install", self.bundle("0.3.0"))
        self.fake_checkout("0.5.0")  # git pull brought 0.5.0; dist/ still has only 0.3.0
        self.assertIn("Update:      0.5.0 is available", self.ok("status"))
        out = self.ok("update")
        self.assertIn(f"Installing Canvas Reader 0.5.0 from {self.checkout}", out)
        self.assertIn("Active: 0.5.0 (was 0.3.0)", out)
        self.assertFalse((self.home.versions / "0.5.0" / "tests").exists())
        self.assertIn("Already up to date: 0.5.0 is active", self.ok("update"))

    def test_update_prefers_a_release_zip_that_is_as_new_or_newer_than_the_clone(self):
        self.fake_checkout("0.5.0")
        self.bundle("0.5.0")
        out = self.ok("update")
        self.assertIn(f"from {self.dist / 'canvas-reader-0.5.0.zip'}", out)
        self.bundle("0.6.0")
        self.assertIn("Active: 0.6.0 (was 0.5.0)", self.ok("update"))
        self.assertIn("Already up to date: 0.6.0 is active", self.ok("update"))

    def test_update_without_any_newer_source_keeps_todays_messages(self):
        code, _, err = self.cli("update")
        self.assertEqual(code, 1)
        self.assertIn("No canvas-reader-X.Y.Z.zip found", err)
        self.ok("install", self.bundle("0.3.0"))
        self.fake_checkout("0.2.0")
        self.assertIn("Already up to date: 0.3.0 is active", self.ok("update"))

    def github(self):
        fake = FakeGitHub()
        self.addCleanup(fake.close)
        os.environ.update(CANVAS_READER_GITHUB_API=fake.base, CANVAS_READER_GITHUB_REPOSITORY="example/canvas-reader")
        return fake

    def published(self, version):
        """A release zip that lives only on the fake GitHub, not in dist/."""
        archive = self.bundle(version)
        moved = self.tmp / archive.name
        archive.rename(moved)
        return moved

    def test_update_from_github_checks_the_download_and_installs_it(self):
        fake = self.github()
        self.ok("install", self.bundle("0.3.0"))
        fake.publish(self.published("0.4.0"))
        out = self.ok("update", "--github")
        self.assertIn("Installing Canvas Reader 0.4.0 from the GitHub release (canvas-reader-0.4.0.zip)", out)
        self.assertIn("Active: 0.4.0 (was 0.3.0)", out)
        info = json.loads((self.home.versions / "0.4.0" / ".install.json").read_text())
        self.assertEqual(info["source"], "the GitHub release (canvas-reader-0.4.0.zip)")
        # The temporary download is neither kept nor remembered as an update folder.
        self.assertEqual(json.loads(self.home.state_file.read_text())["last_source"], str(self.dist))
        downloads = len(fake.requests)
        self.assertIn("Already up to date: 0.4.0 is active", self.ok("update", "--github"))
        self.assertEqual(fake.requests[downloads:], ["/repos/example/canvas-reader/releases/latest"])

    def test_github_download_that_fails_its_checksum_is_not_installed(self):
        fake = self.github()
        self.ok("install", self.bundle("0.3.0"))
        fake.publish(self.published("0.4.0"), checksum="f" * 64)
        code, _, err = self.cli("update", "--github")
        self.assertEqual(code, 1)
        self.assertIn("doesn't match its checksum. Nothing was installed", err)
        self.assertEqual((self.entries(), self.active()), (["0.3.0"], "0.3.0"))
        fake.publish(self.published("0.4.1"), sums=False)
        code, _, err = self.cli("update", "--github")
        self.assertIn("has no SHA256SUMS file", err)
        self.assertEqual(self.entries(), ["0.3.0"])

    def test_github_problems_are_explained_without_changes(self):
        fake = self.github()
        code, _, err = self.cli("update", "--github")  # nothing published yet
        self.assertEqual(code, 1)
        self.assertIn("GitHub has no published release for example/canvas-reader (HTTP 404)", err)
        fake.publish(self.published("0.4.0"), scheme_base="ftp://127.0.0.1")
        code, _, err = self.cli("update", "--github")
        self.assertIn("doesn't use HTTPS", err)
        code, _, err = self.cli("update", "--github", self.dist)
        self.assertIn("either a SOURCE or --github", err)
        fake.close()
        code, _, err = self.cli("update", "--github")
        self.assertIn("Check your internet connection", err)
        self.assertEqual(self.entries(), [])
        with self.assertRaises(versions.Failure):
            versions._https_only("http://downloads.example/canvas-reader-0.4.0.zip", "https://api.github.com")
        self.assertEqual(versions._https_only("https://github.com/a/b", "https://api.github.com"),
                         "https://github.com/a/b")

    def test_failed_install_leaves_nothing_and_says_what_failed(self):
        self.ok("install", self.bundle("0.3.0"))
        self.environment("pip_fails")
        code, _, err = self.cli("install", self.bundle("0.3.1"))
        self.assertEqual(code, 1)
        self.assertIn("Canvas Reader 0.3.1 was not installed: installing dependencies failed (exit status 1).", err)
        self.assertIn("No matching distribution", err)
        self.assertNotIn("hunter2", err)
        self.assertIn("Nothing was changed; 0.3.0 is still active.", err)
        self.assertEqual(self.entries(), ["0.3.0"])
        self.assertEqual(self.active(), "0.3.0")

    def test_versions_that_do_not_start_are_never_activated(self):
        self.ok("install", self.bundle("0.3.0"))
        for outcome, message in (("check_fails", "checking that it starts failed (exit status 1)"),
                                 ("wrong_version", "reports version 9.9.9, not 0.3.1")):
            with self.subTest(outcome=outcome):
                self.environment(outcome)
                code, _, err = self.cli("install", self.bundle("0.3.1"))
                self.assertEqual(code, 1)
                self.assertIn(message, err)
                self.assertEqual((self.entries(), self.active()), (["0.3.0"], "0.3.0"))

    def test_interrupted_install_cleans_up(self):
        self.ok("install", self.bundle("0.3.0"))
        self.environment("interrupt")
        code, _, err = self.cli("install", self.bundle("0.3.1"))
        self.assertEqual(code, 130)
        self.assertIn("Stopped; Canvas Reader 0.3.1 was not installed. Nothing was changed; 0.3.0 is still active.",
                      err)
        self.assertEqual(self.entries(), ["0.3.0"])

    def test_leftovers_from_a_crash_are_reported_then_removed(self):
        self.ok("install", self.bundle("0.3.0"))
        (self.home.versions / "0.3.1").mkdir()
        (self.home.versions / "0.3.1" / ".installing").write_text("{}")
        (self.home.versions / ".staging-x").mkdir()
        self.assertIn("Leftovers:", self.ok("status"))
        self.assertNotIn("0.3.1", self.ok("list"))
        out = self.ok("install", self.bundle("0.3.2"))
        self.assertIn("Removed an incomplete install of 0.3.1", out)
        self.assertEqual(self.entries(), ["0.3.0", "0.3.2"])

    def test_unsafe_or_inconsistent_bundles_are_refused(self):
        def raw_zip(name, entries):
            path = self.tmp / name
            with zipfile.ZipFile(path, "w") as archive:
                for entry, mode in entries:
                    info = zipfile.ZipInfo(entry)
                    info.external_attr = mode << 16
                    archive.writestr(info, "x")
            return path

        cases = {
            "unsafe path": raw_zip("a.zip", [("plugin.json", 0o644), ("../evil", 0o644)]),
            "unsafe path ": raw_zip("b.zip", [("/etc/evil", 0o644)]),
            "symbolic link": raw_zip("c.zip", [("plugin.json", 0o644), ("link", stat.S_IFLNK | 0o777)]),
            "version numbers disagree": self.bundle("0.3.1", init="0.3.0"),
            "not a Canvas Reader bundle": raw_zip("d.zip", [("README.md", 0o644)]),
            "not a readable zip file": self.tmp / "e.zip",
        }
        (self.tmp / "e.zip").write_text("not a zip")
        for message, path in cases.items():
            with self.subTest(message=message):
                code, _, err = self.cli("install", path)
                self.assertEqual(code, 1)
                self.assertIn(message.strip(), err)
                self.assertEqual(self.entries(), [])
        self.assertFalse((self.tmp / "evil").exists())

    def test_reinstall_needs_force_and_restores_the_original_on_failure(self):
        self.ok("install", self.bundle("0.3.0"))
        info = (self.home.versions / "0.3.0" / ".install.json").read_text()
        code, _, err = self.cli("install", self.bundle("0.3.0"))
        self.assertEqual(code, 1)
        self.assertIn("already installed", err)
        self.environment("pip_fails")
        self.assertEqual(self.cli("install", "--force", self.bundle("0.3.0"))[0], 1)
        self.assertEqual((self.home.versions / "0.3.0" / ".install.json").read_text(), info)
        self.assertEqual(self.entries(), ["0.3.0"])
        self.environment("ok")
        self.ok("install", "--force", self.bundle("0.3.0"))
        self.assertEqual(self.entries(), ["0.3.0"])

    def test_final_install_state_failure_restores_new_and_forced_installs(self):
        self.ok("install", self.bundle("0.3.0"))
        previous_state = self.home.state_file.read_bytes()
        previous_link = os.readlink(self.home.current)
        original_info = (self.home.versions / "0.3.0" / ".install.json").read_bytes()
        save_state = versions.save_state

        def fail_after_saving(home, state):
            save_state(home, state)
            raise OSError("state save failed")

        for force in (False, True):
            for no_activate in (False, True):
                with self.subTest(force=force, no_activate=no_activate):
                    version = "0.3.0" if force else "0.3.1"
                    args = ["install", self.bundle(version)]
                    if force:
                        args.append("--force")
                    if no_activate:
                        args.append("--no-activate")
                    with patch.object(versions, "save_state", fail_after_saving):
                        code, _, err = self.cli(*args)
                    self.assertEqual(code, 1, err)
                    self.assertIn("Nothing was changed; 0.3.0 is still active", err)
                    self.assertEqual(os.readlink(self.home.current), previous_link)
                    self.assertEqual(self.home.state_file.read_bytes(), previous_state)
                    self.assertEqual(self.entries(), ["0.3.0"])
                    self.assertEqual((self.home.versions / "0.3.0" / ".install.json").read_bytes(), original_info)

    def test_failed_force_install_restores_a_dangling_original_link(self):
        self.home.versions.mkdir(parents=True)
        target = self.home.versions / "0.3.0"
        target.symlink_to("missing-original")
        self.environment("pip_fails")
        code, _, err = self.cli("install", "--force", self.bundle("0.3.0"))
        self.assertEqual(code, 1, err)
        self.assertTrue(target.is_symlink())
        self.assertFalse(target.exists())
        self.assertEqual(os.readlink(target), "missing-original")
        self.assertEqual(self.entries(), ["0.3.0"])

    def test_install_metadata_failure_restores_new_and_forced_installs(self):
        self.ok("install", self.bundle("0.3.0"))
        previous_state = self.home.state_file.read_bytes()
        original_info = (self.home.versions / "0.3.0" / ".install.json").read_bytes()
        write_text = Path.write_text

        def fail_install_info(path, *args, **kwargs):
            if path.name == ".install.json":
                raise OSError("install metadata failed")
            return write_text(path, *args, **kwargs)

        for force in (False, True):
            with self.subTest(force=force):
                args = ["install", self.bundle("0.3.0" if force else "0.3.1")]
                if force:
                    args.append("--force")
                with patch.object(Path, "write_text", fail_install_info):
                    code, _, err = self.cli(*args)
                self.assertEqual(code, 1, err)
                self.assertIn("install metadata failed", err)
                self.assertEqual(self.active(), "0.3.0")
                self.assertEqual(self.home.state_file.read_bytes(), previous_state)
                self.assertEqual(self.entries(), ["0.3.0"])
                self.assertEqual((self.home.versions / "0.3.0" / ".install.json").read_bytes(), original_info)

    def test_install_failure_after_activation_restores_the_previous_link(self):
        self.ok("install", self.bundle("0.3.0"))
        previous_state = self.home.state_file.read_bytes()
        activate = versions.activate

        def fail_after_activation(*args, **kwargs):
            activate(*args, **kwargs)
            raise OSError("final activation failed")

        with patch.object(versions, "activate", fail_after_activation):
            code, _, err = self.cli("install", self.bundle("0.3.1"))
        self.assertEqual(code, 1, err)
        self.assertIn("Nothing was changed; 0.3.0 is still active", err)
        self.assertEqual(self.active(), "0.3.0")
        self.assertEqual(self.home.state_file.read_bytes(), previous_state)
        self.assertEqual(self.entries(), ["0.3.0"])

    def test_wrapped_zips_and_source_folders_install(self):
        self.ok("install", "--no-activate", self.bundle("0.0.9", wrap=True))
        self.assertIsNone(self.active())
        self.assertTrue((self.home.versions / "0.0.9" / "plugin.json").is_file())
        # A source checkout is installed through its own build allowlist: no tests or environments.
        expected = versions.bundle_version(ROOT)
        self.ok("install", ROOT)
        folder = self.home.versions / expected
        self.assertTrue((folder / "scripts" / "versions.py").is_file())
        self.assertFalse((folder / "tests").exists())
        self.assertFalse((folder / "dist").exists())
        self.assertEqual(self.active(), expected)

    # ---- switching and removing ---------------------------------------------

    def test_use_remove_and_prune(self):
        for version in ("0.1.0", "0.2.0", "0.3.0", "0.4.0"):
            self.ok("install", self.bundle(version))
        code, _, err = self.cli("remove", "0.4.0")
        self.assertEqual(code, 1)
        self.assertIn("is active", err)
        self.assertIn("Active: 0.1.0", self.ok("use", "0.1.0"))
        self.assertEqual(self.cli("use", "9.9.9")[0], 1)
        self.assertIn("Removed 0.2.0, 0.3.0.", self.ok("prune", "--keep", "1"))
        self.assertEqual(self.entries(), ["0.1.0", "0.4.0"])
        self.ok("use", "0.4.0")
        self.assertIn("Removed 0.1.0.", self.ok("remove", "0.1.0"))
        self.assertEqual(self.entries(), ["0.4.0"])

    def test_one_command_at_a_time(self):
        self.ok("install", self.bundle("0.3.0"))
        with versions.locked(self.home):
            code, _, err = self.cli("install", self.bundle("0.3.1"))
        self.assertEqual(code, 1)
        self.assertIn("Another versions.py command is running", err)

    def test_a_stale_lock_descriptor_never_enters_the_managed_folder(self):
        flock = versions.fcntl.flock
        for recreate in (False, True):
            with self.subTest(recreate=recreate):
                def replace_after_lock(descriptor, operation):
                    flock(descriptor, operation)
                    if operation & versions.fcntl.LOCK_EX:
                        self.home.lock_file.unlink()
                        if recreate:
                            self.home.lock_file.write_text("")

                with patch.object(versions.fcntl, "flock", replace_after_lock):
                    with self.assertRaisesRegex(versions.Failure, "lock changed"):
                        with versions.locked(self.home):
                            self.fail("a stale lock allowed a managed-folder operation")
                # The stale descriptor was unlocked and closed; the actual path can be locked normally.
                with versions.locked(self.home):
                    self.assertTrue(self.home.lock_file.is_file())

    def test_activation_state_failure_keeps_link_and_history_unchanged(self):
        self.ok("install", self.bundle("0.3.0"))
        self.ok("install", "--no-activate", self.bundle("0.3.1"))
        previous_state = self.home.state_file.read_bytes()
        previous_link = os.readlink(self.home.current)
        save_state = versions.save_state
        for write_first in (False, True):
            with self.subTest(write_first=write_first):
                def fail_save(home, state):
                    if write_first:
                        save_state(home, state)
                    raise OSError("state save failed")

                with patch.object(versions, "save_state", fail_save):
                    code, _, err = self.cli("use", "0.3.1")
                self.assertEqual(code, 1, err)
                self.assertIn("previous active version and version settings were restored", err)
                self.assertEqual(os.readlink(self.home.current), previous_link)
                self.assertEqual(self.home.state_file.read_bytes(), previous_state)
                self.assertFalse(list(self.home.root.glob(".current-*")))

    def test_activation_link_failure_restores_saved_history(self):
        self.ok("install", self.bundle("0.3.0"))
        self.ok("install", "--no-activate", self.bundle("0.3.1"))
        previous_state = self.home.state_file.read_bytes()
        replace = versions.os.replace

        def fail_current_replace(source, destination):
            if Path(destination) == self.home.current:
                raise OSError("link switch failed")
            return replace(source, destination)

        with patch.object(versions.os, "replace", fail_current_replace):
            code, _, err = self.cli("use", "0.3.1")
        self.assertEqual(code, 1, err)
        self.assertIn("link switch failed", err)
        self.assertEqual(self.active(), "0.3.0")
        self.assertEqual(self.home.state_file.read_bytes(), previous_state)
        self.assertFalse(list(self.home.root.glob(".current-*")))

    # ---- the tunnel profile -------------------------------------------------

    def test_link_tunnel_changes_only_the_launcher_and_keeps_a_backup(self):
        checkout_start = self.make_checkout()
        profile = self.profile(str(checkout_start))
        original = profile.read_text()
        code, _, err = self.cli("link-tunnel")
        self.assertEqual(code, 1)
        self.assertIn("No managed version is installed yet", err)
        out = self.ok("install", self.bundle("0.3.0"))
        self.assertIn("The tunnel still runs this checkout", out)
        self.assertIn("Dry run", self.ok("link-tunnel", "--dry-run"))
        self.assertEqual(profile.read_text(), original)

        out = self.ok("link-tunnel")
        self.assertIn("Restart the tunnel", out)
        managed = str(self.home.current / "scripts" / "start.sh")
        self.assertEqual(self.profile_command(profile), [managed, "--env-file", "/example/it's/canvas.env"])
        changed = [(a, b) for a, b in zip(original.splitlines(), profile.read_text().splitlines()) if a != b]
        self.assertEqual(len(changed), 1)
        self.assertEqual(stat.S_IMODE(profile.stat().st_mode), 0o600)
        backups = list(profile.parent.glob("canvas-reader.yaml.bak-*"))
        self.assertEqual([backup.read_text() for backup in backups], [original])
        self.assertIn("already runs", self.ok("link-tunnel"))
        self.assertIn("Tunnel runs: managed versions (follows the active version)", self.ok("status"))

        self.ok("link-tunnel", "--to", "checkout")
        self.assertEqual(self.profile_command(profile)[0], str(checkout_start))

    def test_link_tunnel_refuses_profiles_without_a_reader_command(self):
        self.ok("install", self.bundle("0.3.0"))
        profile = self.profile("/usr/bin/other-server")
        code, _, err = self.cli("link-tunnel")
        self.assertEqual(code, 1)
        self.assertIn("has no Canvas Reader command", err)
        self.assertNotIn("current", profile.read_text())

    # ---- status -------------------------------------------------------------

    def test_status_flags_old_readers_open_credentials_and_updates(self):
        self.ok("install", self.bundle("0.3.0"))
        self.ok("install", self.bundle("0.3.1"))
        self.bundle("0.4.0")
        credentials = self.user_home / ".config" / "canvas-reader" / "canvas.env"
        credentials.parent.mkdir(parents=True)
        credentials.write_text("CANVAS_API_TOKEN=x\n")
        credentials.chmod(0o644)
        versions_dir = self.home.versions
        self.processes = [
            (4242, datetime(2000, 1, 1, 9, 30),  # noqa: DTZ001 - ps reports local wall-clock time
             (f"{versions_dir}/0.3.0/.venv/bin/python "
              f"{self.home.current}/.venv/bin/canvas-reader --env-file x")),
            (4343, None, "/usr/bin/python3 scripts/versions.py status"),
        ]
        out = self.ok("status")
        self.assertIn("Active:      0.3.1", out)
        self.assertIn("Previous:    0.3.0", out)
        self.assertIn("pid 4242, version 0.3.0, since Jan 01 09:30  <- older than the active 0.3.1", out)
        self.assertNotIn("4343", out)
        self.assertIn("readable by other users", out)
        self.assertIn("Update:      0.4.0 is available", out)

    # ---- removing everything ------------------------------------------------

    def test_uninstall_removes_only_what_was_asked(self):
        checkout_start = self.make_checkout()
        self.ok("install", self.bundle("0.3.0"))
        tunnel_credentials = self.tmp / "connector" / ".env"
        tunnel_credentials.parent.mkdir()
        tunnel_credentials.write_text("CANVAS_API_TOKEN=x\n")
        profile = self.profile(str(self.home.current / "scripts" / "start.sh"), str(tunnel_credentials))
        default_credentials = self.user_home / ".config" / "canvas-reader" / "canvas.env"
        default_credentials.parent.mkdir(parents=True)
        default_credentials.write_text("CANVAS_API_TOKEN=x\n")
        security = self.tmp / "security"
        security.write_text(f'#!/bin/sh\necho "$@" >> "{self.tmp / "security.log"}"\n')
        security.chmod(0o755)
        os.environ["CANVAS_READER_SECURITY_BIN"] = str(security)

        code, _, err = self.cli("uninstall", "--key", "--credentials")
        self.assertEqual(code, 1)
        self.assertIn("Add --yes to confirm", err)
        self.assertTrue(self.home.root.exists())

        out = self.ok("uninstall", "--yes", "--key", "--credentials")
        self.assertFalse(self.home.root.exists())
        self.assertEqual(self.profile_command(profile)[0], str(checkout_start))
        self.assertFalse(default_credentials.exists())
        self.assertFalse(default_credentials.parent.exists())
        self.assertTrue(tunnel_credentials.exists())
        self.assertIn(f"Not deleted: {tunnel_credentials}", out)
        self.assertEqual((self.tmp / "security.log").read_text().splitlines(),
                         ["delete-generic-password -s canvas-reader-tunnel",
                          "delete-generic-password -s canvas-reader-canvas-token"])
        self.assertIn("Nothing to remove", self.ok("uninstall", "--yes"))

    def test_token_command_uses_the_keychain_item_the_server_reads(self):
        code, _, err = self.cli("token", "status")
        self.assertEqual(code, 1)
        self.assertIn("Keychain tool is unavailable", err)
        store = self.tmp / "keychain.txt"
        security = self.tmp / "security"
        security.write_text(
            "#!/bin/sh\n"
            f'echo "$@" >> "{self.tmp / "security.log"}"\n'
            f'case "$1" in\n'
            f'  add-generic-password) echo saved > "{store}" ;;\n'
            f'  find-generic-password) [ -f "{store}" ] || exit 44 ;;\n'
            f'  delete-generic-password) [ -f "{store}" ] || exit 44; rm "{store}" ;;\n'
            "esac\n")
        security.chmod(0o755)
        os.environ["CANVAS_READER_SECURITY_BIN"] = str(security)
        self.assertIn("No Canvas token is saved", self.ok("token", "status"))
        self.assertIn('Saved in your login Keychain as "canvas-reader-canvas-token"', self.ok("token", "store"))
        self.assertIn("A Canvas token is saved", self.ok("token", "status"))
        self.assertIn("Removed the Canvas token", self.ok("token", "forget"))
        self.assertIn("No Canvas token named", self.ok("token", "forget"))
        calls = (self.tmp / "security.log").read_text().splitlines()
        self.assertIn("add-generic-password -U -a canvas-reader -s canvas-reader-canvas-token "
                      "-l Canvas Reader Canvas API token -w", calls)
        # status reads only the item's attributes, never the token itself (-w).
        self.assertIn("find-generic-password -s canvas-reader-canvas-token", calls)
        self.assertFalse(any(call.startswith("find-generic-password") and call.endswith("-w") for call in calls))
        server = (ROOT / "src" / "canvas_reader" / "server.py").read_text()
        self.assertIn(f'KEYCHAIN_SERVICE = "{versions.TOKEN_SERVICE}"', server)

    def test_uninstall_refuses_a_mixed_custom_folder_before_external_changes(self):
        os.environ["CANVAS_READER_HOME"] = str(self.tmp / "mixed")
        self.home = versions.Home(versions.home_dir())
        checkout_start = self.make_checkout()
        self.ok("install", self.bundle("0.3.0"))
        unrelated = self.home.root / "my-project"
        unrelated.mkdir()
        (unrelated / "important.txt").write_text("keep this")
        profile = self.profile(str(self.home.current / "scripts" / "start.sh"))
        previous_profile = profile.read_bytes()
        credentials = self.user_home / ".config" / "canvas-reader" / "canvas.env"
        credentials.parent.mkdir(parents=True)
        credentials.write_text("CANVAS_API_TOKEN=x\n")
        with patch.object(versions, "relink") as relink, patch.object(versions.subprocess, "run") as run:
            code, _, err = self.cli("uninstall", "--yes", "--key", "--credentials")
        self.assertEqual(code, 1, err)
        self.assertIn("Refusing to uninstall", err)
        self.assertIn("my-project", err)
        self.assertEqual((unrelated / "important.txt").read_text(), "keep this")
        self.assertTrue((self.home.versions / "0.3.0").is_dir())
        self.assertEqual(profile.read_bytes(), previous_profile)
        self.assertTrue(credentials.is_file())
        self.assertTrue(checkout_start.is_file())
        relink.assert_not_called()
        run.assert_not_called()

    def test_uninstall_refuses_unrecognized_versions_and_redirected_folders(self):
        self.ok("install", self.bundle("0.3.0"))
        unknown = self.home.versions / "0.9.0"
        unknown.mkdir()
        (unknown / "important.txt").write_text("keep")
        code, _, err = self.cli("uninstall", "--yes")
        self.assertEqual(code, 1, err)
        self.assertIn("0.9.0", err)
        self.assertTrue((unknown / "important.txt").is_file())
        versions.remove_tree(unknown)

        redirected = self.tmp / "redirected"
        self.home.versions.rename(redirected)
        self.home.versions.symlink_to(redirected, target_is_directory=True)
        code, _, err = self.cli("uninstall", "--yes")
        self.assertEqual(code, 1, err)
        self.assertIn("versions", err)
        self.assertTrue((redirected / "0.3.0" / "plugin.json").is_file())
        self.home.versions.unlink()
        redirected.rename(self.home.versions)

        link = self.tmp / "managed-link"
        link.symlink_to(self.home.root, target_is_directory=True)
        os.environ["CANVAS_READER_HOME"] = str(link)
        code, _, err = self.cli("uninstall", "--yes")
        self.assertEqual(code, 1, err)
        self.assertIn("managed-link", err)
        self.assertTrue((self.home.versions / "0.3.0" / "plugin.json").is_file())

    def test_uninstall_keeps_unrelated_files_added_after_its_guard(self):
        self.make_checkout()
        self.ok("install", self.bundle("0.3.0"))
        self.profile(str(self.home.current / "scripts" / "start.sh"))
        unexpected = self.home.root / "important.txt"
        with patch.object(versions, "relink", lambda *args, **kwargs: unexpected.write_text("keep")):
            code, _, err = self.cli("uninstall", "--yes")
        self.assertEqual(code, 1, err)
        self.assertIn("folder was retained", err)
        self.assertEqual(unexpected.read_text(), "keep")

    def test_uninstall_removes_interrupted_install_leftovers_and_finder_files(self):
        self.ok("install", self.bundle("0.3.0"))
        (self.home.versions / ".staging-ab12cd_3" / "src").mkdir(parents=True)
        for folder in (self.home.root, self.home.versions):
            (folder / ".DS_Store").write_bytes(b"\0")
        outside = self.tmp / "outside"
        outside.mkdir()
        disguised = self.home.versions / ".staging-link"
        disguised.symlink_to(outside, target_is_directory=True)
        code, _, err = self.cli("uninstall", "--yes")
        self.assertEqual(code, 1, err)
        self.assertIn(".staging-link", err)
        disguised.unlink()
        self.ok("uninstall", "--yes")
        self.assertFalse(self.home.root.exists())
        self.assertTrue(outside.is_dir())

    def test_uninstall_holds_its_lock_until_the_managed_root_is_removed(self):
        self.ok("install", self.bundle("0.3.0"))
        descriptor = os.open(self.home.lock_file, os.O_RDWR)
        self.addCleanup(os.close, descriptor)
        rmdir = Path.rmdir
        checked = []

        def assert_locked_before_removal(path):
            if path == self.home.root:
                self.assertFalse(self.home.lock_file.exists())
                with self.assertRaises(OSError):
                    versions.fcntl.flock(descriptor, versions.fcntl.LOCK_EX | versions.fcntl.LOCK_NB)
                checked.append(True)
            return rmdir(path)

        with patch.object(Path, "rmdir", assert_locked_before_removal):
            self.ok("uninstall", "--yes")
        self.assertEqual(checked, [True])
        self.assertFalse(self.home.root.exists())

    # ---- releasing ----------------------------------------------------------

    def test_bump_saves_the_old_source_and_sets_the_new_version_everywhere(self):
        root = self.checkout
        (root / "src" / "canvas_reader").mkdir(parents=True)
        (root / "scripts").mkdir()
        (root / "tests").mkdir()
        (root / ".venv").mkdir()
        (root / ".tools").mkdir()
        (root / ".cache" / "pip").mkdir(parents=True)
        (root / ".claude-plugin").mkdir()
        manifest = {"name": "canvas-reader", "version": "0.3.0", "extensions": {"x": {"version": "keep"}}}
        files = {
            "plugin.json": json.dumps(manifest, indent=2) + "\n",
            ".claude-plugin/plugin.json": json.dumps({"name": "canvas-reader", "version": "0.3.0",
                                                      "mcpServers": {"x": {"command": "y"}}}, indent=2) + "\n",
            "pyproject.toml": '[project]\nname = "canvas-reader"\ndynamic = ["version"]\n',
            "src/canvas_reader/__init__.py": '"""Canvas Reader."""\n\n__version__ = "0.3.0"\n',
            "src/canvas_reader/server.py": "",
            "scripts/start.sh": "#!/bin/sh\n",
            "scripts/build_plugin.py": "",
            "CHANGELOG.md": "# Changelog\n\n## 0.3.0 (2000-01-01)\n\n- Old.\n",
            "tests/test_a.py": "",
            ".env": "CANVAS_API_TOKEN=secret\n",
            ".env.example": "CANVAS_API_TOKEN=\n",
            "canvas.env": "CANVAS_API_TOKEN=secret\n",
            ".venv/python": "",
            ".tools/tunnel-client": "",
            ".cache/pip/selfcheck": "",
            "tunnel-profile.yaml": "tunnel_id: tunnel_123\n",
            ".app.json": "{}",
            "PLAN.md": "Local notes are not released.\n",
            "AGENTS.md": "Local instructions are not released.\n",
            "CLAUDE.md": "Local instructions are not released.\n",
        }
        for relative, text in files.items():
            (root / relative).write_text(text)
        out = self.ok("bump", "0.3.1")
        self.assertIn("Version 0.3.0 -> 0.3.1", out)
        self.assertIn("Saved the 0.3.0 source", out)
        # The outgoing version's source is kept in dist/, without credentials, environments or local notes.
        with zipfile.ZipFile(self.dist / "canvas-reader-0.3.0-source.zip") as archive:
            names = {name.split("/", 1)[1] for name in archive.namelist()}
            self.assertTrue(all(entry.date_time == (1980, 1, 1, 0, 0, 0)
                                and not entry.extra and not entry.comment
                                for entry in archive.infolist()))
        self.assertEqual(names, {"plugin.json", ".claude-plugin/plugin.json", "pyproject.toml",
                                 "src/canvas_reader/__init__.py", "src/canvas_reader/server.py", "scripts/start.sh",
                                 "scripts/build_plugin.py", "tests/test_a.py", "CHANGELOG.md", ".env.example"})
        self.assertIn('__version__ = "0.3.1"', (root / "src/canvas_reader/__init__.py").read_text())
        bumped = json.loads((root / "plugin.json").read_text())
        self.assertEqual((bumped["version"], bumped["extensions"]["x"]["version"]), ("0.3.1", "keep"))
        claude = json.loads((root / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual((claude["version"], claude["mcpServers"]), ("0.3.1", {"x": {"command": "y"}}))
        changelog = (root / "CHANGELOG.md").read_text()
        self.assertRegex(changelog, r"^# Changelog\n\n## 0\.3\.1 \(\d{4}-\d\d-\d\d\)\n\n- Describe the changes here\.\n\n"
                                    r"## 0\.3\.0")
        # A changelog that already has the new version's heading is left as it is.
        (root / "CHANGELOG.md").write_text("# Changelog\n\n## 0.3.2 (2000-01-02)\n\n- Written.\n")
        self.assertIn("Saved the 0.3.1 source", self.ok("bump", "0.3.2"))
        self.assertEqual((root / "CHANGELOG.md").read_text(), "# Changelog\n\n## 0.3.2 (2000-01-02)\n\n- Written.\n")
        self.assertEqual(sorted(path.name for path in self.dist.iterdir()),
                         ["canvas-reader-0.3.0-source.zip", "canvas-reader-0.3.1-source.zip"])
        code, _, err = self.cli("bump", "0.3.2")
        self.assertEqual(code, 1)
        self.assertIn("not newer", err)

if __name__ == "__main__":
    unittest.main()
