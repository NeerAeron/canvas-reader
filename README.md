# Canvas Reader

A lean, fast, read-only way for AI assistants to read everything in a student's
own Canvas: deadlines, assignments, grades, announcements, the Inbox, syllabi,
pages, modules, discussions and course files. It runs on your Mac with your own
Canvas token and works with Claude, ChatGPT and Codex.

```text
Claude Code, Claude Desktop, Codex ───────────────────────► Canvas Reader on your Mac ─► your Canvas
ChatGPT (web, desktop, phone), Work ─► Secure MCP Tunnel ─┘
```

Canvas Reader builds on [canvas-mcp](https://github.com/vishalsachdev/canvas-mcp)
1.13.0 (pinned). What's different:

- **Student-facing and read-only, always.** It exposes only canvas-mcp's student
  read tools. Every tool that submits, edits, sends messages or runs code is
  removed at startup, whatever the settings say.
- **Complete, or it says so.** When Canvas or a file fails partway, the answer is
  marked `PARTIAL RESULT` and says what's missing. It never presents part of
  your assignments as all of them.
- **Fast and lean.** "What's due?" across every course is one call. Long files
  are read in chunks and kept in memory for 15 minutes, so follow-up questions
  don't download them again. Instructions and output are kept compact to save
  your assistant's context.
- **Non-invasive.** Nothing is written to disk, there's no telemetry, and the
  assistant is told to ask before opening Canvas in a browser.

## What it reads

30 read-only tools: canvas-mcp's student read tools (courses, assignments and
your submissions with feedback, grades, announcements, discussions, Inbox
conversations, pages, modules, files, to-do items, peer reviews, your profile)
plus four of its own. Upstream tools that only repeat these less accurately are
left out, which keeps the list your assistant reads short. Canvas Reader's own
tools:

| Tool | What it does |
| --- | --- |
| `get_upcoming_deadlines` | What's due in the next N days (default 7) across all active courses, plus recent overdue work, with local times, status and links, in one call. |
| `get_course_assignment_data` | One course's full assignment list, including undated work, with your own due dates and submission status. |
| `read_course_document` | Text of a PDF, Word, PowerPoint, HTML, RTF or text file (up to 25 MiB), in chunks with page or slide markers. |
| `search_course_document` | Find a phrase in a course file and jump to it. |

Word files include content controls, tables, text boxes, headers, footers,
footnotes, comments, charts and SmartArt. PowerPoint files include tables,
charts, speaker notes, comments and hidden slides. Images aren't read (no OCR);
they're counted in a warning, as are scanned PDF pages.

Try: "What's due this week?", "Summarize this week's lecture slides in
Biology", "What does the syllabus say about late work?", "Any new
announcements?"

## Privacy and your data

- Your Canvas token stays on your Mac: in a private credential file, your macOS
  Keychain, or Claude's secure storage. It never goes into the release files or
  tool output.
- Downloaded course text stays in memory, expires after 15 minutes and is wiped
  when the reader stops. Nothing is written to disk. Logs hold warnings and
  errors, not course text.
- What a tool returns (course content, Inbox messages, grades) goes to the
  assistant and model provider you use. Check their data policies.
- Course content is labeled as untrusted, and the assistant is told to treat it
  as data, never as instructions.

[SECURITY.md](SECURITY.md) has the full threat model and how to report a problem.

## Works with

| Client | Support | Guide |
| --- | --- | --- |
| Claude Code (terminal, IDE, desktop app) | Yes: one command, or the plugin | [Claude](docs/clients/claude-apps.md#claude-code) |
| Claude Desktop | Yes: one-click extension, or the config file | [Claude](docs/clients/claude-apps.md#claude-desktop) |
| Claude cloud tasks linked to your Mac | Should work through Claude Desktop; verify | [Claude](docs/clients/claude-apps.md#claude-cloud-tasks-linked-to-your-mac) |
| ChatGPT on the web, desktop and phone | Yes, through OpenAI's Secure MCP Tunnel | [ChatGPT](docs/clients/chatgpt.md) |
| ChatGPT Work | Yes, through the tunnel | [ChatGPT](docs/clients/chatgpt.md) |
| Codex in the ChatGPT desktop app | Yes, through the tunnel or directly | [ChatGPT](docs/clients/chatgpt.md), [Codex](docs/clients/codex.md) |
| dot | Not confirmed; may work through the tunnel | [ChatGPT](docs/clients/chatgpt.md#6-test-where-you-use-it) |
| Codex CLI and IDE extension | Yes: one command | [Codex](docs/clients/codex.md) |
| claude.ai on the web and phone | No: custom connectors reach only remote HTTPS servers | [Why](docs/clients/claude-apps.md#not-supported) |
| Claude Code on the web | No: it would need your token in the cloud | [Why](docs/clients/claude-apps.md#not-supported) |
| Codex cloud | No: MCP isn't available in cloud tasks (the skill is) | [Why](docs/clients/codex.md#codex-cloud-not-supported) |

## Quick start

You need a Mac, Python 3.11 or newer (`python3 --version`; get it from
[python.org](https://www.python.org/downloads/) or `brew install python`), git,
and a Canvas access token (Canvas → Account → Settings → New access token).

1. **Get the code and install the runtime.** This creates its own Python
   environment under `~/.local/share/canvas-reader`, checks that it starts, and
   makes it active.

   ```zsh
   git clone https://github.com/NeerAeron/canvas-reader.git ~/canvas-reader
   cd ~/canvas-reader
   python3 scripts/versions.py install .
   ```

2. **Add your Canvas credentials** to `~/.config/canvas-reader/canvas.env`:
   [SETUP.md step 2](docs/SETUP.md#2-canvas-credentials).
3. **Check it** against your Canvas account:
   [SETUP.md step 3](docs/SETUP.md#3-check-it).
4. **Connect your assistant:** [Claude](docs/clients/claude-apps.md),
   [ChatGPT](docs/clients/chatgpt.md) or [Codex](docs/clients/codex.md).

[docs/SETUP.md](docs/SETUP.md) is the full Mac guide, including installing from
a downloaded release instead of git.

## Updating and removing

```zsh
cd ~/canvas-reader
git pull
python3 scripts/versions.py update     # installs the newer version, checks it, switches to it
python3 scripts/versions.py rollback   # switches back if something is wrong
```

Without git, `python3 scripts/versions.py update --github` downloads the newest
release and checks it against its published checksums. Restart your assistant
(or the tunnel) after updating.

To remove everything, see
[SETUP.md → Remove everything](docs/SETUP.md#remove-everything).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Report security problems privately as
described in [SECURITY.md](SECURITY.md).

## License and disclaimer

MIT License, © 2026 Neer Aeron. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Not affiliated with or endorsed by Instructure, OpenAI or Anthropic. Canvas is a
trademark of Instructure.
