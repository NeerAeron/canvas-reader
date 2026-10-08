#!/usr/bin/env python3
"""Build the Canvas Reader release files in dist/.

- canvas-reader-<version>.zip: the runtime bundle that versions.py installs.
- canvas-reader-<version>.mcpb: a one-click Claude Desktop extension that starts
  the installed runtime (built with --mcpb).

Only the files listed here are included. User credentials, environments and
connection state are never copied into a distribution.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import stat
import struct
import zipfile


PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
BUNDLE_FILES = (
    "README.md", "CHANGELOG.md", "LICENSE", "NOTICE", "SECURITY.md", "pyproject.toml", "constraints.txt",
    ".env.example", "docs/SETUP.md", "docs/VERIFICATION.md", "docs/clients/claude-apps.md",
    "docs/clients/chatgpt.md", "docs/clients/codex.md",
    "assets/icon-light.png", "assets/icon-dark.png",
    "scripts/start.sh", "scripts/launch.sh", "scripts/tunnel.sh", "scripts/smoke.py", "scripts/versions.py",
)
SKILL_FILES = (
    "skills/canvas-reader/SKILL.md",
    "skills/canvas-reader/agents/openai.yaml",
    "skills/canvas-reader/assets/icon.svg",
    "skills/canvas-reader/assets/icon.png",
)
EXECUTABLE_FILES = {"scripts/versions.py"}
EXCLUDED_PARTS = {
    ".git", ".venv", ".tools", ".cache", "venv", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".agents", "build", "dist", ".DS_Store",
}
ARCHIVE_DATE = (1980, 1, 1, 0, 0, 0)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Colour information is retained; text, EXIF and C2PA provenance are not.
PNG_CHUNKS = {b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND", b"sRGB", b"gAMA", b"cHRM"}
PUBLIC_PATTERNS = (
    ("personal home path", re.compile(
        r"(?:" + "/" + "Users/|" + "/" + r"home/)[^/\s<>\"']+/|[A-Za-z]:\\Users\\[^\\\s<>\"']+\\")),
    ("account connection ID", re.compile(r"\b(?:tunnel_|(?:plugin_)?asdk_app_)[a-fA-F0-9]{32}\b")),
    ("API key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b")),
    ("filled credential assignment", re.compile(
        r"(?m)^[ \t]*(?:export[ \t]+)?(?:CANVAS_API_TOKEN|CONTROL_PLANE_API_KEY)[ \t]*=[ \t]*"
        r"(?:\"[^\"$\r\n]+\"|'[^'$\r\n]+'|[^\"'\s#$][^\s\r\n]*)[ \t]*(?:#.*)?$")),
)


def _check_public_data(relative: str, data: bytes) -> None:
    """Catch common private configuration and image provenance before export."""
    if relative.endswith(".png"):
        if not data.startswith(PNG_SIGNATURE):
            raise ValueError(f"Invalid PNG asset: {relative}")
        offset = len(PNG_SIGNATURE)
        while offset < len(data):
            if offset + 12 > len(data):
                raise ValueError(f"Invalid PNG asset: {relative}")
            length = struct.unpack_from(">I", data, offset)[0]
            kind = data[offset + 4:offset + 8]
            if offset + length + 12 > len(data):
                raise ValueError(f"Invalid PNG asset: {relative}")
            if kind not in PNG_CHUNKS:
                raise ValueError(f"Remove PNG metadata before publication: {relative}")
            offset += length + 12
            if kind == b"IEND":
                if offset != len(data):
                    raise ValueError(f"Remove trailing PNG data before publication: {relative}")
                return
        raise ValueError(f"Invalid PNG asset: {relative}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"Expected UTF-8 source file: {relative}") from None
    for reason, pattern in PUBLIC_PATTERNS:
        if pattern.search(text):
            # Do not echo the matched path, identifier or credential.
            raise ValueError(f"Remove {reason} before publication: {relative}")


def _read_source(root: Path, relative: str) -> bytes:
    path = root / relative
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError(f"Refusing to package symlink: {relative}")
    if not path.is_file():
        raise ValueError(f"Required package file is missing: {relative}")
    data = path.read_bytes()
    if relative == ".env.example":
        _check_template(data)
    _check_public_data(relative, data)
    return data


def _python_sources(root: Path) -> dict[str, bytes]:
    source = root / "src" / "canvas_reader"
    if source.is_symlink() or not source.is_dir():
        raise ValueError("src/canvas_reader must be a regular source directory")
    files: dict[str, bytes] = {}
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(root)
        if any(part in EXCLUDED_PARTS or part.endswith(".egg-info") for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"Refusing to package symlink: {relative.as_posix()}")
        if path.is_file() and path.suffix == ".py":
            files[relative.as_posix()] = _read_source(root, relative.as_posix())
    for required in ("src/canvas_reader/__init__.py", "src/canvas_reader/server.py"):
        if required not in files:
            raise ValueError(f"Required package file is missing: {required}")
    return files


def _check_template(data: bytes) -> None:
    """Never export a filled-in token through the example configuration."""
    for line in data.decode("utf-8").splitlines():
        match = re.match(r"\s*(?:export\s+)?CANVAS_API_TOKEN\s*=(.*)", line)
        if match and match.group(1).strip().strip("\"'"):
            raise ValueError(".env.example must contain a blank CANVAS_API_TOKEN")


def package_files(root: Path, target: str = "unified") -> tuple[dict[str, bytes], str]:
    """Return validated, allowlisted archive contents and the plugin version.

    ``target`` is kept for older copies of versions.py, which pass "unified".
    """
    root = root.resolve()
    if target != "unified":
        raise ValueError("The only bundle target is unified")
    manifest = json.loads(_read_source(root, "plugin.json"))
    if manifest.get("$schema") != PLUGIN_SCHEMA or manifest.get("name") != "canvas-reader":
        raise ValueError("plugin.json must declare the portable Canvas Reader manifest")
    version = manifest.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version):
        raise ValueError("plugin.json must contain a valid version")
    manifest = copy.deepcopy(manifest)
    extension = manifest.setdefault("extensions", {}).setdefault("com.openai", {})
    # Account-specific connection wiring never ships in the bundle.
    extension.pop("apps", None)
    extension.pop("mcpServers", None)
    extension.pop("skills", None)

    files = {relative: _read_source(root, relative) for relative in BUNDLE_FILES + SKILL_FILES}
    _check_template(files[".env.example"])
    files.update(_python_sources(root))
    files["plugin.json"] = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    return files, version


def build_plugin(root: Path, output_dir: Path, target: str = "unified") -> Path:
    """Build a ZIP with one plugin root at the archive root."""
    files, version = package_files(root, target)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"canvas-reader-{version}.zip"
    _write_zip(archive, files)
    return archive


def mcpb_files(root: Path) -> tuple[dict[str, bytes], str]:
    """A Claude Desktop extension (MCP Bundle, manifest 0.3) that runs the installed runtime.

    Its metadata, settings and environment come from .claude-plugin/plugin.json,
    so the Claude Code plugin and the Desktop extension always agree.
    """
    root = root.resolve()
    plugin = json.loads(_read_source(root, ".claude-plugin/plugin.json"))
    _, version = package_files(root)
    if plugin.get("version") != version:
        raise ValueError(".claude-plugin/plugin.json and plugin.json disagree about the version")
    server = plugin["mcpServers"]["canvas-reader"]
    homepage = plugin["homepage"]
    manifest = {
        "manifest_version": "0.3",
        "name": "canvas-reader",
        "display_name": "Canvas Reader",
        "version": version,
        "description": plugin["description"],
        "long_description": (
            "Canvas Reader gives Claude read-only access to your own Canvas account. It starts the Canvas "
            "Reader runtime installed on this Mac with scripts/versions.py, so install that first: "
            f"{homepage}/blob/main/docs/SETUP.md. Leave the settings blank to use "
            "~/.config/canvas-reader/canvas.env."),
        "author": plugin["author"],
        "repository": {"type": "git", "url": plugin["repository"]},
        "homepage": homepage,
        "documentation": f"{homepage}/blob/main/docs/clients/claude-apps.md",
        "support": f"{homepage}/issues",
        "license": plugin["license"],
        "keywords": plugin["keywords"],
        "icon": "icon.png",
        "server": {
            "type": "binary",
            "entry_point": "server/launch.sh",
            # Run through sh so the launcher works even if extraction drops the executable bit.
            "mcp_config": {"command": "/bin/sh", "args": ["${__dirname}/server/launch.sh"], "env": server["env"]},
        },
        "compatibility": {"platforms": ["darwin"]},
        "user_config": plugin["userConfig"],
    }
    files = {
        "manifest.json": (json.dumps(manifest, indent=2) + "\n").encode("utf-8"),
        "server/launch.sh": _read_source(root, "scripts/launch.sh"),
        "icon.png": _read_source(root, "assets/icon-light.png"),
    }
    return files, version


def _write_zip(archive: Path, files: dict[str, bytes]) -> None:
    if archive.is_symlink():
        raise ValueError("Refusing to overwrite a symlink archive")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for relative, data in sorted(files.items()):
            info = zipfile.ZipInfo(relative, date_time=ARCHIVE_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            mode = 0o755 if relative in EXECUTABLE_FILES or relative.endswith(".sh") else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            handle.writestr(info, data)


def build_mcpb(root: Path, output_dir: Path) -> Path:
    files, version = mcpb_files(root)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"canvas-reader-{version}.mcpb"
    _write_zip(archive, files)
    return archive


def write_release(root: Path, dist: Path) -> list[Path]:
    """Add this version's release files to ``dist``: its zip, .mcpb and SHA256SUMS-<version>.

    Earlier releases and the source snapshots versions.py bump saves stay, so dist/ keeps the
    whole release history. Rebuilding the same version replaces only that version's files.
    """
    _, version = package_files(root)
    dist.mkdir(parents=True, exist_ok=True)
    legacy = dist / "SHA256SUMS"  # one unversioned file, used briefly before 0.4.0 was published
    if legacy.is_file():
        legacy.unlink()
    archives = [build_plugin(root, dist), build_mcpb(root, dist)]
    sums = dist / f"SHA256SUMS-{version}"
    sums.write_text("".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in archives))
    return [*archives, sums]


def archive_contents(archive: Path) -> dict[str, tuple[int, bytes]]:
    """Each entry's file mode and bytes; two archives match when these match."""
    with zipfile.ZipFile(archive) as handle:
        return {info.filename: (info.external_attr >> 16, handle.read(info)) for info in handle.infolist()}


def stale_release_files(root: Path, dist: Path) -> list[str]:
    """Names of committed release files in ``dist`` that differ from a fresh build (or are missing)."""
    files, version = package_files(root)
    expected = {f"canvas-reader-{version}.zip": files, f"canvas-reader-{version}.mcpb": mcpb_files(root)[0]}
    stale = []
    for name, wanted in expected.items():
        path = dist / name
        if not path.is_file():
            stale.append(name)
            continue
        committed = archive_contents(path)
        modes = {relative: (0o100755 if relative in EXECUTABLE_FILES or relative.endswith(".sh") else 0o100644)
                 for relative in wanted}
        if committed != {relative: (modes[relative], data) for relative, data in wanted.items()}:
            stale.append(name)
    return stale


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    # Accepted so older build commands keep working; unified is the only bundle.
    parser.add_argument("--target", choices=("unified",), default="unified", help=argparse.SUPPRESS)
    parser.add_argument("--mcpb", action="store_true", help="Also build the Claude Desktop extension (.mcpb)")
    parser.add_argument("--release", action="store_true",
                        help="Add this version's zip, .mcpb and SHA256SUMS-<version> to dist/ (earlier releases stay)")
    parser.add_argument("--check-dist", action="store_true",
                        help="Check that dist/ holds this version's zip and .mcpb with the current contents")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[1] / "dist")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.check_dist:
        try:
            stale = stale_release_files(root, args.output_dir)
        except (ValueError, KeyError, OSError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            parser.error(str(exc))
        if stale:
            parser.exit(1, f"Out of date in {args.output_dir}: {', '.join(stale)}. Rebuild with "
                           "scripts/build_plugin.py --mcpb and commit dist/.\n")
        print(f"{args.output_dir} matches this version's release files.")
        return
    try:
        if args.release:
            archives = write_release(root, args.output_dir)
        else:
            archives = [build_plugin(root, args.output_dir)]
            if args.mcpb:
                archives.append(build_mcpb(root, args.output_dir))
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    for archive in archives:
        print(archive)


if __name__ == "__main__":
    main()
