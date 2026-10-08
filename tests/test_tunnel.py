from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPER = PROJECT_ROOT / "scripts" / "tunnel.sh"


class TunnelHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.capture = self.directory / "arguments.json"
        self.fake = self.directory / "tunnel-client"
        self.fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['CAPTURE_ARGS']).write_text(json.dumps(sys.argv[1:]))\n"
            "Path(os.environ['CAPTURE_ARGS'] + '.key').write_text(os.environ.get('CONTROL_PLANE_API_KEY', ''))\n"
            "print('mock tunnel-client 0.0.15')\n"
        )
        self.fake.chmod(0o755)
        # A stand-in for /usr/bin/security so tests never touch the real Keychain.
        self.keychain = self.directory / "keychain.json"
        self.security = self.directory / "security"
        self.security.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "store = Path(os.environ['FAKE_KEYCHAIN'])\n"
            "data = json.loads(store.read_text()) if store.exists() else {}\n"
            "args = sys.argv[1:]\n"
            "Path(str(store) + '.argv').write_text(json.dumps(args))\n"
            "service = args[args.index('-s') + 1]\n"
            "if args[0] == 'add-generic-password':\n"
            "    data[service] = sys.stdin.readline().strip()\n"
            "elif service not in data:\n"
            "    sys.exit(44)\n"
            "elif args[0] == 'find-generic-password' and '-w' in args:\n"
            "    print(data[service])\n"
            "elif args[0] == 'delete-generic-password':\n"
            "    del data[service]\n"
            "store.write_text(json.dumps(data))\n"
        )
        self.security.chmod(0o755)
        self.credentials = self.directory / "Canvas's $(touch SHOULD_NOT_EXIST) env file.env"
        self.credentials.write_text("CANVAS_API_TOKEN=canvas-secret-fixture\n")
        self.env = {
            "PATH": os.environ["PATH"],
            "TUNNEL_CLIENT_BIN": str(self.fake),
            "CAPTURE_ARGS": str(self.capture),
            "CANVAS_ENV_FILE": str(self.credentials),
            "CONTROL_PLANE_API_KEY": "runtime-secret-fixture",
            "CANVAS_READER_SECURITY_BIN": str(self.directory / "no-keychain-in-tests"),
            "FAKE_KEYCHAIN": str(self.keychain),
        }

    def run_helper(self, *args, env=None, stdin=""):
        return subprocess.run(
            ["/bin/bash", str(HELPER), *args],
            env=self.env if env is None else env,
            text=True,
            input=stdin,
            capture_output=True,
            cwd=self.directory,
        )

    def test_setup_quotes_paths_and_passes_only_secret_references(self):
        self.env["TUNNEL_CLIENT_PROFILE_DIR"] = str(self.directory / "profiles")
        result = self.run_helper("--setup", "tunnel_fixture123")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.capture.read_text())
        self.assertEqual(args[0], "init")
        self.assertEqual(args[args.index("--sample") + 1], "sample_mcp_stdio_local")
        self.assertEqual(args[args.index("--profile") + 1], "canvas-reader")
        self.assertEqual(args[args.index("--profile-dir") + 1], self.env["TUNNEL_CLIENT_PROFILE_DIR"])
        command = args[args.index("--mcp-command") + 1]
        self.assertEqual(shlex.split(command), [str(PROJECT_ROOT / "scripts" / "start.sh"), "--env-file", str(self.credentials.resolve())])
        self.assertNotIn("--env-file", args)
        self.assertNotIn("runtime-secret-fixture", json.dumps(args) + result.stdout + result.stderr)
        self.assertNotIn("canvas-secret-fixture", json.dumps(args) + result.stdout + result.stderr)
        self.assertFalse((self.directory / "SHOULD_NOT_EXIST").exists())
        self.assertNotIn("--force", args)

    def test_setup_points_at_the_installed_version_when_there_is_one(self):
        managed = self.directory / "managed home"
        start = managed / "versions" / "9.9.9" / "scripts" / "start.sh"
        start.parent.mkdir(parents=True)
        start.write_text("#!/bin/sh\n")
        start.chmod(0o755)
        (managed / "current").symlink_to(Path("versions") / "9.9.9")
        for env in ({"CANVAS_READER_HOME": str(managed)},
                    {"HOME": str(self.directory), "CANVAS_READER_HOME": "~/managed home"}):
            with self.subTest(env=env):
                result = self.run_helper("--setup", "tunnel_fixture123", env=dict(self.env, **env))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("follow updates", result.stderr)
                args = json.loads(self.capture.read_text())
                command = shlex.split(args[args.index("--mcp-command") + 1])
                self.assertEqual(command[0], str(managed / "current" / "scripts" / "start.sh"))
        # Without an installed version the profile runs this folder, and setup says what to do next.
        result = self.run_helper("--setup", "tunnel_fixture123", env=dict(self.env, CANVAS_READER_HOME=str(self.directory / "none")))
        self.assertIn("link-tunnel", result.stderr)
        command = shlex.split(json.loads(self.capture.read_text())[-1])
        self.assertEqual(command[0], str(PROJECT_ROOT / "scripts" / "start.sh"))

    def test_setup_uses_explicit_id_before_environment(self):
        self.env["CANVAS_READER_TUNNEL_ID"] = "tunnel_fromenv"
        result = self.run_helper("--setup", "tunnel_explicit")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.capture.read_text())
        self.assertEqual(args[args.index("--tunnel-id") + 1], "tunnel_explicit")
        result = self.run_helper("--setup")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.capture.read_text())
        self.assertEqual(args[args.index("--tunnel-id") + 1], "tunnel_fromenv")

    def test_missing_inputs_fail_before_cli_is_called(self):
        for removed, expected in (("CONTROL_PLANE_API_KEY", "CONTROL_PLANE_API_KEY"), ("CANVAS_ENV_FILE", "CANVAS_ENV_FILE")):
            env = dict(self.env)
            env.pop(removed)
            result = self.run_helper("--setup", "tunnel_fixture", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(expected, result.stderr)
            self.assertFalse(self.capture.exists())
        result = self.run_helper("--setup")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("actual tunnel ID", result.stderr)
        self.assertFalse(self.capture.exists())

    def test_doctor_and_run_use_only_documented_flags(self):
        self.env["CANVAS_READER_TUNNEL_PROFILE"] = "personal-canvas"
        for flag, expected in (("--doctor", ["doctor", "--profile", "personal-canvas", "--explain"]), ("--run", ["run", "--profile", "personal-canvas"])):
            result = self.run_helper(flag)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(self.capture.read_text()), expected)

    def test_status_is_local_and_does_not_echo_credentials(self):
        result = self.run_helper("--status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.capture.read_text()), ["--version"])
        self.assertIn("not checked", result.stdout)
        self.assertNotIn("runtime-secret-fixture", result.stdout + result.stderr)
        self.assertNotIn("canvas-secret-fixture", result.stdout + result.stderr)

    def test_missing_binary_has_actionable_install_error(self):
        self.env["TUNNEL_CLIENT_BIN"] = str(self.directory / "missing-client")
        result = self.run_helper("--doctor")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("brew install openai/tools/tunnel-client", result.stderr)
        self.assertFalse(self.capture.exists())

    def test_help_needs_no_configuration(self):
        result = self.run_helper("--help", env={"PATH": os.environ["PATH"]})
        self.assertEqual(result.returncode, 0)
        self.assertIn("--setup", result.stdout)
        self.assertFalse(self.capture.exists())

    def test_keychain_store_load_status_and_forget(self):
        env = dict(self.env, CANVAS_READER_SECURITY_BIN=str(self.security))
        env.pop("CONTROL_PLANE_API_KEY")
        self.assertIn("missing", self.run_helper("--status", env=env).stdout)
        result = self.run_helper("--store-key", env=env, stdin="runtime-from-keychain\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(Path(str(self.keychain) + ".argv").read_text())
        self.assertEqual(argv[-1], "-w")  # the key is typed at the prompt, never passed as an argument
        self.assertNotIn("runtime-from-keychain", json.dumps(argv) + result.stdout + result.stderr)
        self.assertIn("saved in Keychain", self.run_helper("--status", env=env).stdout)
        result = self.run_helper("--run", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(str(self.capture) + ".key").read_text(), "runtime-from-keychain")
        self.assertNotIn("runtime-from-keychain", result.stdout + result.stderr)
        self.assertIn("Keychain", result.stderr)
        self.assertEqual(self.run_helper("--forget-key", env=env).returncode, 0)
        result = self.run_helper("--run", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--store-key", result.stderr)

    def test_environment_key_wins_over_keychain(self):
        self.keychain.write_text(json.dumps({"canvas-reader-tunnel": "keychain-value"}))
        env = dict(self.env, CANVAS_READER_SECURITY_BIN=str(self.security))
        self.assertEqual(self.run_helper("--doctor", env=env).returncode, 0)
        self.assertEqual(Path(str(self.capture) + ".key").read_text(), "runtime-secret-fixture")

    def test_default_credential_file_is_used_for_setup_and_status(self):
        home = self.directory / "home"
        default = home / ".config" / "canvas-reader" / "canvas.env"
        default.parent.mkdir(parents=True)
        default.write_text("CANVAS_API_TOKEN=canvas-secret-fixture\n")
        env = dict(self.env, HOME=str(home))
        env.pop("CANVAS_ENV_FILE")
        status = self.run_helper("--status", env=env)
        self.assertIn(str(default), status.stdout)
        self.assertNotIn("canvas-secret-fixture", status.stdout + status.stderr)
        result = self.run_helper("--setup", "tunnel_fixture123", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.capture.read_text())
        command = shlex.split(args[args.index("--mcp-command") + 1])
        self.assertEqual(command[-2:], ["--env-file", str(default.resolve())])


if __name__ == "__main__":
    unittest.main()
