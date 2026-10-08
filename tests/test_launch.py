"""scripts/launch.sh: every client starts the installed, active version the same way."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = PROJECT_ROOT / "scripts" / "launch.sh"


class LauncherTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.tmp = Path(scratch.name)
        self.home = self.tmp / "home with spaces"
        self.home.mkdir()
        self.capture = self.tmp / "capture.json"
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.home),
                    "CAPTURE": str(self.capture)}

    def install(self, managed: Path) -> Path:
        """A fake active version whose start.sh records where it lives, its arguments, stdin and settings."""
        version = managed / "versions" / "9.9.9"
        (version / "scripts").mkdir(parents=True)
        start = version / "scripts" / "start.sh"
        start.write_text(
            f"#!/bin/sh\nexec {sys.executable} -c '"
            "import json, os, sys; json.dump({\"where\": sys.argv[1], \"argv\": sys.argv[2:], \"stdin\": sys.stdin.read(), "
            "\"env\": {k: os.environ.get(k) for k in (\"CANVAS_API_URL\", \"CANVAS_API_TOKEN\", \"TIMEZONE\", "
            "\"CANVAS_ENV_FILE\")}}, open(os.environ[\"CAPTURE\"], \"w\"))' "
            f"'{managed.name}' \"$@\"\n")
        start.chmod(0o755)
        (managed / "current").symlink_to(Path("versions") / "9.9.9")
        return start

    def launch(self, *args: str, stdin: str = "", **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(["/bin/sh", str(LAUNCHER), *args], input=stdin, text=True, capture_output=True,
                              env={**self.env, **env}, cwd=self.tmp)

    def captured(self) -> dict:
        return json.loads(self.capture.read_text())

    def test_default_location_passes_arguments_and_protocol_input(self):
        self.install(self.home / ".local" / "share" / "canvas-reader")
        message = '{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'
        result = self.launch("--env-file", "/elsewhere/canvas.env", stdin=message)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.captured()["argv"], ["--env-file", "/elsewhere/canvas.env"])
        self.assertEqual(self.captured()["stdin"], message)

    def test_same_home_rules_as_versions_py(self):
        custom = self.tmp / "custom"
        data = self.tmp / "data"
        self.install(custom)
        self.install(data / "canvas-reader")
        self.install(self.home / ".local" / "share" / "canvas-reader")
        (self.home / "elsewhere").mkdir()
        self.install(self.home / "elsewhere" / "custom-tilde")
        cases = (({"CANVAS_READER_HOME": str(custom)}, "custom"), ({"XDG_DATA_HOME": str(data)}, "canvas-reader"),
                 ({"XDG_DATA_HOME": "relative/data"}, "canvas-reader"), ({}, "canvas-reader"),
                 ({"CANVAS_READER_HOME": "~/elsewhere/custom-tilde"}, "custom-tilde"))
        for env, where in cases:
            with self.subTest(env=env):
                self.capture.unlink(missing_ok=True)
                result = self.launch(**env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.captured()["where"], where)
        # The XDG case must use data/canvas-reader, not the default under HOME.
        os.unlink(self.home / ".local" / "share" / "canvas-reader" / "current")
        self.assertEqual(self.launch(XDG_DATA_HOME=str(data)).returncode, 0)
        self.assertNotEqual(self.launch(XDG_DATA_HOME="relative/data").returncode, 0)
        # versions.py agrees on every one of these locations.
        script = ("import importlib.util, sys; spec = importlib.util.spec_from_file_location('v', sys.argv[1]); "
                  "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
                  "print(module.home_dir())")
        expected = {"CANVAS_READER_HOME": custom, "XDG_DATA_HOME": data / "canvas-reader"}
        for key, folder in expected.items():
            output = subprocess.run([sys.executable, "-c", script, str(PROJECT_ROOT / "scripts" / "versions.py")],
                                    env={**self.env, key: str(folder if key == "CANVAS_READER_HOME" else data)},
                                    capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(Path(output), folder)

    def test_not_installed_prints_one_actionable_line(self):
        result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertIn("python3 scripts/versions.py install", result.stderr)

    def test_unset_client_settings_are_dropped_and_real_ones_kept(self):
        self.install(self.home / ".local" / "share" / "canvas-reader")
        result = self.launch(CANVAS_API_URL="${user_config.canvas_url}", CANVAS_API_TOKEN="",
                             TIMEZONE="America/Chicago", CANVAS_ENV_FILE="${user_config.env_file}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.captured()["env"], {"CANVAS_API_URL": None, "CANVAS_API_TOKEN": None,
                                                  "TIMEZONE": "America/Chicago", "CANVAS_ENV_FILE": None})
        result = self.launch(CANVAS_API_URL="https://school.example.edu", CANVAS_API_TOKEN="fixture-token")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.captured()["env"]["CANVAS_API_TOKEN"], "fixture-token")
        self.assertEqual(self.captured()["env"]["CANVAS_API_URL"], "https://school.example.edu")

    def test_launcher_ships_executable_in_the_bundle(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("build_plugin", PROJECT_ROOT / "scripts" / "build_plugin.py")
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        self.assertIn("scripts/launch.sh", build.BUNDLE_FILES)
        self.assertTrue(os.access(LAUNCHER, os.X_OK))


if __name__ == "__main__":
    unittest.main()
