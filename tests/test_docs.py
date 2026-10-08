"""Documentation: every relative link and #anchor resolves, and no doc points at removed files."""

from __future__ import annotations

from pathlib import Path
import re
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKIPPED = {".git", ".venv", ".tools", ".cache", "dist", "node_modules"}
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")


def documents() -> list[Path]:
    found = []
    for path in sorted(PROJECT_ROOT.rglob("*.md")):
        parts = path.relative_to(PROJECT_ROOT).parts
        if not SKIPPED.intersection(parts) and parts[0] != "PLAN.md":
            found.append(path)
    return found


def anchors(path: Path) -> set[str]:
    """GitHub-style heading anchors."""
    text = re.sub(r"```.*?```", "", path.read_text(), flags=re.S)
    slugs = set()
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*$", text, re.M):
        slug = re.sub(r"[^\w\- ]", "", heading.strip().lower().replace("`", "")).replace(" ", "-")
        slugs.add(slug)
    return slugs


class DocumentationTests(unittest.TestCase):
    def test_relative_links_and_anchors_resolve(self):
        docs = documents()
        self.assertTrue(any(path.name == "README.md" for path in docs))
        for path in docs:
            text = re.sub(r"```.*?```", "", path.read_text(), flags=re.S)
            for target in LINK.findall(text):
                if re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", target):
                    continue  # external
                file_part, _, anchor = target.partition("#")
                destination = (path.parent / file_part).resolve() if file_part else path
                with self.subTest(document=str(path.relative_to(PROJECT_ROOT)), link=target):
                    self.assertTrue(destination.exists(), "missing file")
                    if anchor:
                        self.assertIn(anchor, anchors(destination), "missing heading")

    def test_only_the_real_instruction_files_use_their_names(self):
        # macOS ignores filename case, so any claude.md or agents.md is read by coding agents
        # as instructions, and versions.py leaves such files out of copies and source snapshots.
        expected = {"AGENTS.md", ".claude/CLAUDE.md"}
        found = set()
        for path in PROJECT_ROOT.rglob("*"):
            relative = path.relative_to(PROJECT_ROOT)
            if path.is_file() and not SKIPPED.intersection(relative.parts) \
                    and path.name.casefold() in {"claude.md", "agents.md"}:
                found.add(relative.as_posix())
        self.assertEqual(found, expected)

    def test_docs_do_not_point_at_removed_files(self):
        removed = ("docs/USAGE.md", "docs/TUNNEL.md", "docs/LOCAL-INSTALL.md", "docs/WORKFLOW.md",
                   "docs/REGISTRATION.md", "docs/archive", "local-backup.sh", "config/local-mcp.json",
                   "canvas-reader-local", "icon-clear.png")
        for path in documents():
            if path.name == "CHANGELOG.md":
                continue  # history may name what was removed
            text = path.read_text()
            for name in removed:
                with self.subTest(document=str(path.relative_to(PROJECT_ROOT)), name=name):
                    self.assertNotIn(name, text)


if __name__ == "__main__":
    unittest.main()
