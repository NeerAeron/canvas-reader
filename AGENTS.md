# AGENTS.md

Guidance for coding agents (Claude Code, Codex and others) and contributors
working on Canvas Reader. Read this before changing anything.

## Core idea

Canvas Reader is a lean, efficient, fast and non-invasive way for agents to read
everything in a student's Canvas (including grades, files, announcements, inbox
etc.). That is the ethos of the project; judge every change against it.

It is also what sets Canvas Reader apart from
[canvas-mcp](https://github.com/vishalsachdev/canvas-mcp), which it builds on:
student-facing, read-only, complete, and quick to answer.

## What this project is

Canvas Reader is a read-only MCP server for a student's own Canvas LMS account.
It runs locally on the user's Mac over stdio, using the user's own Canvas API
token. It exposes the student read tools of
[canvas-mcp](https://github.com/vishalsachdev/canvas-mcp) 1.13.0 (pinned), minus
duplicates, and adds four tools: `get_upcoming_deadlines`,
`get_course_assignment_data`, `read_course_document` and
`search_course_document`. It offers tools only: no resources or prompts.

Clients reach it in two ways:

- **Locally (stdio):** Claude Code, Claude Desktop, Codex CLI, IDE and app.
- **Through OpenAI's Secure MCP Tunnel:** ChatGPT (web, desktop, mobile), Work,
  and Codex in the ChatGPT desktop app. The tunnel runs the same local launcher.

There is no hosted service. Each user runs their own copy with their own token.

## Priorities

When goals conflict, the earlier one wins:

1. **Read-only access to Canvas.** Nothing may submit, edit, send or run code.
2. **Thorough coverage with few mistakes.** Cover announcements, assignments and
   due dates, the Inbox, files, pages, modules, discussions and grades. Never
   claim a complete answer (for example "all your assignments") when an error or
   missing access means it may not be; say what couldn't be read.
3. **Fast retrieval.** Let the agent answer in as few tool calls and as little
   waiting as possible.
4. **Easy setup** for Claude and ChatGPT clients.
5. **Token efficiency.** Keep tool output, server instructions and the skill
   compact.

## Layout

| Path | What it is |
| --- | --- |
| `src/canvas_reader/server.py` | Startup, forced read-only settings, server instructions, clean shutdown |
| `src/canvas_reader/assignments.py` | Deadlines and assignment lists, retries, partial results |
| `src/canvas_reader/documents.py` | Safe downloads, in-memory cache, reading and search |
| `src/canvas_reader/extract.py` | PDF, Word, PowerPoint, RTF, HTML and text extraction |
| `src/canvas_reader/common.py` | Links, local times, Canvas error helpers |
| `scripts/start.sh` | Launcher: runs `.venv/bin/canvas-reader` next to it |
| `scripts/launch.sh` | Stable launcher for MCP clients: runs the installed, active version's `start.sh` |
| `scripts/versions.py` | Version manager (install, update, rollback, uninstall, Keychain token). Standard library only, Python 3.9+ |
| `scripts/tunnel.sh` | OpenAI Secure MCP Tunnel helper; runtime key in the macOS Keychain |
| `scripts/smoke.py` | Startup check; `--live` reads a real account (users only) |
| `scripts/build_plugin.py` | Builds the release zip (and, with `--mcpb`, the Claude Desktop extension) from explicit allowlists |
| `skills/canvas-reader/` | Agent Skill (`SKILL.md`, `agents/openai.yaml`, icons) shared by all clients |
| `plugin.json` | Agent Plugins manifest used by ChatGPT and Codex (no server) |
| `.claude-plugin/` | Claude Code plugin manifest (inline server, settings) and marketplace |
| `.agents/plugins/marketplace.json` | Codex plugin marketplace (skill only) |
| `docs/` | `SETUP.md` (Mac guide), `clients/` (Claude, ChatGPT, Codex), `VERIFICATION.md` |
| `dist/` | Every release: `canvas-reader-X.Y.Z.zip`, `.mcpb` and `SHA256SUMS-X.Y.Z`, plus source snapshots (tracked) |
| `.claude/CLAUDE.md` | Imports this file for Claude Code |
| `constraints.txt` | Tested dependency pins |
| `tests/` | Unit and integration tests (no network, no real Canvas) |

## Commands

```zsh
python3 -m venv .venv
.venv/bin/python -m pip install -c constraints.txt -e ".[test]"
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests
.venv/bin/python scripts/build_plugin.py --mcpb     # dist/canvas-reader-<version>.zip and .mcpb
```

- **Startup check without Canvas:** write a temporary env file containing
  `CANVAS_API_URL=https://example.instructure.com/api/v1` and
  `CANVAS_API_TOKEN=dummy`, then run
  `.venv/bin/python scripts/smoke.py --env-file <that file>`.
- **`versions.py` must also pass on Python 3.9:**
  `python3.9 -m unittest discover -s tests -p test_versions.py`
  (use `uv python install 3.9` if needed).
- **`smoke.py --live` needs a real Canvas account.** Never run it yourself; the
  user runs it.

## Rules

- **Read-only, always.** Never expose tools that submit, edit, send messages or
  run code. The server forces this at startup and the tests enforce it.
- **Course data stays in memory.** Nothing is written to disk, logs never contain
  course text, and the cache is wiped when the reader stops.
- **Canvas content is untrusted.** Keep the `UNTRUSTED CANVAS CONTENT` fencing.
- **Keep the tool list lean.** Every session sends each tool's description and
  schema to the model. `tests/test_server.py` maps every tool to a coverage area
  (`COVERAGE`) and caps the list's size (`TOOL_LIST_BUDGET_CHARS`). Remove
  upstream duplicates in `server.REMOVED_TOOLS`; fix upstream wording that names
  tools we don't offer in `server.UPSTREAM_TEXT`. Never remove the only tool for
  an area.
- **Never drop content silently.** When something fails partway, return
  `status: "partial"` with a `notice` first that starts `PARTIAL RESULT` and says
  what is missing.
- **No secrets or personal data, anywhere:**
  - never commit or print tokens, API keys, tunnel IDs, app IDs, personal paths,
    usernames or real course data;
  - use `~` or `$HOME` in docs;
  - don't read `~/.config/canvas-reader/`, Keychain items or
    `~/.config/tunnel-client/`;
  - in tests, use a temporary `HOME` and `CANVAS_READER_HOME`.
- **Don't touch the user's real install from tests or experiments.** That means no
  `versions.py uninstall`, `link-tunnel` or `use` against the real
  `~/.local/share/canvas-reader`.
- **`versions.py`:** standard library only, Python 3.9 compatible, and every
  failure or interruption leaves nothing behind.
- **Dependencies:** `canvas-mcp` is pinned to 1.13.0, and some tests patch its
  internals. If dependencies change, update `constraints.txt` and those tests.
- **Bundle contents:** the zip is built from the allowlists in
  `scripts/build_plugin.py`. Add any new runtime file there and in the packaging
  tests.
- **`dist/` is tracked in git and keeps every release:** each version's zip,
  `.mcpb` and `SHA256SUMS-X.Y.Z`, plus the source snapshots `bump` saves. A
  fresh clone can then install the newest with `versions.py update`, or any
  earlier one with `versions.py install dist/canvas-reader-X.Y.Z.zip`. Never
  delete or rewrite an earlier release's files, and never commit personal
  builds there.
- **Keep changes focused.** Don't refactor unrelated code. Add or update tests for
  every behavior change.
- **Changelog:** record user-visible changes under "Unreleased" in `CHANGELOG.md`.

## Docs style

Plain language, short sentences, Mac-first. Make every command copy-pasteable.
Say what each step does and how to tell it worked. Use no personal paths, names
or account details.

## Release

1. Run `python3 scripts/versions.py bump X.Y.Z`. It saves the outgoing
   version's source to `dist/canvas-reader-<old>-source.zip` and sets the version
   in `__init__.py`, `plugin.json`, `.claude-plugin/plugin.json` and the
   CHANGELOG.
2. Describe the changes in `CHANGELOG.md`, then run the tests.
3. Run `.venv/bin/python scripts/build_plugin.py --release`. It adds this
   version's zip, `.mcpb` and `SHA256SUMS-X.Y.Z` to `dist/`; earlier releases and
   source snapshots stay. Commit them; `--check-dist` confirms they match the
   source.
4. Tag `vX.Y.Z` on that commit and push the tag. The Release workflow checks
   `dist/` against a fresh build and creates a **draft** GitHub release with the
   zip, the `.mcpb` and `SHA256SUMS` (the contents of `SHA256SUMS-X.Y.Z`). A
   person reviews and publishes it.

Rebuild `dist/` (step 3) after any later change to a bundled file, before
tagging.
