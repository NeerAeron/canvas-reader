# Verification: Canvas Reader 0.4.0

These checks used temporary folders, synthetic credentials, a throwaway Claude
Code configuration and a throwaway Codex home. No live Canvas account, installed
version, tunnel profile, Keychain item or client configuration was used.

## Verified

- **Tests:** the full suite (206 tests) passes on Python 3.14 and 3.11 on
  macOS, and the version-manager tests (`tests/test_versions.py`) also pass on
  Python 3.9. GitHub Actions runs the suite on macOS and Ubuntu with Python
  3.11–3.14, plus the 3.9 job and the manifest checks.
- **Read-only:** a real stdio start offers 30 tools, all read-only; write and
  code-execution tools are absent.
- **Coverage after trimming:** every tool belongs to a coverage area (due dates,
  assignments, submissions and feedback, grades, announcements, Inbox, files,
  syllabus, pages, modules, discussions, courses), and each area keeps its
  tools. All 30 tools were called through the real server against a stubbed,
  empty Canvas and answered. No tool description names a tool that isn't
  offered. The tool list the model reads is about 14% smaller than in 0.3.3.
- **Release files:** the zip builds from its allowlist, installs into a temporary
  `CANVAS_READER_HOME` with `versions.py install`, and passes
  `smoke.py --env-file <dummy file>`. The Claude Desktop extension (`.mcpb`)
  passes `mcpb validate` (manifest 0.3).
- **Claude Code plugin:** `claude plugin validate` passes on Claude Code 2.1.118,
  and `claude plugin validate --strict` passes on 2.1.288, for both the
  marketplace and the plugin. Installed from a local copy into a throwaway
  configuration, `claude mcp list` shows its server as connected: the plugin
  runs `scripts/launch.sh`, which starts the installed 0.4.0. Unset plugin
  settings reach the reader as unset (so the credential file applies), and a set
  one (`--config timezone=…`) reaches it as set.
- **Codex:** with Codex CLI 0.160.0, `codex plugin marketplace add` reads
  `.agents/plugins/marketplace.json` before the Claude marketplace file, and the
  installed plugin uses the root `plugin.json` (skill only, no server).
  `codex mcp add` writes the documented `config.toml` entry.
- **Privacy:** no tracked file contains personal paths, usernames, emails,
  connection IDs, keys or real course data, and the PNG icons hold only image
  data.

## Not verified here: your checklist

These need your accounts, so only you can run them.

1. Merge the pull request.
2. Update your install: `python3 scripts/versions.py update`, then `status`.
3. Run the live check from [SETUP.md step 3](SETUP.md#3-check-it).
4. **Claude:**
   - Claude Code: `claude mcp add …` or the plugin; ask "What's due this week?"
   - Claude Desktop: install the `.mcpb` extension (or add the config), restart,
     and ask the same question.
   - A Claude cloud task linked to your Mac: confirm the Canvas tools appear.
5. **ChatGPT:** restart the tunnel and refresh the connection; then test
   ChatGPT, Work, Codex in the ChatGPT desktop app, and dot.
6. **Codex:** CLI or IDE with `codex mcp add …`; ask the same question.
7. **Publish:** publish the draft release, make the repository public, and add
   its description and topics.
8. **Turn on private vulnerability reporting** (GitHub offers it only for public
   repositories): **Settings → Security → Private vulnerability reporting**, or
   `gh api -X PUT repos/NeerAeron/canvas-reader/private-vulnerability-reporting`.

Run the tests yourself:

```zsh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests
```
