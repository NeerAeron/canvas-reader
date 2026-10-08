from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import posixpath
import re
import secrets
import struct
import tempfile
import unittest
import zipfile
import zlib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_plugin", PROJECT_ROOT / "scripts" / "build_plugin.py")
assert SPEC and SPEC.loader
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)
# Synthetic ID only, used to prove the publication check; it is not a real connection.
TEST_APP_ID = "plugin_asdk_app_" + ("1234abcdef567890" * 2)
# Linked from the README for contributors, but not part of the installed bundle.
SOURCE_ONLY_DOCUMENTS = {"CONTRIBUTING.md", "AGENTS.md"}


class PackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "source"
        self.root.mkdir()
        self.output = Path(self.temp.name) / "output"
        for relative in BUILD.BUNDLE_FILES + BUILD.SKILL_FILES:
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            original = PROJECT_ROOT / relative
            destination.write_bytes(original.read_bytes() if original.is_file() else b"test fixture\n")
        (self.root / "plugin.json").write_bytes((PROJECT_ROOT / "plugin.json").read_bytes())
        source = self.root / "src" / "canvas_reader"
        source.mkdir(parents=True)
        (source / "__init__.py").write_text("")
        (source / "server.py").write_text("def main():\n    pass\n")

    def test_bundle_contains_runtime_and_no_connection_wiring(self) -> None:
        archive = BUILD.build_plugin(self.root, self.output)
        self.assertEqual(archive.name, "canvas-reader-" + json.loads((self.root / "plugin.json").read_text())["version"] + ".zip")
        with zipfile.ZipFile(archive) as handle:
            names = set(handle.namelist())
            self.assertTrue({"scripts/start.sh", "scripts/versions.py", "docs/SETUP.md", "CHANGELOG.md", "SECURITY.md",
                             "constraints.txt", "src/canvas_reader/server.py", "skills/canvas-reader/SKILL.md"}.issubset(names))
            for absent in ("mcp.json", ".mcp.json", ".app.json", "config/local-mcp.json", "scripts/local-backup.sh",
                           "docs/USAGE.md", "docs/TUNNEL.md", "docs/WORKFLOW.md", "assets/icon.png"):
                self.assertNotIn(absent, names)
            self.assertFalse(any(name.startswith(("tests/", ".claude-plugin/", ".agents/", ".github/")) for name in names))
            self.assertNotIn("apps", json.loads(handle.read("plugin.json"))["extensions"]["com.openai"])

    def test_only_the_unified_target_exists(self) -> None:
        for target in ("portable", "openai", "workflow"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                BUILD.build_plugin(self.root, self.output, target)
        self.assertFalse(self.output.exists())
        # Older copies of versions.py call package_files(root, "unified").
        files, _ = BUILD.package_files(self.root, "unified")
        self.assertIn("plugin.json", files)

    def test_unified_excludes_unlisted_private_state(self) -> None:
        secret = secrets.token_bytes(32)
        for relative in (".env", ".app.json", ".tools/tunnel-client", ".cache/private.py",
                         "PLAN.md", "AGENTS.md", "CLAUDE.md", ".agents/local-notes.md",
                         "config/personal-mcp.json"):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(secret)
        archive = BUILD.build_plugin(self.root, self.output)
        with zipfile.ZipFile(archive) as handle:
            for name in handle.namelist():
                self.assertNotIn(secret, handle.read(name))

    def test_private_state_and_caches_are_excluded(self) -> None:
        secret = secrets.token_bytes(32)
        for relative in (
            ".env", ".env.backup", ".app.json", ".venv/token.txt",
            "src/canvas_reader/__pycache__/secret.py", "tests/__pycache__/secret.py",
            "tests/notes.py", "tests/private.env",
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(secret)
        archive = BUILD.build_plugin(self.root, self.output)
        with zipfile.ZipFile(archive) as handle:
            for name in handle.namelist():
                self.assertNotIn(secret, handle.read(name))
                self.assertFalse(name.startswith((".venv/", "__pycache__/")))
            self.assertNotIn(".env", handle.namelist())
            self.assertNotIn("tests/notes.py", handle.namelist())
            self.assertNotIn("tests/private.env", handle.namelist())

    def test_filled_example_token_is_rejected(self) -> None:
        (self.root / ".env.example").write_text("CANVAS_API_TOKEN=filled-token\n")
        with self.assertRaisesRegex(ValueError, "blank CANVAS_API_TOKEN"):
            BUILD.build_plugin(self.root, self.output)

    def test_public_source_rejects_private_configuration_without_echoing_it(self) -> None:
        private_values = (
            "/" + "Users" + "/student/private/canvas.env",
            "/" + "home" + "/student/private/canvas.env",
            "C:" + "\\Users" + "\\student\\canvas.env",
            "tunnel_" + "a" * 32,
            TEST_APP_ID,
            "sk-" + "a" * 24,
            "CANVAS_API_TOKEN=" + "example-token",
            "CONTROL_PLANE_API_KEY='" + "example-key'",
        )
        for value in private_values:
            with self.subTest(value_type=value.split("=", 1)[0][:12]):
                (self.root / "README.md").write_text(value + "\n")
                with self.assertRaises(ValueError) as raised:
                    BUILD.build_plugin(self.root, self.output)
                self.assertNotIn(value, str(raised.exception))
                self.assertIn("README.md", str(raised.exception))
        self.assertFalse(self.output.exists())

    def test_public_source_keeps_author_information(self) -> None:
        manifest = json.loads((self.root / "plugin.json").read_text())
        manifest["author"] = {"name": "Example Author", "email": "author@example.com"}
        (self.root / "plugin.json").write_text(json.dumps(manifest))
        (self.root / "LICENSE").write_text("Copyright Example Author <author@example.com>\n")
        files, _ = BUILD.package_files(self.root)
        self.assertEqual(json.loads(files["plugin.json"])["author"], manifest["author"])
        self.assertIn(b"author@example.com", files["LICENSE"])

    def test_public_source_allows_runtime_secret_references(self) -> None:
        BUILD._check_public_data("scripts/example.sh", (
            'export CONTROL_PLANE_API_KEY=$runtime_secret\n'
            'CANVAS_API_TOKEN="${token_from_secure_storage}"\n'
            'CANVAS_API_TOKEN=\nTIMEZONE=UTC\n'
        ).encode())
        BUILD._check_public_data("scripts/example.py", (
            '    CANVAS_API_TOKEN="check-only", PYTHONDONTWRITEBYTECODE="1")\n'
        ).encode())

    def test_png_provenance_is_rejected(self) -> None:
        image = self.root / "assets" / "icon-light.png"
        original = image.read_bytes()
        for kind in (b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"caBX"):
            payload = b"private provenance"
            chunk = struct.pack(">I", len(payload)) + kind + payload
            chunk += struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
            image.write_bytes(original[:8] + chunk + original[8:])
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "PNG metadata"):
                BUILD.build_plugin(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_public_zip_has_fixed_dates_and_no_filesystem_metadata(self) -> None:
        archive = BUILD.build_plugin(self.root, self.output)
        with zipfile.ZipFile(archive) as handle:
            self.assertEqual(handle.comment, b"")
            for entry in handle.infolist():
                self.assertEqual(entry.date_time, (1980, 1, 1, 0, 0, 0))
                self.assertEqual(entry.extra, b"")
                self.assertEqual(entry.comment, b"")
                self.assertNotIn("__MACOSX", entry.filename)

    def test_unified_bundle_keeps_relative_document_links(self) -> None:
        files, _ = BUILD.package_files(PROJECT_ROOT)
        for name, data in files.items():
            if not name.endswith(".md"):
                continue
            for target in re.findall(r"\]\(([^\s)]+)(?:\s+[^)]*)?\)", data.decode()):
                target = target.split("#", 1)[0]
                if not target or re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", target):
                    continue
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
                if resolved in SOURCE_ONLY_DOCUMENTS:
                    self.assertTrue((PROJECT_ROOT / resolved).is_file(), f"{name} links to a missing file: {target}")
                    continue
                self.assertIn(resolved, files, f"{name} links to a missing release file: {target}")

    def test_symlink_source_is_rejected(self) -> None:
        outside = Path(self.temp.name) / "private.py"
        outside.write_text("secret = 'not distributed'\n")
        (self.root / "src/canvas_reader/link.py").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            BUILD.build_plugin(self.root, self.output)

    def test_icons_are_original_and_wired_everywhere(self) -> None:
        # Only the original artwork: no Canvas emblem or other logo files.
        self.assertEqual(sorted(path.name for path in (PROJECT_ROOT / "assets").iterdir() if path.name != ".DS_Store"),
                         ["icon-dark.png", "icon-dark.svg", "icon-light.png", "icon.svg"])
        interface = json.loads((PROJECT_ROOT / "plugin.json").read_text())["extensions"]["com.openai"]["interface"]
        expected = {"logo": "icon-light.png", "composerIcon": "icon-light.png",
                    "logoDark": "icon-dark.png", "composerIconDark": "icon-dark.png"}
        for field, name in expected.items():
            self.assertEqual(interface[field], f"./assets/{name}", field)
        for name in ("icon-light.png", "icon-dark.png"):
            data = (PROJECT_ROOT / "assets" / name).read_bytes()
            self.assertEqual(struct.unpack(">II", data[16:24]), (512, 512), name)
            BUILD._check_public_data(f"assets/{name}", data)  # no metadata chunks
        skill = PROJECT_ROOT / "skills" / "canvas-reader"
        icons = dict(re.findall(r'^\s*(icon_small|icon_large):\s*"([^"]+)"', (skill / "agents" / "openai.yaml").read_text(), re.M))
        self.assertEqual(icons, {"icon_small": "./assets/icon.svg", "icon_large": "./assets/icon.png"})
        # The skill carries its own copies so it works when linked or copied on its own.
        self.assertEqual((skill / "assets" / "icon.svg").read_bytes(), (PROJECT_ROOT / "assets" / "icon.svg").read_bytes())
        self.assertEqual((skill / "assets" / "icon.png").read_bytes(), (PROJECT_ROOT / "assets" / "icon-light.png").read_bytes())
        # A Canvas-red accent is fine; the Canvas emblem is not. It is built from a ring of
        # segments and dots, so the artwork uses no circles, ellipses, arcs or brand text.
        for name in ("icon.svg", "icon-dark.svg"):
            artwork = re.sub(r"<title>.*?</title>|<!--.*?-->", "", (PROJECT_ROOT / "assets" / name).read_text(), flags=re.S)
            with self.subTest(icon=name):
                self.assertIsNone(re.search(r"<(circle|ellipse|text|image)\b", artwork))
                self.assertIsNone(re.search(r"\bd=\"[^\"]*[AaCcQqSsTt]", artwork))  # straight lines only
                self.assertNotRegex(artwork.lower(), r"canvas|instructure")

    def test_unified_bundle_from_this_checkout_ships_every_module(self) -> None:
        files, version = BUILD.package_files(PROJECT_ROOT)
        self.assertEqual(version, json.loads((PROJECT_ROOT / "plugin.json").read_text())["version"])
        for module in ("__init__", "assignments", "common", "documents", "extract", "server"):
            self.assertIn(f"src/canvas_reader/{module}.py", files)
        self.assertFalse(any(name.startswith(("tests/", ".venv/", ".tools/")) for name in files))


if __name__ == "__main__":
    unittest.main()
