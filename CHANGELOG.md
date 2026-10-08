# Changelog

## 0.4.0 (2026-10-02)

The first public release: one runtime on your Mac, with setup guides for
Claude, ChatGPT and Codex.

### New

- **Claude Code plugin** in `.claude-plugin/`, installed with
  `/plugin marketplace add NeerAeron/canvas-reader` and
  `/plugin install canvas-reader@canvas-reader`. It declares the server inline
  (no root `.mcp.json`), starts the installed runtime, includes the skill, and
  has optional settings for the Canvas address, token (secure storage) and time
  zone. It validates with older and current Claude Code.
- **Claude Desktop extension**: `canvas-reader-X.Y.Z.mcpb` (MCP Bundle manifest
  0.3), built with `build_plugin.py --mcpb` from the same settings.
- **Codex plugin marketplace** in `.agents/plugins/marketplace.json` for the
  skill (`codex plugin marketplace add NeerAeron/canvas-reader`). Codex reads it
  before the Claude marketplace file and uses the root `plugin.json`, which
  starts no server.
- **Client guides**: `docs/clients/claude-apps.md` (Claude Code, Claude Desktop, cloud
  tasks linked to your Mac), `docs/clients/chatgpt.md` (Secure MCP Tunnel for
  ChatGPT, Work, Codex in the ChatGPT app and dot) and `docs/clients/codex.md`.
  Each says which clients aren't supported and why.
- `scripts/launch.sh` starts the installed, active version for any MCP client,
  so client settings survive updates. It prints one clear line if nothing is
  installed, and ignores empty or unexpanded client settings.
- `versions.py update` in a git clone installs the clone's own source when it
  is newer than every release zip: `git pull`, then `versions.py update`.
- `versions.py update --github` downloads the newest published GitHub release,
  checks it against the release's `SHA256SUMS`, then installs it.
- The Canvas token can live in the macOS Keychain instead of the credential
  file: `versions.py token store` (also `forget` and `status`). The reader uses
  it only when neither the credential file nor the client sets a token.
  `uninstall --credentials` removes it too.
- New original icon: a stack of course pages with a Canvas-red bookmark and a
  check, in `assets/icon.svg`, with 512 px light and dark PNGs. The red ties it
  to Canvas without using Instructure's emblem; the previous logos, which were
  based on that emblem, are removed. The skill carries its own copy for Codex.
- `SECURITY.md` (private reporting and the threat model) and `CONTRIBUTING.md`.

### Changed

- **Leaner tool list:** 30 tools instead of 34. Four upstream tools that only
  repeated Canvas Reader's, less accurately, are gone: `list_assignments` (raw
  UTC dates), `get_my_submission_status` (older status rules),
  `get_course_content_overview` (a preview often taken for the syllabus) and
  `get_page_details` (a 500-character preview). Upstream resource templates and
  the `summarize-course` prompt, which repeated tools, are gone too. Upstream
  descriptions that named tools the server doesn't offer are reworded. The
  model now reads about 14% less about the tools in every session. Tests map
  every tool to a coverage area and cap the list's size.
- The default time zone is `America/Los_Angeles` (it was `UTC`). Set
  `TIMEZONE` in the credential file for another one.
- The README is rewritten for new users, with a "Works with" table for every
  client. `docs/SETUP.md` is now the Mac guide: install, credentials, check,
  updates, troubleshooting and removal.
- The server instructions and skill no longer favor the tunnel: when more than
  one Canvas Reader connection is available, the assistant uses the one you
  prefer and says which it used.
- `tunnel.sh --setup` points a new profile straight at the installed version,
  so `link-tunnel` is only needed for older profiles. Its "missing client"
  message recommends `brew install openai/tools/tunnel-client`.
- `versions.py bump` also sets the version in `.claude-plugin/plugin.json`.
- After an install, `versions.py` points to the client guides instead of
  `link-tunnel`, and its restart hint covers Claude and Codex as well as the
  tunnel.
- Tracing in the upstream MCP library is forced off, alongside its update check.
- `plugin.json` has `homepage` and `repository`. `CLAUDE.md` moved to
  `.claude/CLAUDE.md` so it isn't mistaken for plugin content.
- Releases: `dist/` (tracked in git) keeps every release, with each version's
  zip, `.mcpb` and `SHA256SUMS-X.Y.Z`, plus the source snapshots that
  `versions.py bump` saves. A clone can install the newest with
  `versions.py update`, or any earlier one, offline. `build_plugin.py --release`
  adds a version's files and `--check-dist` checks them.
- CI on macOS and Linux with Python 3.11–3.14 runs the tests and builds the
  release files; Python 3.9 runs the version-manager tests; the Claude plugin
  and the `.mcpb` are validated. A `v*` tag creates a draft GitHub release.

### Fixed

- A blank value in the credential file (such as `CANVAS_API_TOKEN=`) no longer
  erases the same setting passed by the client.

### Removed

- The `portable`, `openai` and `workflow` build targets, the
  `scripts/local-backup.sh` helper, `mcp.json`, `config/local-mcp.json`, the
  Canvas-like icons and the legacy docs (`USAGE`, `TUNNEL`, `LOCAL-INSTALL`,
  `WORKFLOW`, `REGISTRATION`, `docs/archive/`). Git history keeps them.

## 0.3.3 (2026-10-02)

- Neer Aeron is listed as author in `plugin.json`, `pyproject.toml` and `LICENSE`.
- Setup no longer assumes a particular Mac, folder or credential file, and
  `.env.example` uses the neutral `UTC` timezone default. Set `TIMEZONE` for local
  deadlines.
- `.cache/` is ignored by git; verification reports no longer describe a real
  account's courses, and the docs no longer include a specific tunnel ID.
- Public documentation omits personal installation and device details. Setup
  covers source checkouts and downloaded bundles; privacy text names the chosen
  client and model provider and accurately describes in-memory cache expiry.
- Icons retain their artwork with image provenance removed. ZIPs use neutral
  timestamps and exclude local runtime and planning files.
- Release builds reject personal home paths, connection IDs, literal credentials
  and image metadata. Author information is preserved, and supporting setup
  documents are included in the bundle.

## 0.3.2 (2026-10-02)

- Recovery pagination follows Canvas continuation links instead of guessing
  completeness from page length.
- RTF emoji are decoded safely; missing referenced Office parts are reported.
- Embedded Word documents share an extraction budget.
- Failed version activation restores the previous version and state.
- Uninstall refuses folders containing unrelated files, but removes
  interrupted-install leftovers and Finder `.DS_Store` files.
- Setup instructions cover an existing credential file and the manual local
  backup.

## 0.3.1 (2026-10-02)

### New

- `scripts/versions.py` installs each version in its own folder
  (`~/.local/share/canvas-reader/versions/X.Y.Z`) with its own Python
  environment. A new version becomes active only after it starts correctly, and
  `rollback` switches back in one step. `update` installs the newest bundle from
  `dist/`; `status` shows what's active, what the tunnel runs and which readers
  need a restart. `link-tunnel` points the tunnel profile at the active version
  (with a backup), and `uninstall` removes everything it installed. It uses only
  the Python standard library and supports Python 3.9+. The server needs 3.11+.
- Partial results instead of failures. When Canvas or a file fails partway, the
  tools return what loaded, with `status: "partial"` and a `notice` first that
  starts `PARTIAL RESULT` and says what failed and what may be missing. The
  instructions tell the assistant to say the answer is incomplete.
  - Assignment and course lists are retried once, then loaded page by page, so
    every page that works is kept.
  - A damaged PDF page, slide, chart, diagram, header, footnote or comment part
    is skipped and named; the rest of the file is read. Damaged XML is repaired
    where possible and reported.

### Fixed

- Stopping the reader (Control-C, the tunnel stopping, or the client
  disconnecting) exits within a second without a traceback, even mid-way
  through a long PDF. 0.3.0 printed a long traceback on Control-C.
- One unreadable slide no longer loses the whole deck.

### Changed

- Course text is held in memory only and wiped when the reader stops; nothing is
  written to disk. canvas-mcp's audit logs and fastmcp's update check are off.
- Quieter logs: warnings and errors only. Set `LOG_LEVEL=INFO` in the credential
  file for more.
- SETUP now installs through `versions.py`, and Codex runs
  `~/.local/share/canvas-reader/current/scripts/start.sh`.

## 0.3.0 (2026-10-02)

### New

- `get_upcoming_deadlines`: what's due across all active courses in one call,
  with recent overdue work, local due times (weekday and time zone), status and
  links. It replaces upstream `get_my_upcoming_assignments`, which had no links,
  IDs or overdue work.
- PowerPoint files: slide text, tables, charts, SmartArt, speaker notes, comments
  and hidden slides. RTF files are also read.
- `search_course_document`: find a phrase in a course file and jump to it.
- Credentials load automatically from `~/.config/canvas-reader/canvas.env`.
- `scripts/tunnel.sh --store-key` keeps the OpenAI runtime key in the macOS
  Keychain; `--run` and `--doctor` load it, and `--forget-key` removes it.
- `constraints.txt` pins the tested dependency versions.

### Fixed

- Word files are read completely. Text in content controls was silently
  dropped; headers, footers, footnotes, endnotes, comments, text boxes, charts
  and SmartArt were only flagged as unread. All are now included.
- Submission status: graded-but-never-submitted work no longer counts as
  completed, and excused or on-paper work no longer looks unfinished. A new
  `submission.status` field says which case applies.
- Due dates come with `due_at_local` and `due_display`, so the assistant doesn't
  convert UTC itself.
- Continuing or searching a document reuses its text for 15 minutes instead of
  downloading and parsing it again. Long PDFs keep going across calls instead of
  stopping at page 1,000 or timing out.
- Document text is wrapped in the same untrusted-content markers as the other
  tools.
- `.env.example` no longer suggests a `.env` file in the project, which was never
  read. Broken doc pointers, the setuptools floor and the User-Agent version are
  fixed.

### Changed

- Removed `search_canvas_tools` and the code-API resource, which advertised
  disabled TypeScript features.
- A credential file now overrides stray shell variables. The reader drops the
  tunnel's OpenAI key from its environment and warns if the credential file is
  readable by other users. An expired token produces a clear `auth_error`.
- Rewrote and shortened the server instructions and skill. The time zone comes
  from your settings.
- python-docx is no longer a runtime dependency; Word and PowerPoint XML is read
  directly with lxml. `pypdf[crypto]` is declared for encrypted PDFs.
- Icons resized to 512 px. Docs condensed into README, SETUP and VERIFICATION.

Building this release installed, registered and changed nothing in any account.
The bundle contains no connection IDs, keys or tokens.

## 0.2.0 (2026-10-01)

A single bundle: tunnel access by default with an opt-in local stdio backup.
