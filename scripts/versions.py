#!/usr/bin/env python3
"""Install, update, switch and remove Canvas Reader versions on this Mac.

Each version gets its own folder and Python environment, and one link says
which version is active:

    ~/.local/share/canvas-reader/
        versions/0.3.0/        a complete, verified copy
        versions/0.3.1/
        current -> versions/0.3.1    what the tunnel (and Codex) run
        state.json             the previous version and a short history

A new version is installed and checked on the side; it becomes active only
when it starts correctly, and a failed or interrupted install leaves nothing
behind. Switching back is instant. Only the Python standard library is used;
Python 3.9 or later runs the version manager.

Run "scripts/versions.py --help" for the commands; docs/SETUP.md explains them.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import ssl
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\Z")
RELEASE_ZIP = re.compile(r"canvas-reader-(\d+\.\d+\.\d+)\.zip\Z")
MAX_ZIP_ENTRIES = 2_000
MAX_ZIP_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
CHECK_SECONDS = 180
INSTALL_SECONDS = 900
LIVE_SECONDS = 300
HISTORY_LIMIT = 20
TAIL_LINES = 15
REQUIRED_FILES = ("plugin.json", "pyproject.toml", "src/canvas_reader/__init__.py",
                  "src/canvas_reader/server.py", "scripts/start.sh")
# Never copied from a folder: environments, tools, caches, build output, and anything credential-like
# or account-specific (tunnel profiles and app IDs, as in .gitignore).
SKIPPED_NAMES = {".git", ".venv", "venv", ".tools", ".cache", "dist", "build", "__pycache__", ".pytest_cache",
                 ".ruff_cache", ".mypy_cache", "node_modules", ".DS_Store", ".idea", ".vscode", "__MACOSX",
                 ".tunnel", ".app.json", ".agents"}
DUMMY_CANVAS_URL = "https://example.instructure.com/api/v1"
GITHUB_REPOSITORY = "NeerAeron/canvas-reader"
GITHUB_API = "https://api.github.com"
DOWNLOAD_SECONDS = 60
CHECKSUMS = "SHA256SUMS"
TOKEN_SERVICE = "canvas-reader-canvas-token"  # the same Keychain item the server reads
RESTART_HINT = ("Restart what runs the reader to use it: the tunnel (Control-C, then scripts/tunnel.sh --run), "
                "a new Claude Code or Codex session, or quit and reopen Claude Desktop.")
TUNNEL_RESTART_HINT = "Restart the tunnel to use it: press Control-C where it runs, then run scripts/tunnel.sh --run again."


class Failure(Exception):
    """A problem the user can fix; the message says how."""


class StepFailed(Failure):
    def __init__(self, step: str, reason: str, output: str = ""):
        super().__init__(f"{step} failed ({reason})")
        self.step, self.reason, self.output = step, reason, output


# --------------------------------------------------------------------------
# Locations and small helpers
# --------------------------------------------------------------------------

class Home:
    """The managed folder: versions, the current link, state and a lock."""

    def __init__(self, root: Path):
        self.root = root
        self.versions = root / "versions"
        self.current = root / "current"
        self.state_file = root / "state.json"
        self.lock_file = root / ".lock"


def home_dir() -> Path:
    if os.environ.get("CANVAS_READER_HOME"):
        return Path(os.environ["CANVAS_READER_HOME"]).expanduser().absolute()
    data = os.environ.get("XDG_DATA_HOME", "")
    base = Path(data) if os.path.isabs(data) else Path.home() / ".local" / "share"
    return base / "canvas-reader"


def shown(path: Path | str) -> str:
    """A path for messages, with the home folder shortened to ~."""
    text, home = str(path), str(Path.home())
    return "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def version_key(version: str) -> tuple:
    match = re.match(r"(\d+)\.(\d+)\.(\d+)(.*)", version)
    if not match:
        return (-1, -1, -1, False, version)
    major, minor, patch, suffix = match.groups()
    return (int(major), int(minor), int(patch), suffix == "", suffix)  # 1.0.0 sorts after 1.0.0-rc1


def write_atomic(path: Path, text: str, mode: int | None = None) -> None:
    """Replace a file in one step, so a crash never leaves half a file."""
    path = Path(os.path.realpath(path))
    if mode is None:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def remove_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def tail(output: str) -> str:
    lines = [line for line in output.strip().splitlines() if line.strip()][-TAIL_LINES:]
    return re.sub(r"(://)[^/@\s]+@", r"\1***@", "\n".join(lines))  # never echo credentials in URLs


def clean_environment(**overrides: str) -> dict[str, str]:
    """This process's environment without credentials or Python overrides, plus ``overrides``."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("CANVAS_") and key not in {
               "CONTROL_PLANE_API_KEY", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"}}
    env.update(overrides)
    return env


def run(command: list[str], step: str, *, cwd: Path | None = None, env: dict[str, str] | None = None,
        timeout: int = CHECK_SECONDS) -> str:
    """Run a command with captured output; failures raise StepFailed with the output's last lines."""
    try:
        completed = subprocess.run(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        output = error.output.decode("utf-8", "replace") if isinstance(error.output, bytes) else ""
        raise StepFailed(step, f"no result after {timeout} seconds", output) from None
    except OSError as error:
        raise StepFailed(step, f"could not run {Path(command[0]).name}: {error.strerror}") from None
    output = completed.stdout.decode("utf-8", "replace")
    if completed.returncode != 0:
        raise StepFailed(step, f"exit status {completed.returncode}", output)
    return output


@contextlib.contextmanager
def locked(home: Home):
    """Let only one command change the managed folder at a time."""
    home.versions.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(home.lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        raise Failure("Another versions.py command is running; wait for it to finish and try again.") from None
    try:
        # Uninstall may remove this path after another command has opened it. A lock on
        # that old inode must not grant access to a newly created managed folder.
        try:
            held = os.fstat(descriptor)
            current = os.stat(home.lock_file, follow_symlinks=False)
            same_file = (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)
        except OSError:
            same_file = False
        if not same_file:
            raise Failure("The managed folder's lock changed while this command was starting; "
                          "try again. Nothing was changed.")
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


# --------------------------------------------------------------------------
# State: which versions exist, which is active, what came before
# --------------------------------------------------------------------------

def load_state(home: Home) -> dict:
    try:
        state = json.loads(home.state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(home: Home, state: dict) -> None:
    state["history"] = state.get("history", [])[-HISTORY_LIMIT:]
    write_atomic(home.state_file, json.dumps(state, indent=2) + "\n", 0o644)


def state_snapshot(home: Home) -> tuple[str, int] | None:
    """Keep the exact previous state so a failed commit can restore it."""
    if not home.state_file.exists():
        return None
    return (home.state_file.read_text(encoding="utf-8"), stat.S_IMODE(home.state_file.stat().st_mode))


def restore_state(home: Home, snapshot: tuple[str, int] | None) -> None:
    if snapshot is None:
        with contextlib.suppress(FileNotFoundError):
            home.state_file.unlink()
    elif state_snapshot(home) != snapshot:
        write_atomic(home.state_file, *snapshot)


def active_version(home: Home) -> str | None:
    try:
        target = os.readlink(home.current)
    except OSError:
        return None
    return Path(target).name or None


def restore_current(home: Home, target: str | None) -> None:
    current = os.readlink(home.current) if home.current.is_symlink() else None
    if current == target:
        return
    if target is None:
        home.current.unlink()
    else:
        temporary = home.root / f".current-{os.getpid()}"
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        os.symlink(target, temporary)
        os.replace(temporary, home.current)


def incomplete(folder: Path) -> bool:
    return (folder / ".installing").exists()


def environment_ok(folder: Path) -> bool:
    """The version's environment exists and its Python is still present (pyenv can remove it)."""
    return (folder / ".venv/bin/canvas-reader").is_file() and (folder / ".venv/bin/python").exists()


def installed_versions(home: Home) -> list[str]:
    """Complete installs, newest first."""
    if not home.versions.is_dir():
        return []
    found = [child.name for child in home.versions.iterdir()
             if child.is_dir() and not child.is_symlink() and VERSION.match(child.name) and not incomplete(child)]
    return sorted(found, key=version_key, reverse=True)


def install_info(folder: Path) -> dict:
    try:
        info = json.loads((folder / ".install.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


def clean_up_leftovers(home: Home, say=print) -> None:
    """Remove what an interrupted run left behind (called with the lock held)."""
    if not home.versions.is_dir():
        return
    for child in sorted(home.versions.iterdir()):
        if child.name.startswith(".staging-") or (child.is_dir() and incomplete(child)):
            remove_tree(child)
            if not child.name.startswith("."):
                say(f"Removed an incomplete install of {child.name} left by an interrupted run.")
    for child in sorted(home.versions.iterdir()):
        match = re.fullmatch(r"\.replaced-(.+)-\d+", child.name)
        if match:
            original = home.versions / match.group(1)
            if original.exists():
                remove_tree(child)
            else:
                os.rename(child, original)
                say(f"Restored {match.group(1)} after an interrupted reinstall.")
    for child in home.root.glob(".current-*"):
        with contextlib.suppress(OSError):
            child.unlink()


def activate(home: Home, version: str, action: str, *, last_source: str | None = None) -> str | None:
    """Point current at a version in one atomic step; returns the version it replaced."""
    folder = home.versions / version
    if not folder.is_dir() or incomplete(folder):
        raise Failure(f"Canvas Reader {version} is not installed.")
    old = active_version(home)
    if home.current.exists() and not home.current.is_symlink():
        raise Failure(f"{shown(home.current)} is not a link; move it aside and try again.")
    previous_state = state_snapshot(home)
    previous_link = os.readlink(home.current) if home.current.is_symlink() else None
    temporary = home.root / f".current-{os.getpid()}"
    state = load_state(home)
    if last_source is not None:
        state["last_source"] = last_source
    if old and old != version:
        state["previous"] = old
    state.setdefault("history", []).append({"at": now(), "action": action, "version": version, "from": old})
    try:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        os.symlink(os.path.join("versions", version), temporary)
        # Saving state first keeps current unchanged when the state file cannot be written.
        save_state(home, state)
        os.replace(temporary, home.current)
    except BaseException as error:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        restore_current(home, previous_link)
        restore_state(home, previous_state)
        if isinstance(error, OSError):
            raise Failure(f"Could not activate {version}: {error.strerror or error}. "
                          "The previous active version and version settings were restored.") from None
        raise
    return old


# --------------------------------------------------------------------------
# Reading a bundle (zip or folder)
# --------------------------------------------------------------------------

def _safe_name(name: str) -> bool:
    parts = name.split("/")
    return (bool(name) and "\\" not in name and "\x00" not in name and not name.startswith("/")
            and not re.match(r"[A-Za-z]:", name) and all(part not in ("", ".", "..") for part in parts))


def extract_zip(archive: Path, destination: Path) -> None:
    """Unpack a bundle zip after checking every entry; refuses anything unsafe."""
    try:
        bundle = zipfile.ZipFile(archive)
    except (OSError, zipfile.BadZipFile):
        raise Failure(f"{shown(archive)} is not a readable zip file.") from None
    with bundle:
        entries = [entry for entry in bundle.infolist() if not entry.filename.startswith("__MACOSX/")
                   and Path(entry.filename).name != ".DS_Store"]
        if len(entries) > MAX_ZIP_ENTRIES:
            raise Failure(f"{shown(archive)} has too many files to be a Canvas Reader bundle.")
        names = {entry.filename for entry in entries}
        # Accept both a bundle with files at the top and one wrapped in a single folder.
        tops = {name.split("/", 1)[0] for name in names}
        prefix = f"{tops.pop()}/" if len(tops) == 1 and f"{next(iter(tops))}/plugin.json" in names else ""
        total = 0
        for entry in entries:
            name = entry.filename
            if not _safe_name(name.rstrip("/")):
                raise Failure(f"{shown(archive)} contains an unsafe path ({name!r}); it was not installed.")
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise Failure(f"{shown(archive)} contains a symbolic link ({name}); it was not installed.")
            if entry.flag_bits & 0x1:
                raise Failure(f"{shown(archive)} is encrypted; use an unencrypted bundle.")
            relative = name.removeprefix(prefix)
            if entry.is_dir() or not relative:
                continue
            total += entry.file_size
            if entry.file_size > MAX_FILE_BYTES or total > MAX_ZIP_BYTES:
                raise Failure(f"{shown(archive)} is too large to be a Canvas Reader bundle.")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with bundle.open(entry) as source, open(target, "xb") as output:
                    shutil.copyfileobj(source, output)
            except FileExistsError:
                raise Failure(f"{shown(archive)} lists {relative} twice; it was not installed.") from None
            except (zipfile.BadZipFile, OSError, EOFError):
                raise Failure(f"{shown(archive)} is damaged ({relative} could not be unpacked).") from None
            os.chmod(target, 0o755 if mode & 0o111 else 0o644)


def _skipped(path: Path) -> bool:
    name = path.name
    credential = (name == ".env" or name.endswith(".env") or name.startswith(".env.")) and name != ".env.example"
    return (name in SKIPPED_NAMES or name.casefold() in {"plan.md", "agents.md", "claude.md"}
            or name.endswith((".egg-info", ".pyc", ".pem", ".key", ".p12"))
            or credential or name.startswith(".install") or re.fullmatch(r"tunnel.*\.(json|ya?ml)", name) is not None)


def source_files(root: Path):
    """Regular files under root, never entering skipped folders, as (path, relative path) pairs."""
    for folder, subfolders, files in os.walk(root):
        subfolders[:] = sorted(name for name in subfolders
                               if not _skipped(Path(name)) and not os.path.islink(os.path.join(folder, name)))
        for name in sorted(files):
            path = Path(folder) / name
            if not _skipped(path) and not path.is_symlink() and path.is_file():
                yield path, path.relative_to(root)


def copy_folder(source: Path, destination: Path) -> None:
    """Copy a source folder: a checkout through its own build allowlist, else everything but skipped names."""
    builder = source / "scripts" / "build_plugin.py"
    if builder.is_file():
        spec = importlib.util.spec_from_file_location("canvas_reader_bundle_builder", builder)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
            files, _ = module.package_files(source, "unified")
        except Exception as error:  # the builder's own checks explain what is wrong
            raise Failure(f"{shown(source)} could not be packaged: {error}") from None
        for relative, data in files.items():
            if not _safe_name(relative):
                raise Failure(f"{shown(source)} lists an unsafe path ({relative!r}).")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            original = source / relative
            executable = relative.endswith(".sh") or (original.is_file() and original.stat().st_mode & 0o111)
            os.chmod(target, 0o755 if executable else 0o644)
        return
    for folder, subfolders, files in os.walk(source):
        for name in (*subfolders, *files):
            if os.path.islink(os.path.join(folder, name)) and not _skipped(Path(name)):
                relative = Path(folder, name).relative_to(source)
                raise Failure(f"{shown(source)} contains a symbolic link ({relative}); copy the real files instead.")
        subfolders[:] = [name for name in subfolders if not _skipped(Path(name))]
    for path, relative in source_files(source):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        os.chmod(target, 0o755 if path.stat().st_mode & 0o111 else 0o644)


def bundle_version(root: Path) -> str:
    """The version a bundle declares; every place that states it must agree."""
    missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    if missing:
        raise Failure("This is not a Canvas Reader bundle (missing " + ", ".join(missing) + ").")
    try:
        manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Failure("The bundle's plugin.json cannot be read.") from None
    if not isinstance(manifest, dict) or manifest.get("name") != "canvas-reader":
        raise Failure("The bundle's plugin.json is not for canvas-reader.")
    found = {"plugin.json": manifest.get("version")}
    match = re.search(r"""^__version__\s*=\s*["']([^"']+)["']""",
                      (root / "src/canvas_reader/__init__.py").read_text(encoding="utf-8"), re.MULTILINE)
    found["src/canvas_reader/__init__.py"] = match.group(1) if match else None
    match = re.search(r'^version\s*=\s*"([^"]+)"', (root / "pyproject.toml").read_text(encoding="utf-8"),
                      re.MULTILINE)
    if match:
        found["pyproject.toml"] = match.group(1)
    values = set(found.values())
    if len(values) != 1 or None in values:
        stated = ", ".join(f"{name} says {value or 'nothing'}" for name, value in found.items())
        raise Failure(f"The bundle's version numbers disagree ({stated}); rebuild it.")
    version = values.pop()
    if not isinstance(version, str) or not VERSION.match(version):
        raise Failure(f"The bundle's version {version!r} is not a valid version number.")
    return version


def required_python(root: Path) -> tuple[int, int]:
    match = re.search(r'requires-python\s*=\s*">=\s*(\d+)\.(\d+)', (root / "pyproject.toml").read_text(encoding="utf-8"))
    return (int(match.group(1)), int(match.group(2))) if match else (3, 11)


def choose_python(requested: str | None, minimum: tuple[int, int]) -> str:
    """A Python new enough for this version: --python, $CANVAS_READER_PYTHON, then common names."""
    candidates = [requested] if requested else [
        os.environ.get("CANVAS_READER_PYTHON"), sys.executable, str(SCRIPT_ROOT / ".venv/bin/python"),
        "python3.14", "python3.13", "python3.12", "python3.11", "python3",
    ]
    seen = set()
    for candidate in filter(None, candidates):
        path = candidate if os.sep in candidate else shutil.which(candidate)
        if not path or not os.path.exists(path) or os.path.realpath(path) in seen:
            continue
        seen.add(os.path.realpath(path))
        try:
            output = subprocess.run([path, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                                    capture_output=True, text=True, timeout=30, check=False).stdout.strip()
            found = tuple(int(part) for part in output.split("."))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
        if found >= minimum:
            return path
        if requested:
            raise Failure(f"{requested} is Python {output}; Canvas Reader needs {minimum[0]}.{minimum[1]} or later.")
    if requested:
        raise Failure(f"{requested} is not a working Python.")
    raise Failure(f"No Python {minimum[0]}.{minimum[1]} or later was found. Install one (for example from "
                  "python.org) or pass --python /path/to/python3.")


# --------------------------------------------------------------------------
# Installing
# --------------------------------------------------------------------------

def create_environment(folder: Path, python: str | None, say=print) -> None:
    """Create the version's own Python environment and install it there (tests replace this)."""
    interpreter = choose_python(python, required_python(folder))
    run([interpreter, "-m", "venv", str(folder / ".venv")], "creating the Python environment",
        env=clean_environment(), timeout=INSTALL_SECONDS)
    command = [str(folder / ".venv/bin/python"), "-m", "pip", "install", "--disable-pip-version-check",
               "--no-input", "--progress-bar", "off"]
    if (folder / "constraints.txt").is_file():
        command += ["-c", "constraints.txt"]
    else:
        say("  This bundle has no constraints.txt; installing the newest compatible dependencies.")
    run([*command, "."], "installing dependencies", cwd=folder, env=clean_environment(), timeout=INSTALL_SECONDS)


def finish_files(folder: Path) -> None:
    """Remove build leftovers and make the launchers executable."""
    remove_tree(folder / "build")
    for leftover in (folder / "src").glob("*.egg-info"):
        remove_tree(leftover)
    for script in (*(folder / "scripts").glob("*.sh"), folder / "scripts" / "versions.py"):
        if script.is_file():
            os.chmod(script, 0o755)


def verify(folder: Path, version: str) -> int:
    """Start the version without contacting Canvas; returns how many tools it offers."""
    step = "checking that it starts"
    with tempfile.TemporaryDirectory(prefix="canvas-reader-check-") as scratch:
        env = clean_environment(HOME=scratch, TMPDIR=scratch, CANVAS_API_URL=DUMMY_CANVAS_URL,
                                CANVAS_API_TOKEN="check-only", PYTHONDONTWRITEBYTECODE="1")
        env.pop("XDG_CONFIG_HOME", None)
        output = run([str(folder / "scripts/start.sh"), "--check"], step, cwd=folder, env=env)
    report = None
    for line in reversed(output.strip().splitlines()):
        with contextlib.suppress(ValueError):
            candidate = json.loads(line)
            if isinstance(candidate, dict):
                report = candidate
                break
    if report is None or report.get("status") != "ok":
        raise StepFailed(step, "it did not report a successful start", output)
    if report.get("version") not in (None, version):  # 0.2.0 does not report its version
        raise StepFailed(step, f"it reports version {report.get('version')}, not {version}", output)
    tools = report.get("tools")
    if not isinstance(tools, list) or not tools:
        raise StepFailed(step, "it offers no tools", output)
    return len(tools)


def live_check(folder: Path, env_file: str | None) -> str:
    """Read a few items from Canvas with the user's own credentials (prints counts only)."""
    command = [str(folder / ".venv/bin/python"), str(folder / "scripts/smoke.py"), "--live"]
    if env_file:
        command += ["--env-file", str(Path(env_file).expanduser())]
    env = {key: value for key, value in os.environ.items() if key != "CONTROL_PLANE_API_KEY"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    output = run(command, "checking it against Canvas", cwd=folder, env=env, timeout=LIVE_SECONDS)
    return tail(output)


def install(home: Home, source: Path, *, activate_it: bool = True, force: bool = False, live: bool = False,
            python: str | None = None, env_file: str | None = None, action: str = "install",
            origin: str | None = None, say=print) -> str:
    """Install a bundle as a new version; nothing changes unless every step succeeds.

    ``origin`` names a download (such as a GitHub release) whose temporary file
    should be neither shown nor remembered as a place to look for updates.
    """
    source = Path(os.path.abspath(os.path.expanduser(str(source))))
    if not source.exists():
        raise Failure(f"{shown(source)} does not exist.")
    with locked(home):
        clean_up_leftovers(home, say)
        before = active_version(home)
        previous_link = os.readlink(home.current) if home.current.is_symlink() else None
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=home.versions))
        target = replaced = version = None
        created = False  # True once the new version's folder is ours to remove on failure
        previous_state, have_snapshot = None, False
        try:
            previous_state = state_snapshot(home)
            have_snapshot = True
            if source.is_dir():
                copy_folder(source, staging)
            else:
                extract_zip(source, staging)
            version = bundle_version(staging)
            target = home.versions / version
            if target.exists() or target.is_symlink():
                if not force:
                    raise Failure(f"Canvas Reader {version} is already installed. Switch to it with "
                                  f"scripts/versions.py use {version}, or reinstall it with --force.")
                replaced = home.versions / f".replaced-{version}-{os.getpid()}"
                os.rename(target, replaced)
            (staging / ".installing").write_text(json.dumps({"pid": os.getpid(), "started": now()}) + "\n")
            os.rename(staging, target)
            staging, created = None, True
            say(f"Installing Canvas Reader {version} from {origin or shown(source)}")
            say("  Creating its Python environment and installing dependencies (this can take a minute)...")
            create_environment(target, python, say)
            finish_files(target)
            say("  Checking that it starts...")
            tools = verify(target, version)
            if live:
                say("  Checking it against Canvas with your credentials...")
                say("  " + live_check(target, env_file).replace("\n", "\n  "))
            info = {"version": version, "installed_at": now(), "source": origin or str(source), "tools": tools}
            (target / ".install.json").write_text(json.dumps(info, indent=2) + "\n")
            (target / ".installing").unlink()
            last_source = None if origin else str(source.parent if source.is_file() else source)
            if activate_it:
                activate(home, version, action, last_source=last_source)
            elif last_source is not None:
                state = load_state(home)
                state["last_source"] = last_source
                save_state(home, state)
        except BaseException as error:
            for leftover in (staging, target if created else None):
                if leftover is not None:
                    with contextlib.suppress(OSError):
                        remove_tree(leftover)
            if replaced is not None and (replaced.exists() or replaced.is_symlink()):
                os.rename(replaced, home.versions / version)
            if have_snapshot:
                restore_current(home, previous_link)
                restore_state(home, previous_state)
            if isinstance(error, (Failure, KeyboardInterrupt, OSError)):
                raise _install_failure(error, version, before) from None
            raise
        if replaced is not None:
            try:
                remove_tree(replaced)
            except OSError:
                say(f"Note: the install succeeded, but the old reinstall backup remains at {shown(replaced)}.")
        if activate_it:
            note = f" (was {before})" if before and before != version else ""
            say(f"Installed and checked {version} ({tools} tools). Active: {version}{note}.")
        else:
            say(f"Installed and checked {version} ({tools} tools). Still active: {before or 'none'}; "
                f"switch with scripts/versions.py use {version}.")
        return version


def _install_failure(error: BaseException, version: str | None, before: str | None,
                     outcome: str = "was not installed") -> BaseException:
    name = f"Canvas Reader {version}" if version else "Canvas Reader"
    unchanged = f"Nothing was changed; {before} is still active." if before else "Nothing was changed."
    if isinstance(error, KeyboardInterrupt):
        return Interrupted(f"Stopped; {name} {outcome}. {unchanged}")
    if isinstance(error, StepFailed):
        message = f"{name} {outcome}: {error.step} failed ({error.reason})."
        if error.output:
            message += "\nLast output:\n    " + tail(error.output).replace("\n", "\n    ")
        return Failure(f"{message}\n{unchanged}")
    if isinstance(error, OSError):
        where = f" ({shown(error.filename)})" if error.filename else ""
        return Failure(f"{name} {outcome}: {error.strerror or error}{where}. {unchanged}")
    return Failure(f"{error} {unchanged}" if version else str(error))


class Interrupted(Failure):
    """Stopped by Control-C or a stop signal, after cleaning up."""


# --------------------------------------------------------------------------
# Finding updates
# --------------------------------------------------------------------------

def release_zips(folder: Path) -> list[tuple[str, Path]]:
    if not folder.is_dir():
        return []
    found = []
    for path in folder.iterdir():
        match = RELEASE_ZIP.match(path.name)
        if match and path.is_file():
            found.append((match.group(1), path))
    return sorted(found, key=lambda item: version_key(item[0]), reverse=True)


def update_folders(home: Home) -> list[Path]:
    folders = [SCRIPT_ROOT / "dist"]
    last = load_state(home).get("last_source")
    if isinstance(last, str):
        folders.append(Path(last))
    unique = []
    for folder in folders:
        if folder not in unique:
            unique.append(folder)
    return unique


def newest_release(folders: list[Path]) -> tuple[str, Path] | None:
    found = [item for folder in folders for item in release_zips(folder)]
    return max(found, key=lambda item: version_key(item[0])) if found else None


def checkout_release(home: Home) -> tuple[str, Path] | None:
    """This source checkout as an update, when versions.py runs from one (a git clone)."""
    if not is_checkout(SCRIPT_ROOT) or str(SCRIPT_ROOT).startswith(str(home.root)):
        return None
    version = checkout_version(SCRIPT_ROOT)
    return (version, SCRIPT_ROOT) if version else None


def _github_base() -> tuple[str, str]:
    api = (os.environ.get("CANVAS_READER_GITHUB_API") or GITHUB_API).rstrip("/")
    repository = os.environ.get("CANVAS_READER_GITHUB_REPOSITORY") or GITHUB_REPOSITORY
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise Failure("CANVAS_READER_GITHUB_REPOSITORY must look like owner/repository.")
    _https_only(api, api)
    return api, repository


def _https_only(url: str, api: str) -> str:
    """Downloads use HTTPS; plain HTTP is accepted only from a local test server."""
    parts, base = urlsplit(url), urlsplit(api)
    local = base.scheme == "http" and base.hostname in ("127.0.0.1", "localhost", "::1")
    if parts.scheme == "https" or (local and parts.scheme == "http" and parts.hostname == base.hostname):
        return url
    raise Failure("The GitHub release lists a download that doesn't use HTTPS. Nothing was installed.")


def _download(url: str, limit: int, what: str, accept: str = "application/octet-stream") -> bytes:
    """Fetch at most ``limit`` bytes; failures say what to do and never echo the response."""
    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "canvas-reader-versions"})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_SECONDS) as response:
            data = response.read(limit + 1)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise Failure(f"GitHub has no {what} (HTTP 404).") from None
        raise Failure(f"GitHub refused {what} (HTTP {error.code}). Try again later.") from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, ssl.SSLCertVerificationError):
            raise Failure("Python could not verify GitHub's certificate. If you use the python.org installer, "
                          "run 'Install Certificates.command' from its folder in Applications, then try again.") from None
        raise Failure(f"Could not download {what} from GitHub. Check your internet connection.") from None
    except (OSError, ValueError):
        raise Failure(f"Could not download {what} from GitHub. Check your internet connection.") from None
    if len(data) > limit:
        raise Failure(f"GitHub's {what} is larger than expected. Nothing was installed.")
    return data


def github_release() -> tuple[str, str, str, str]:
    """(version, zip name, zip URL, SHA256SUMS URL) of the newest published GitHub release."""
    api, repository = _github_base()
    text = _download(f"{api}/repos/{repository}/releases/latest", 1024 * 1024,
                     f"published release for {repository}", "application/vnd.github+json")
    try:
        release = json.loads(text.decode("utf-8"))
    except ValueError:
        raise Failure("GitHub's answer about the newest release could not be read. Try again later.") from None
    assets = {}
    for asset in release.get("assets", []) if isinstance(release, dict) else []:
        if isinstance(asset, dict) and isinstance(asset.get("name"), str) \
                and isinstance(asset.get("browser_download_url"), str):
            assets[asset["name"]] = asset["browser_download_url"]
    zips = [(match.group(1), name) for name, match in ((name, RELEASE_ZIP.match(name)) for name in assets) if match]
    if not zips:
        raise Failure(f"The newest release of {repository} on GitHub has no canvas-reader-X.Y.Z.zip.")
    version, name = max(zips, key=lambda item: version_key(item[0]))
    if CHECKSUMS not in assets:
        raise Failure(f"The newest release of {repository} has no {CHECKSUMS} file, so {name} can't be "
                      "checked. Nothing was installed.")
    return version, name, _https_only(assets[name], api), _https_only(assets[CHECKSUMS], api)


def download_release(name: str, zip_url: str, sums_url: str, folder: Path) -> Path:
    """Download a release zip into ``folder`` and check it against the release's SHA256SUMS."""
    sums = _download(sums_url, 64 * 1024, CHECKSUMS).decode("utf-8", "replace")
    expected = None
    for line in sums.splitlines():
        match = re.fullmatch(r"\s*([0-9a-fA-F]{64})\s+\*?(\S+)\s*", line)
        if match and match.group(2) == name:
            expected = match.group(1).lower()
    if expected is None:
        raise Failure(f"{CHECKSUMS} doesn't list {name}. Nothing was installed.")
    data = _download(zip_url, MAX_ZIP_BYTES, name)
    if hashlib.sha256(data).hexdigest() != expected:
        raise Failure(f"The downloaded {name} doesn't match its checksum. Nothing was installed; try again later.")
    path = folder / name
    path.write_bytes(data)
    return path


# --------------------------------------------------------------------------
# The tunnel profile
# --------------------------------------------------------------------------

def tunnel_profile(name: str | None = None) -> Path:
    """The tunnel-client profile tunnel.sh uses (same environment variables)."""
    if os.environ.get("TUNNEL_CLIENT_PROFILE_FILE") and not name:
        return Path(os.environ["TUNNEL_CLIENT_PROFILE_FILE"]).expanduser()
    name = name or os.environ.get("CANVAS_READER_TUNNEL_PROFILE") or "canvas-reader"
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise Failure("Use letters, numbers, underscores or hyphens for the tunnel profile name.")
    folder = Path(os.environ.get("TUNNEL_CLIENT_PROFILE_DIR") or Path.home() / ".config" / "tunnel-client")
    return folder.expanduser() / f"{name}.yaml"


_COMMAND_LINE = re.compile(r"^(?P<lead>\s*(?:-\s+)?command:[ \t]*)(?P<value>\S.*?)[ \t]*$")


def _decode_scalar(value: str) -> str:
    """A YAML scalar as tunnel-client writes it (double-quoted, Go %q style), or single-quoted/plain."""
    if value.startswith('"'):
        try:
            text, end = json.JSONDecoder().raw_decode(value)
        except ValueError:
            raise Failure("The tunnel profile's command uses quoting this tool can't read; edit it by hand.") from None
        rest = value[end:].strip()
        if rest and not rest.startswith("#"):
            raise Failure("The tunnel profile's command line has unexpected text after it; edit it by hand.")
        return text
    if value.startswith("'"):
        match = re.match(r"'((?:[^']|'')*)'\s*(?:#.*)?$", value)
        if not match:
            raise Failure("The tunnel profile's command line can't be read; edit it by hand.")
        return match.group(1).replace("''", "'")
    return re.sub(r"\s+#.*$", "", value)


def _shell_word(word: str) -> str:
    """Quote like tunnel.sh: bare options, everything else in single quotes."""
    if re.fullmatch(r"--?[A-Za-z0-9][A-Za-z0-9_-]*", word):
        return word
    return "'" + word.replace("'", "'\\''") + "'"


def profile_commands(text: str) -> list[tuple[int, str, list[str], int]]:
    """(line number, prefix, shell words, index of start.sh) for each Canvas Reader command."""
    found = []
    for number, line in enumerate(text.splitlines()):
        match = _COMMAND_LINE.match(line)
        if not match:
            continue
        try:
            words = shlex.split(_decode_scalar(match.group("value")))
        except ValueError:
            continue
        for index, word in enumerate(words):
            if word.endswith("scripts/start.sh"):
                found.append((number, match.group("lead"), words, index))
                break
    return found


def tunnel_target(profile: Path) -> str | None:
    """The start.sh path the tunnel runs, if the profile exists and names one."""
    try:
        commands = profile_commands(profile.read_text(encoding="utf-8"))
    except OSError:
        return None
    return commands[0][2][commands[0][3]] if commands else None


def describe_target(path: str | None, home: Home) -> str:
    if path is None:
        return "not set up"
    norm = os.path.normpath(path)
    managed = os.path.normpath(str(home.root))
    if norm == os.path.join(managed, "current", "scripts", "start.sh"):
        return "managed versions (follows the active version)"
    match = re.match(re.escape(os.path.join(managed, "versions")) + r"/([^/]+)/scripts/start\.sh$", norm)
    if match:
        return f"managed version {match.group(1)} only (pinned; run versions.py link-tunnel to follow updates)"
    if is_checkout(SCRIPT_ROOT) and norm == os.path.normpath(str(SCRIPT_ROOT / "scripts" / "start.sh")):
        return f"this checkout ({shown(SCRIPT_ROOT)})"
    missing = "" if os.path.isfile(path) else " - missing!"
    return f"{shown(path)}{missing}"


def relink(profile: Path, start: str, *, dry_run: bool = False, say=print) -> bool:
    """Point the profile's command at another start.sh; keeps everything else. Returns True if it changed."""
    real = Path(os.path.realpath(profile))
    text = real.read_text(encoding="utf-8")
    commands = profile_commands(text)
    if not commands:
        raise Failure(f"{shown(profile)} has no Canvas Reader command. Create the profile with "
                      "scripts/tunnel.sh --setup <tunnel ID> (docs/clients/chatgpt.md).")
    lines = text.splitlines(keepends=True)
    changed = False
    for number, lead, words, index in commands:
        if words[index] == start:
            continue
        old = words[index]
        words = [*words[:index], start, *words[index + 1:]]
        command = " ".join(_shell_word(word) for word in words)
        ending = "\n" if lines[number].endswith("\n") else ""
        lines[number] = f"{lead}{json.dumps(command, ensure_ascii=False)}{ending}"
        say(f"Tunnel profile {shown(profile)}:\n  was: {shown(old)}\n  now: {shown(start)}")
        changed = True
    if not changed:
        say(f"The tunnel already runs {shown(start)}.")
        return False
    if dry_run:
        say("Dry run: nothing was changed.")
        return False
    backup = real.with_name(f"{real.name}.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(real, backup)
    write_atomic(real, "".join(lines))
    say(f"Saved. The previous profile is at {shown(backup)}.")
    return True


# --------------------------------------------------------------------------
# Running readers, credentials, checkouts
# --------------------------------------------------------------------------

_READER = re.compile(r"(?:/bin/canvas-reader|canvas_reader\.server|-m canvas_reader)(?:\s|$)")


def list_processes() -> list[tuple[int, datetime | None, str]]:
    """(pid, start time, command line) of every process, via ps (tests replace this)."""
    try:
        output = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "lstart=", "-o", "command="], capture_output=True, text=True,
                                timeout=10, check=False, env={**os.environ, "LC_ALL": "C"}).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = []
    for line in output.splitlines():
        parts = line.split(None, 6)
        if len(parts) < 7 or not parts[0].isdigit():
            continue
        try:
            started = datetime.strptime(" ".join(parts[1:6]), "%a %b %d %H:%M:%S %Y")
        except ValueError:
            started = None
        found.append((int(parts[0]), started, parts[6]))
    return found


def reader_processes(home: Home) -> list[dict]:
    """Running Canvas Reader servers and which copy each one runs."""
    readers = []
    versions = os.path.join(os.path.normpath(str(home.root)), "versions") + os.sep
    for pid, started, command in list_processes():
        if pid == os.getpid() or not _READER.search(command):
            continue
        if versions in command:
            runs = command.split(versions, 1)[1].split("/", 1)[0]
        elif str(SCRIPT_ROOT) + os.sep in command:
            runs = "checkout"
        else:
            runs = "another copy"
        readers.append({"pid": pid, "started": started, "runs": runs})
    return readers


def credential_file(profile: Path) -> tuple[Path | None, str]:
    """The Canvas credential file the tunnel uses (else the default) and how it was found."""
    try:
        commands = profile_commands(profile.read_text(encoding="utf-8"))
    except OSError:
        commands = []
    for _, _, words, _ in commands:
        if "--env-file" in words[:-1]:
            return Path(words[words.index("--env-file") + 1]), "used by the tunnel"
    if os.environ.get("CANVAS_ENV_FILE"):
        return Path(os.environ["CANVAS_ENV_FILE"]).expanduser(), "from CANVAS_ENV_FILE"
    return default_credentials(), "default location"


def default_credentials() -> Path:
    config = os.environ.get("XDG_CONFIG_HOME")
    return (Path(config) if config else Path.home() / ".config") / "canvas-reader" / "canvas.env"


def is_checkout(folder: Path) -> bool:
    return (folder / "scripts" / "build_plugin.py").is_file() and (folder / "src" / "canvas_reader").is_dir()


def checkout_version(folder: Path) -> str | None:
    try:
        return bundle_version(folder)
    except (Failure, OSError):
        return None


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_status(args) -> int:
    home = Home(home_dir())
    state = load_state(home)
    active = active_version(home)
    versions = installed_versions(home)
    previous = state.get("previous") if state.get("previous") in versions else None
    print(f"Canvas Reader versions ({shown(home.root)})")
    if not versions:
        print("  Installed:   none yet")
    else:
        print(f"  Active:      {active or 'none'}")
        if previous:
            print(f"  Previous:    {previous} (scripts/versions.py rollback switches back)")
        print(f"  Installed:   {', '.join(versions)}")
        broken = [version for version in versions if not environment_ok(home.versions / version)]
        if broken:
            print(f"  Broken:      {', '.join(broken)} (its Python is gone; reinstall with install --force)")
    if home.versions.is_dir() and any(child.name.startswith(".staging-") or (child.is_dir() and incomplete(child))
                                      for child in home.versions.iterdir()):
        print("  Leftovers:   an interrupted install; the next install or update removes it")
    if is_checkout(SCRIPT_ROOT) and not str(SCRIPT_ROOT).startswith(str(home.root)):
        print(f"  Checkout:    {shown(SCRIPT_ROOT)} ({checkout_version(SCRIPT_ROOT) or 'version unknown'})")
    profile = tunnel_profile(getattr(args, "profile", None))
    target = tunnel_target(profile)
    if profile.is_file():
        print(f"  Tunnel runs: {describe_target(target, home)}")
    else:
        print(f"  Tunnel runs: no profile at {shown(profile)} (scripts/tunnel.sh --setup creates it)")
    credentials, origin = credential_file(profile)
    if credentials is not None and credentials.is_file():
        private = not credentials.stat().st_mode & 0o077
        note = "private" if private else f"readable by other users: run chmod 600 '{credentials}'"
        print(f"  Credentials: {shown(credentials)} ({origin}; {note})")
    else:
        print(f"  Credentials: none found at {shown(credentials)} (docs/SETUP.md, step 2)")
    if token_saved(security_tool()):
        print("  Keychain:    a Canvas token is saved (used when no file or client sets one)")
    readers = reader_processes(home)
    if readers:
        for reader in readers:
            since = reader["started"].strftime("%b %d %H:%M") if reader["started"] else "unknown time"
            runs = reader["runs"]
            stale = runs not in (active, "checkout", "another copy") and active is not None
            label = f"version {runs}" if runs not in ("checkout", "another copy") else runs
            warning = f"  <- older than the active {active}; restart the tunnel" if stale else ""
            print(f"  Running:     pid {reader['pid']}, {label}, since {since}{warning}")
    else:
        print("  Running:     no reader process (the tunnel is stopped, or no chat has used it yet)")
    newest = newest_release(update_folders(home))
    checkout = checkout_release(home)
    if checkout and (newest is None or version_key(checkout[0]) > version_key(newest[0])):
        newest = checkout
    if newest and (active is None or version_key(newest[0]) > version_key(active)):
        print(f"  Update:      {newest[0]} is available ({shown(newest[1])}); run scripts/versions.py update")
    if versions and target and describe_target(target, home).startswith("this checkout"):
        print("\nThe tunnel still runs the checkout, not the managed versions. "
              "To switch it once: scripts/versions.py link-tunnel")
    return 0


def cmd_list(args) -> int:
    home = Home(home_dir())
    versions = installed_versions(home)
    if not versions:
        print("No versions installed. Install one with: scripts/versions.py update")
        return 0
    active, previous = active_version(home), load_state(home).get("previous")
    for version in versions:
        info = install_info(home.versions / version)
        marks = [mark for mark, on in (("active", version == active), ("previous", version == previous),
                                       ("broken", not environment_ok(home.versions / version))) if on]
        installed = str(info.get("installed_at", ""))[:16].replace("T", " ")
        line = f"{'*' if version == active else ' '} {version:<12} {installed:<17} {', '.join(marks)}"
        print(line.rstrip())
    return 0


def cmd_install(args) -> int:
    home = Home(home_dir())
    install(home, Path(args.source), activate_it=not args.no_activate, force=args.force, live=args.live,
            python=args.python, env_file=args.env_file)
    if not args.no_activate:
        _after_activation(home, args)
    return 0


def cmd_update(args) -> int:
    home = Home(home_dir())
    active = active_version(home)
    if getattr(args, "github", False):
        if args.source:
            raise Failure("Give either a SOURCE or --github, not both.")
        return _update_from_github(home, active, args)
    source = Path(args.source).expanduser() if args.source else None
    if source is not None and (source.is_file() or (source / "plugin.json").is_file()):
        candidate = source  # one specific bundle
    else:
        folders = [source] if source is not None else update_folders(home)
        newest = newest_release(folders)
        # A clone updates itself: after git pull, its own source may be newer than any zip.
        checkout = checkout_release(home) if source is None else None
        if checkout and (newest is None or version_key(checkout[0]) > version_key(newest[0])):
            newest = checkout
        if newest is None:
            where = " or ".join(shown(folder) for folder in folders)
            raise Failure(f"No canvas-reader-X.Y.Z.zip found in {where}. Build one with "
                          "scripts/build_plugin.py, or pass the zip: scripts/versions.py update PATH")
        version, candidate = newest
        handled = _not_newer(home, active, version, args)
        if handled is not None:
            return handled
    install(home, candidate, activate_it=not args.no_activate, force=args.force, live=args.live,
            python=args.python, env_file=args.env_file, action="update")
    if not args.no_activate:
        _after_activation(home, args)
    return 0


def _not_newer(home: Home, active: str | None, version: str, args) -> int | None:
    """Handle an update that needs no install (up to date, or installed already); None means install it."""
    if active and version_key(version) <= version_key(active):
        print(f"Already up to date: {active} is active (newest available: {version}).")
        return 0
    if version in installed_versions(home) and not args.force:
        if args.no_activate:
            print(f"{version} is already installed; switch to it with scripts/versions.py use {version}.")
            return 0
        print(f"{version} is already installed; switching to it.")
        return _switch(home, version, "update", args)
    return None


def _update_from_github(home: Home, active: str | None, args) -> int:
    print("Checking GitHub for the newest release...")
    version, name, zip_url, sums_url = github_release()
    handled = _not_newer(home, active, version, args)
    if handled is not None:
        return handled
    with tempfile.TemporaryDirectory(prefix="canvas-reader-download-") as scratch:
        print(f"Downloading {name} and checking it against {CHECKSUMS}...")
        archive = download_release(name, zip_url, sums_url, Path(scratch))
        install(home, archive, activate_it=not args.no_activate, force=args.force, live=args.live,
                python=args.python, env_file=args.env_file, action="update",
                origin=f"the GitHub release ({name})")
    if not args.no_activate:
        _after_activation(home, args)
    return 0


def _switch(home: Home, version: str, action: str, args) -> int:
    with locked(home):
        clean_up_leftovers(home)
        if version not in installed_versions(home):
            raise Failure(f"{version} is not installed. Installed: {', '.join(installed_versions(home)) or 'none'}.")
        if version == active_version(home):
            print(f"{version} is already active.")
            return 0
        folder = home.versions / version
        if not environment_ok(folder):
            raise Failure(f"{version}'s Python environment is broken (its Python may have been removed). "
                          f"Reinstall it with scripts/versions.py install --force <its zip>.")
        print(f"Checking that {version} starts...")
        try:
            tools = verify(folder, version)
        except StepFailed as error:
            raise _install_failure(error, version, active_version(home), "was not activated") from None
        old = activate(home, version, action)
        print(f"Active: {version} ({tools} tools){f'; was {old}' if old else ''}.")
    _after_activation(home, args)
    return 0


def _after_activation(home: Home, args) -> None:
    profile = tunnel_profile(getattr(args, "profile", None))
    target = tunnel_target(profile)
    description = describe_target(target, home)
    if description.startswith("managed versions"):
        if any(reader["runs"] not in (active_version(home),) for reader in reader_processes(home)):
            print(RESTART_HINT)
        else:
            print("The tunnel uses it the next time it starts.")
    elif target is None:
        print("Next: connect your assistants (docs/SETUP.md, step 4). Every client runs the active version.")
    else:
        print(f"The tunnel still runs {description}. To use managed versions: scripts/versions.py link-tunnel")


def cmd_use(args) -> int:
    return _switch(Home(home_dir()), args.version, "use", args)


def cmd_rollback(args) -> int:
    home = Home(home_dir())
    previous = load_state(home).get("previous")
    if not previous or previous not in installed_versions(home):
        raise Failure("There is no earlier version to switch back to. See installed versions with "
                      "scripts/versions.py list.")
    return _switch(home, previous, "rollback", args)


def _pinned(home: Home, version: str) -> bool:
    target = tunnel_target(tunnel_profile())
    return bool(target) and os.path.normpath(target).startswith(
        os.path.join(os.path.normpath(str(home.root)), "versions", version) + os.sep)


def cmd_remove(args) -> int:
    home = Home(home_dir())
    with locked(home):
        clean_up_leftovers(home)
        version = args.version
        if version not in installed_versions(home):
            raise Failure(f"{version} is not installed.")
        if version == active_version(home):
            raise Failure(f"{version} is active. Switch first (scripts/versions.py use VERSION or rollback).")
        if _pinned(home, version):
            raise Failure(f"The tunnel profile runs {version} directly. Run scripts/versions.py link-tunnel first.")
        remove_tree(home.versions / version)
        state = load_state(home)
        if state.get("previous") == version:
            state.pop("previous")
            save_state(home, state)
    print(f"Removed {version}.")
    return 0


def cmd_prune(args) -> int:
    if args.keep < 1:
        raise Failure("--keep must be at least 1.")
    home = Home(home_dir())
    with locked(home):
        clean_up_leftovers(home)
        versions = installed_versions(home)
        keep = set(versions[:args.keep]) | {active_version(home), load_state(home).get("previous")}
        removed = sorted((version for version in versions if version not in keep and not _pinned(home, version)),
                         key=version_key)
        for version in removed:
            remove_tree(home.versions / version)
    print(f"Removed {', '.join(removed)}." if removed else "Nothing to remove.")
    return 0


def cmd_link_tunnel(args) -> int:
    home = Home(home_dir())
    profile = tunnel_profile(args.profile)
    if not profile.is_file():
        raise Failure(f"No tunnel profile at {shown(profile)}. Create it first with scripts/tunnel.sh --setup "
                      "<tunnel ID> (docs/clients/chatgpt.md), or set TUNNEL_CLIENT_PROFILE_FILE.")
    destination = args.to or "managed"
    if destination == "managed":
        if active_version(home) is None:
            raise Failure("No managed version is installed yet. Run scripts/versions.py update first.")
        start = str(home.current / "scripts" / "start.sh")
    elif destination == "checkout":
        if not is_checkout(SCRIPT_ROOT) or str(SCRIPT_ROOT).startswith(str(home.root)):
            raise Failure("Run this from your source checkout's scripts/versions.py to link the checkout.")
        start = str(SCRIPT_ROOT / "scripts" / "start.sh")
    else:
        path = Path(destination).expanduser().absolute()
        start = str(path if path.name == "start.sh" else path / "scripts" / "start.sh")
    if not os.access(start, os.X_OK):
        raise Failure(f"{shown(start)} is missing or not executable.")
    if relink(profile, start, dry_run=args.dry_run) and not args.dry_run:
        print(TUNNEL_RESTART_HINT)
    return 0


def check_uninstall_home(home: Home) -> None:
    """Refuse a mixed or redirected folder before uninstall changes anything."""
    if not home.root.exists() and not home.root.is_symlink():
        return

    def refuse(path: Path) -> None:
        raise Failure(f"Refusing to uninstall: {shown(path)} is not a recognized Canvas Reader managed path. "
                      "Nothing was changed; check CANVAS_READER_HOME and keep unrelated files outside it.")

    def regular(path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            refuse(path)

    def managed_link(path: Path) -> None:
        if not path.is_symlink():
            refuse(path)
        target = Path(os.path.abspath(path.parent / os.readlink(path)))
        if not VERSION.fullmatch(target.name) or target.parent != home.versions.absolute():
            refuse(path)

    if home.root.is_symlink() or not home.root.is_dir():
        refuse(home.root)
    for child in home.root.iterdir():
        if child.name == "versions":
            if child.is_symlink() or not child.is_dir():
                refuse(child)
            for folder in child.iterdir():
                # Leftovers of an interrupted install, and Finder metadata; uninstall removes them.
                if folder.name == ".DS_Store":
                    regular(folder)
                    continue
                if re.fullmatch(r"\.staging-[A-Za-z0-9_]+", folder.name):
                    if folder.is_symlink() or not folder.is_dir():
                        refuse(folder)
                    continue
                replaced = re.fullmatch(r"\.replaced-(.+)-\d+", folder.name)
                version = replaced.group(1) if replaced else folder.name
                if folder.is_symlink() or not folder.is_dir() or not VERSION.fullmatch(version):
                    refuse(folder)
                try:
                    matches = bundle_version(folder) == version
                except (Failure, OSError, ValueError):
                    matches = False
                # Complete 0.3.1 installs already carry this metadata; no migration is needed.
                if not matches or (not incomplete(folder) and install_info(folder).get("version") != version):
                    refuse(folder)
        elif child.name == "current" or re.fullmatch(r"\.current-\d+", child.name):
            managed_link(child)
        elif child.name == "state.json":
            regular(child)
            try:
                state = json.loads(child.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                refuse(child)
            if not isinstance(state, dict) or not set(state) <= {"history", "previous", "last_source"}:
                refuse(child)
        elif child.name in (".lock", ".DS_Store"):
            regular(child)
        else:
            refuse(child)


def cmd_uninstall(args) -> int:
    home = Home(home_dir())
    check_uninstall_home(home)
    profile = tunnel_profile()
    target = tunnel_target(profile)
    managed_root = os.path.normpath(str(home.root)) + os.sep
    tunnel_managed = bool(target) and os.path.normpath(target).startswith(managed_root)
    checkout_ready = (is_checkout(SCRIPT_ROOT) and not str(SCRIPT_ROOT).startswith(str(home.root))
                      and (SCRIPT_ROOT / ".venv/bin/canvas-reader").is_file())
    plan, notes = [], []
    if home.root.exists():
        plan.append(f"Delete every installed version and the version settings: {shown(home.root)}")
    if tunnel_managed:
        if checkout_ready:
            plan.append(f"Point the tunnel back at the checkout ({shown(SCRIPT_ROOT)})")
        else:
            notes.append("The tunnel profile runs a managed version; afterwards, recreate it with "
                         "scripts/tunnel.sh --setup <tunnel ID> before running the tunnel.")
    security = security_tool()
    service = os.environ.get("CANVAS_READER_KEYCHAIN_SERVICE", "canvas-reader-tunnel")
    if args.key:
        plan.append(f"Delete the OpenAI runtime key from your Keychain ({service})")
    credentials = default_credentials()
    if args.credentials:
        if credentials.is_file():
            plan.append(f"Delete your Canvas credential file: {shown(credentials)}")
        if os.access(security, os.X_OK):
            plan.append(f"Delete the Canvas token from your Keychain, if saved ({token_service()})")
        tunnel_credentials, origin = credential_file(profile)
        if tunnel_credentials and tunnel_credentials != credentials and tunnel_credentials.is_file():
            notes.append(f"Not deleted: {shown(tunnel_credentials)} ({origin}); it is outside Canvas Reader's "
                         "folders, so delete it yourself if you no longer need it.")
    readers = [reader for reader in reader_processes(home) if reader["runs"] not in ("checkout", "another copy")]
    if readers and home.root.exists():
        notes.append(f"{len(readers)} reader process(es) still run from the managed versions; stop the tunnel first.")
    if not plan:
        print("Nothing to remove.")
        for note in notes:
            print(f"Note: {note}")
        return 0
    print("This will:")
    for item in plan:
        print(f"  - {item}")
    for note in notes:
        print(f"Note: {note}")
    if not args.yes:
        if not sys.stdin.isatty():
            raise Failure("Add --yes to confirm.")
        if input("Type yes to continue: ").strip().lower() != "yes":
            print("Nothing was changed.")
            return 1
    if home.root.exists():
        with locked(home):
            check_uninstall_home(home)
            owned = list(home.root.iterdir())
            owned_versions = list(home.versions.iterdir())
            if tunnel_managed and checkout_ready:
                relink(profile, str(SCRIPT_ROOT / "scripts" / "start.sh"))
            for child in owned:
                if child.name != ".lock":
                    if child == home.versions:
                        for folder in owned_versions:
                            remove_tree(folder)
                        try:
                            child.rmdir()
                        except OSError:
                            raise Failure(f"Uninstall stopped: {shown(child)} now contains other files; "
                                          "they were retained. No saved key or credential file was deleted.") from None
                    else:
                        remove_tree(child)
            home.lock_file.unlink()
            # Keep the old inode locked through deletion; pending openers must validate it.
            # Never recursively remove the root: retain any unrelated file added during uninstall.
            try:
                home.root.rmdir()
            except OSError:
                raise Failure(f"Installed versions were removed, but {shown(home.root)} now contains other files; "
                              "the folder was retained. No saved key or credential file was deleted.") from None
        print(f"Deleted {shown(home.root)}.")
    elif tunnel_managed and checkout_ready:
        relink(profile, str(SCRIPT_ROOT / "scripts" / "start.sh"))
    if args.key:
        if os.access(security, os.X_OK):
            result = subprocess.run([security, "delete-generic-password", "-s", service], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, check=False)
            print(f"Removed \"{service}\" from your Keychain." if result.returncode == 0
                  else f"No saved key named \"{service}\".")
        else:
            print("The macOS Keychain tool is unavailable here; no key was removed.")
    if args.credentials and credentials.is_file():
        credentials.unlink()
        with contextlib.suppress(OSError):
            credentials.parent.rmdir()  # only if now empty
        print(f"Deleted {shown(credentials)}.")
    if args.credentials and os.access(security, os.X_OK):
        forget_token(security)
    print("Not touched: your project folder, the tunnel profile, your Claude, Codex and ChatGPT settings "
          "(docs/SETUP.md, Remove everything), and the token itself in Canvas (delete it in Canvas, "
          "Account > Settings).")
    return 0


def security_tool() -> str:
    return os.environ.get("CANVAS_READER_SECURITY_BIN") or "/usr/bin/security"


def token_service() -> str:
    return os.environ.get("CANVAS_READER_TOKEN_SERVICE") or TOKEN_SERVICE


def token_saved(security: str) -> bool:
    """Whether a Canvas token is in the Keychain; reads only the item's attributes, never the token."""
    if not os.access(security, os.X_OK):
        return False
    return subprocess.run([security, "find-generic-password", "-s", token_service()], stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False).returncode == 0


def forget_token(security: str) -> None:
    result = subprocess.run([security, "delete-generic-password", "-s", token_service()], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    print(f"Removed the Canvas token \"{token_service()}\" from your Keychain." if result.returncode == 0
          else f"No Canvas token named \"{token_service()}\" in your Keychain.")


def cmd_token(args) -> int:
    security = security_tool()
    if not os.access(security, os.X_OK):
        raise Failure("The macOS Keychain tool is unavailable here; keep the token in the credential file instead.")
    if args.action == "status":
        if token_saved(security):
            print(f"A Canvas token is saved in your Keychain as \"{token_service()}\". Canvas Reader uses it when "
                  "neither the credential file nor the client sets CANVAS_API_TOKEN.")
        else:
            print("No Canvas token is saved in your Keychain.")
    elif args.action == "forget":
        forget_token(security)
    else:
        print("Paste your Canvas access token when asked, twice. Typing stays hidden.")
        result = subprocess.run([security, "add-generic-password", "-U", "-a", os.environ.get("USER") or "canvas-reader",
                                 "-s", token_service(), "-l", "Canvas Reader Canvas API token", "-w"], check=False)
        if result.returncode != 0:
            raise Failure("The token was not saved.")
        print(f"Saved in your login Keychain as \"{token_service()}\". Leave CANVAS_API_TOKEN blank in the "
              "credential file so this token is used, then restart Canvas Reader in your apps.")
    return 0


# --------------------------------------------------------------------------
# Releasing (source checkout only)
# --------------------------------------------------------------------------

def write_source_snapshot(root: Path, archive: Path, version: str) -> int:
    """Zip the checkout's source, without environments, build output or credential-like files."""
    archive.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    temporary = archive.with_name(f".{archive.name}.partial")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path, relative in source_files(root):
                info = zipfile.ZipInfo(f"canvas-reader-{version}/{relative.as_posix()}",
                                       date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | (0o755 if path.stat().st_mode & 0o111 else 0o644)) << 16
                bundle.writestr(info, path.read_bytes())
                count += 1
        os.replace(temporary, archive)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    return count


def cmd_bump(args) -> int:
    root = SCRIPT_ROOT
    if not is_checkout(root) or not (root / "CHANGELOG.md").is_file():
        raise Failure("bump works in the source checkout (with CHANGELOG.md and scripts/build_plugin.py).")
    new = args.version
    if not VERSION.match(new):
        raise Failure(f"{new!r} is not a version number like 0.3.1.")
    current = bundle_version(root)
    if version_key(new) <= version_key(current):
        raise Failure(f"{new} is not newer than the current version {current}.")
    # Keep the outgoing version's source next to the release files.
    snapshot = root / "dist" / f"canvas-reader-{current}-source.zip"
    if snapshot.exists():
        print(f"Source of {current} already saved: {shown(snapshot)}")
    else:
        count = write_source_snapshot(root, snapshot, current)
        print(f"Saved the {current} source ({count} files) to {shown(snapshot)}")
    init = root / "src/canvas_reader/__init__.py"
    text = init.read_text(encoding="utf-8")
    write_atomic(init, re.sub(r"""^(__version__\s*=\s*)["'][^"']+["']""", rf'\g<1>"{new}"', text, count=1,
                              flags=re.MULTILINE))
    # The portable manifest and, when present, the Claude Code plugin manifest (which pins
    # users to its version until it changes).
    for manifest_path in (root / "plugin.json", root / ".claude-plugin" / "plugin.json"):
        if manifest_path.parent != root and not manifest_path.is_file():
            continue
        manifest = manifest_path.read_text(encoding="utf-8")
        updated = re.sub(r'^(  "version":\s*)"[^"]+"', rf'\g<1>"{new}"', manifest, count=1, flags=re.MULTILINE)
        if json.loads(updated).get("version") != new:
            raise Failure(f"Could not update the version in {manifest_path.relative_to(root)}; edit it by hand.")
        write_atomic(manifest_path, updated)
    changelog_path = root / "CHANGELOG.md"
    changelog = changelog_path.read_text(encoding="utf-8")
    heading = f"## {new} ({datetime.now().date().isoformat()})"
    if re.search(rf"^## {re.escape(new)}\b", changelog, re.MULTILINE):
        pass
    elif re.search(r"^## Unreleased\s*$", changelog, re.MULTILINE):
        changelog = re.sub(r"^## Unreleased\s*$", heading, changelog, count=1, flags=re.MULTILINE)
    else:
        changelog = re.sub(r"^(# Changelog[ \t]*\n)", rf"\g<1>\n{heading}\n\n- Describe the changes here.\n",
                           changelog, count=1, flags=re.MULTILINE)
    write_atomic(changelog_path, changelog)
    print(f"Version {current} -> {new} in src/canvas_reader/__init__.py, the plugin manifests and CHANGELOG.md.")
    print("Next: describe the changes in CHANGELOG.md, run the tests, then build with "
          ".venv/bin/python scripts/build_plugin.py --release (AGENTS.md, Release).")
    return 0


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def _interrupt(signum, frame):
    raise KeyboardInterrupt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts/versions.py",
        description="Install, update, switch and remove Canvas Reader versions. Each version has its own "
                    "folder and Python environment; the active one is what the tunnel runs.",
        epilog="Everyday use: scripts/versions.py update, then restart the tunnel. "
               "If something is wrong: scripts/versions.py rollback. See docs/SETUP.md.")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    def add(name, handler, help_text):
        command = commands.add_parser(name, help=help_text, description=help_text)
        command.set_defaults(handler=handler)
        return command

    add("status", cmd_status, "Show the active version, what the tunnel runs, running readers and updates.")
    add("list", cmd_list, "List installed versions.")
    for name, handler, help_text in (
        ("install", cmd_install, "Install a bundle zip or folder as a new version, check it, and make it active."),
        ("update", cmd_update, "Install the newest release (a dist/canvas-reader-X.Y.Z.zip, or this source checkout) "
                               "if it is newer than the active one."),
    ):
        command = add(name, handler, help_text)
        if name == "install":
            command.add_argument("source", metavar="SOURCE", help="A canvas-reader-X.Y.Z.zip or an unzipped folder")
        else:
            command.add_argument("source", metavar="SOURCE", nargs="?",
                                 help="A zip, or a folder to search (default: this checkout's dist/ and source)")
            command.add_argument("--github", action="store_true",
                                 help="Download the newest published GitHub release, check it against SHA256SUMS, "
                                      "and install it")
        command.add_argument("--no-activate", action="store_true", help="Install and check, but keep the active version")
        command.add_argument("--force", action="store_true", help="Reinstall a version that is already installed")
        command.add_argument("--live", action="store_true",
                             help="Also read a few items from Canvas with your credentials (prints counts only)")
        command.add_argument("--env-file", help="Credential file for --live (default: ~/.config/canvas-reader/canvas.env)")
        command.add_argument("--python", help="Python 3.11+ to build the environment with (default: found automatically)")
    command = add("use", cmd_use, "Switch to an installed version.")
    command.add_argument("version", metavar="VERSION")
    add("rollback", cmd_rollback, "Switch back to the previously active version.")
    command = add("remove", cmd_remove, "Delete an installed version that is not active.")
    command.add_argument("version", metavar="VERSION")
    command = add("prune", cmd_prune, "Delete old versions; keeps the newest, the active and the previous one.")
    command.add_argument("--keep", type=int, default=2, help="How many of the newest versions to keep (default 2)")
    command = add("link-tunnel", cmd_link_tunnel,
                  "Point the tunnel profile at the managed versions (once); keeps a backup of the profile.")
    command.add_argument("--to", metavar="managed|checkout|PATH",
                         help="managed (default), checkout (this source folder), or another Canvas Reader folder")
    command.add_argument("--profile", help="Tunnel profile name (default: canvas-reader)")
    command.add_argument("--dry-run", action="store_true", help="Show the change without saving it")
    command = add("uninstall", cmd_uninstall, "Delete all managed versions; optionally the saved key and credentials.")
    command.add_argument("--yes", action="store_true", help="Don't ask for confirmation")
    command.add_argument("--key", action="store_true", help="Also delete the OpenAI runtime key from the Keychain")
    command.add_argument("--credentials", action="store_true",
                         help="Also delete ~/.config/canvas-reader/canvas.env and a Canvas token saved in the Keychain")
    command = add("token", cmd_token, "Keep the Canvas token in the macOS Keychain instead of the credential file.")
    command.add_argument("action", choices=("store", "forget", "status"),
                         help="store (hidden prompt), forget, or status")
    command = add("bump", cmd_bump, "Release helper: save the current source to dist/, then set a new version "
                                    "number everywhere it is stated.")
    command.add_argument("version", metavar="VERSION")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not hasattr(args, "profile"):
        args.profile = None
    previous = {signum: signal.signal(signum, _interrupt) for signum in (signal.SIGTERM, signal.SIGHUP)}
    try:
        return args.handler(args)
    except Interrupted as stopped:
        print(stopped, file=sys.stderr)
        return 130
    except Failure as failure:
        print(f"versions.py: {failure}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        return 130
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    sys.exit(main())
