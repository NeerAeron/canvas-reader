"""Client wiring: the Claude Code plugin, its marketplace and the Claude Desktop extension."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest
import zipfile

from canvas_reader import __version__

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_plugin", PROJECT_ROOT / "scripts" / "build_plugin.py")
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)
REPOSITORY = "https://github.com/NeerAeron/canvas-reader"


def load(relative: str) -> dict:
    return json.loads((PROJECT_ROOT / relative).read_text())


class ClaudePluginTests(unittest.TestCase):
    def test_manifest_identity_matches_the_runtime(self):
        plugin = load(".claude-plugin/plugin.json")
        self.assertEqual(plugin["name"], "canvas-reader")
        self.assertEqual(plugin["version"], __version__)
        self.assertEqual(plugin["author"], {"name": "Neer Aeron"})
        self.assertEqual(plugin["license"], "MIT")
        self.assertEqual((plugin["homepage"], plugin["repository"]), (REPOSITORY, REPOSITORY))
        # displayName is rejected by older Claude Code releases; the plain name reads well.
        self.assertNotIn("displayName", plugin)

    def test_server_is_inline_and_starts_the_installed_runtime(self):
        plugin = load(".claude-plugin/plugin.json")
        self.assertEqual(list(plugin["mcpServers"]), ["canvas-reader"])
        server = plugin["mcpServers"]["canvas-reader"]
        self.assertEqual(server["command"], "${CLAUDE_PLUGIN_ROOT}/scripts/launch.sh")
        self.assertEqual(server["env"], {
            "CANVAS_API_URL": "${user_config.canvas_url}",
            "CANVAS_API_TOKEN": "${user_config.canvas_token}",
            "TIMEZONE": "${user_config.timezone}",
        })
        referenced = set(re.findall(r"\$\{user_config\.(\w+)\}", json.dumps(server)))
        self.assertEqual(referenced, set(plugin["userConfig"]))
        for option in plugin["userConfig"].values():
            self.assertTrue({"type", "title", "description"} <= set(option))
            self.assertLessEqual(set(option), {"type", "title", "description", "sensitive", "required", "default"})
            self.assertFalse(option.get("required", False))  # the credential file is a valid alternative
        self.assertIs(plugin["userConfig"]["canvas_token"]["sensitive"], True)
        # A root .mcp.json would also start as a project server in anyone's checkout; bin/ blocks
        # installation on claude.ai and Cowork; a root CLAUDE.md is not loaded from a plugin.
        for absent in (".mcp.json", "bin", "CLAUDE.md"):
            self.assertFalse((PROJECT_ROOT / absent).exists(), absent)
        self.assertIn("@../AGENTS.md", (PROJECT_ROOT / ".claude" / "CLAUDE.md").read_text())

    def test_marketplace_lists_this_repository_as_the_plugin(self):
        marketplace = load(".claude-plugin/marketplace.json")
        self.assertEqual(marketplace["name"], "canvas-reader")
        self.assertEqual(marketplace["owner"]["name"], "Neer Aeron")
        self.assertTrue(marketplace["metadata"]["description"])
        self.assertNotIn("description", marketplace)  # older Claude Code rejects the top-level key
        self.assertEqual([(entry["name"], entry["source"]) for entry in marketplace["plugins"]],
                         [("canvas-reader", "./")])


class CodexPluginTests(unittest.TestCase):
    def test_codex_marketplace_lists_the_skill_only_plugin(self):
        marketplace = load(".agents/plugins/marketplace.json")
        self.assertEqual(marketplace["name"], "canvas-reader")
        self.assertEqual(marketplace["plugins"], [{
            "name": "canvas-reader", "source": {"source": "local", "path": "./"},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, "category": "Productivity"}])
        # Codex reads the root portable manifest first (before .claude-plugin/plugin.json). It
        # must not start a server: Codex connects with `codex mcp add` instead.
        portable = load("plugin.json")
        self.assertEqual(portable["version"], __version__)
        self.assertNotIn("mcpServers", portable)
        self.assertNotIn("mcpServers", portable["extensions"]["com.openai"])
        self.assertFalse((PROJECT_ROOT / "mcp.json").exists())
        self.assertEqual((portable["homepage"], portable["repository"]), (REPOSITORY, REPOSITORY))

    def test_skill_meets_the_agent_skills_specification(self):
        skill = PROJECT_ROOT / "skills" / "canvas-reader"
        text = (skill / "SKILL.md").read_text()
        front = re.match(r"---\n(.*?)\n---\n", text, re.S).group(1)
        fields = dict(re.findall(r"^(\w[\w-]*):\s*(.+)$", front, re.M))
        self.assertEqual(fields["name"], skill.name)
        self.assertRegex(fields["name"], r"^(?!-)(?!.*--)[a-z0-9-]{1,64}(?<!-)$")
        self.assertTrue(0 < len(fields["description"]) <= 1024)
        self.assertLess(len(text.splitlines()), 500)


class DesktopExtensionTests(unittest.TestCase):
    def test_mcpb_runs_the_launcher_with_the_plugin_settings(self):
        files, version = BUILD.mcpb_files(PROJECT_ROOT)
        self.assertEqual(set(files), {"manifest.json", "server/launch.sh", "icon.png"})
        self.assertEqual(files["server/launch.sh"], (PROJECT_ROOT / "scripts" / "launch.sh").read_bytes())
        self.assertEqual(files["icon.png"], (PROJECT_ROOT / "assets" / "icon-light.png").read_bytes())
        manifest = json.loads(files["manifest.json"])
        plugin = load(".claude-plugin/plugin.json")
        self.assertEqual((manifest["manifest_version"], manifest["name"], manifest["version"], version),
                         ("0.3", "canvas-reader", __version__, __version__))
        self.assertEqual(manifest["server"]["type"], "binary")
        self.assertEqual(manifest["server"]["entry_point"], "server/launch.sh")
        self.assertEqual(manifest["server"]["mcp_config"], {
            "command": "/bin/sh", "args": ["${__dirname}/server/launch.sh"],
            "env": plugin["mcpServers"]["canvas-reader"]["env"]})
        self.assertEqual(manifest["user_config"], plugin["userConfig"])
        self.assertEqual(manifest["compatibility"], {"platforms": ["darwin"]})

    def test_check_dist_detects_missing_and_stale_release_files(self):
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory)
            self.assertEqual(BUILD.stale_release_files(PROJECT_ROOT, dist),
                             [f"canvas-reader-{__version__}.zip", f"canvas-reader-{__version__}.mcpb"])
            BUILD.build_plugin(PROJECT_ROOT, dist)
            BUILD.build_mcpb(PROJECT_ROOT, dist)
            self.assertEqual(BUILD.stale_release_files(PROJECT_ROOT, dist), [])
            # Same contents compressed differently still match; changed contents don't.
            zip_path = dist / f"canvas-reader-{__version__}.zip"
            entries = BUILD.archive_contents(zip_path)
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as handle:
                for name, (mode, data) in entries.items():
                    info = zipfile.ZipInfo(name)
                    info.external_attr = mode << 16
                    handle.writestr(info, data if name != "README.md" else data + b"\nchanged\n")
            self.assertEqual(BUILD.stale_release_files(PROJECT_ROOT, dist), [zip_path.name])

    def test_release_keeps_history_and_writes_versioned_checksums(self):
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory)
            history = ("canvas-reader-0.3.3.zip", "canvas-reader-0.3.3.mcpb", "SHA256SUMS-0.3.3",
                       "canvas-reader-0.3.3-source.zip", "notes.txt")
            for name in (*history, "SHA256SUMS"):
                (dist / name).write_text("old")
            zip_name, mcpb_name, sums_name = (f"canvas-reader-{__version__}.zip", f"canvas-reader-{__version__}.mcpb",
                                              f"SHA256SUMS-{__version__}")
            for attempt in range(2):  # rebuilding the same version replaces only its own files
                written = BUILD.write_release(PROJECT_ROOT, dist)
                self.assertEqual([path.name for path in written], [zip_name, mcpb_name, sums_name])
                # Earlier releases and source snapshots stay; only the unversioned checksum file goes.
                self.assertEqual(sorted(path.name for path in dist.iterdir()),
                                 sorted([*history, zip_name, mcpb_name, sums_name]))
                for name in history:
                    self.assertEqual((dist / name).read_text(), "old")
            # The same "<sha256>  <name>" lines that sha256sum writes and versions.py --github reads.
            self.assertEqual((dist / sums_name).read_text(), "".join(
                f"{hashlib.sha256((dist / name).read_bytes()).hexdigest()}  {name}\n" for name in (zip_name, mcpb_name)))
            self.assertEqual(BUILD.stale_release_files(PROJECT_ROOT, dist), [])

    def test_committed_dist_keeps_every_release_with_checksums(self):
        dist = PROJECT_ROOT / "dist"
        names = {path.name for path in dist.iterdir() if path.is_file()}
        self.assertNotIn("SHA256SUMS", names)  # checksums are per version
        releases = {name: match.group(1) for name in names
                    if (match := re.fullmatch(r"canvas-reader-(\d+\.\d+\.\d+)\.(?:zip|mcpb)", name))}
        self.assertTrue(releases)
        listed = {}
        for name in names:
            if name.startswith("SHA256SUMS-"):
                for line in (dist / name).read_text().splitlines():
                    digest, listed_name = line.split()
                    listed[listed_name] = (name, digest)
        for name, version in releases.items():
            with self.subTest(release=name):
                self.assertEqual(listed[name][0], f"SHA256SUMS-{version}")
                self.assertEqual(listed[name][1], hashlib.sha256((dist / name).read_bytes()).hexdigest())
                with zipfile.ZipFile(dist / name) as handle:
                    manifest = "plugin.json" if name.endswith(".zip") else "manifest.json"
                    self.assertEqual(json.loads(handle.read(manifest))["version"], version)
        self.assertLessEqual(set(listed), names)  # every checksum names a file that is here

    def test_mcpb_archive_is_reproducible(self):
        with tempfile.TemporaryDirectory() as directory:
            first = BUILD.build_mcpb(PROJECT_ROOT, Path(directory) / "a").read_bytes()
            second = BUILD.build_mcpb(PROJECT_ROOT, Path(directory) / "b")
            self.assertEqual(first, second.read_bytes())
            self.assertEqual(second.name, f"canvas-reader-{__version__}.mcpb")
            with zipfile.ZipFile(second) as handle:
                mode = handle.getinfo("server/launch.sh").external_attr >> 16
                self.assertTrue(mode & 0o111)


if __name__ == "__main__":
    unittest.main()
