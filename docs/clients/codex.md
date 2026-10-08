# Canvas Reader in Codex

Codex starts Canvas Reader directly on your Mac; no tunnel is needed. The Codex
CLI, the IDE extension and Codex in the ChatGPT desktop app share one settings
file, `~/.codex/config.toml`, so setting it up once covers all three.

Set up the runtime and your Canvas credentials first: follow
[SETUP.md](../SETUP.md) steps 1–3.

## 1. Add the server

```zsh
codex mcp add canvas-reader -- "$HOME/.local/share/canvas-reader/current/scripts/start.sh"
codex mcp list
```

`canvas-reader` should be listed as enabled. If your credential file isn't at
`~/.config/canvas-reader/canvas.env`, add `--env-file /path/to/canvas.env` at the
end of the first command.

The command writes this to `~/.codex/config.toml`, with your user name in place
of `<you>`. Add the `startup_timeout_sec` line yourself: the reader usually
starts in about a second, but the first start after an update can be slower than
Codex's default limit.

```toml
[mcp_servers.canvas-reader]
command = "/Users/<you>/.local/share/canvas-reader/current/scripts/start.sh"
startup_timeout_sec = 30
```

Prefer the app? In Codex in the ChatGPT desktop app, open **Settings → MCP
servers → Add server**, enter `canvas-reader`, choose **STDIO**, paste the full
command path, save and restart. In the IDE extension, the same form is under the
gear menu → **MCP servers**.

## 2. Add the skill (optional)

The skill teaches Codex which tool fits each question. Either link it, which
follows updates:

```zsh
mkdir -p ~/.agents/skills
ln -s ~/.local/share/canvas-reader/current/skills/canvas-reader ~/.agents/skills/canvas-reader
```

Or install it as a plugin from this repository's marketplace:

```zsh
codex plugin marketplace add NeerAeron/canvas-reader
codex plugin add canvas-reader@canvas-reader
```

You can also browse it with `/plugins` inside Codex. The Codex plugin contains
only the skill; the server comes from step 1. OpenAI's documentation doesn't yet
confirm that local servers bundled inside plugins work in Codex, so the plugin
doesn't include one.

## 3. Try it

Start a new Codex session and ask "What's due this week?" Codex should call
`get_upcoming_deadlines` once and answer with due times, status and links.

## Codex cloud: not supported

Codex cloud tasks run in OpenAI's containers. MCP servers aren't documented
there, and Canvas Reader has to run on your Mac to keep your token there. Cloud
tasks can read the skill's instructions but have no Canvas tools.

## Updating

After `python3 scripts/versions.py update`, start a new Codex session; it runs
the new version automatically. Update the plugin with
`codex plugin marketplace upgrade`.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| The server fails to start or times out | Run `~/.local/share/canvas-reader/current/scripts/start.sh --check`; it names the problem. Check `startup_timeout_sec`. |
| "Canvas Reader is not installed yet" | Install the runtime: [SETUP.md](../SETUP.md) step 1. |
| `auth_error` | Make a new Canvas token and put it in the credential file. |

## Remove

```zsh
codex mcp remove canvas-reader
rm ~/.agents/skills/canvas-reader
codex plugin remove canvas-reader@canvas-reader
codex plugin marketplace remove canvas-reader
```

`rm` removes only the link. Skip any line for something you didn't add.
