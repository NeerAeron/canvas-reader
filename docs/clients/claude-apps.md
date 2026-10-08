# Canvas Reader in Claude

Set up the runtime and your Canvas credentials first: follow
[SETUP.md](../SETUP.md) steps 1–3. Every Claude option below starts that same
installed copy, so it keeps working after `versions.py update`.

| Where you use Claude | How to connect | Section |
| --- | --- | --- |
| Claude Code (terminal, IDE, desktop app's Code tab) | One command, or the plugin | [Claude Code](#claude-code) |
| Claude Desktop (chat) | One-click extension, or a config file | [Claude Desktop](#claude-desktop) |
| Claude cloud tasks linked to your Mac | Through Claude Desktop (should work; verify) | [Cloud tasks](#claude-cloud-tasks-linked-to-your-mac) |
| claude.ai on the web or phone, Claude Code on the web | Not supported | [Why not](#not-supported) |

## Claude Code

Pick **one** of these two options; using both gives you two copies of the tools.

### Option A: one command (simplest)

```zsh
claude mcp add --scope user canvas-reader -- "$HOME/.local/share/canvas-reader/current/scripts/start.sh"
```

`--scope user` makes Canvas Reader available in every project. If your
credential file isn't at `~/.config/canvas-reader/canvas.env`, add
`--env-file /path/to/canvas.env` at the end of the command.

Check it:

```zsh
claude mcp list
```

`canvas-reader` should show as connected. Inside a session, `/mcp` shows the
same.

Optionally add the Canvas Reader skill, which teaches Claude the best tool for
each question. The link follows updates:

```zsh
mkdir -p ~/.claude/skills
ln -s ~/.local/share/canvas-reader/current/skills/canvas-reader ~/.claude/skills/canvas-reader
```

### Option B: the plugin

The plugin bundles the server connection and the skill. In Claude Code, run:

```text
/plugin marketplace add NeerAeron/canvas-reader
/plugin install canvas-reader@canvas-reader
```

The same from your shell:

```zsh
claude plugin marketplace add NeerAeron/canvas-reader
claude plugin install canvas-reader@canvas-reader
```

The plugin asks for three optional settings. Leave them blank if your
credential file is set up:

| Setting | What it is |
| --- | --- |
| Canvas address | Your school's Canvas address, such as `https://your-school.instructure.com` |
| Canvas access token | Kept in Claude Code's secure storage, not in a settings file |
| Time zone | For due dates, such as `America/New_York` |

Change them later with `/plugin configure canvas-reader@canvas-reader`.

**Which value wins:** a value in the credential file wins over the plugin
setting. Blank or missing lines in the file fall through to the plugin settings.
If no token is set anywhere, Canvas Reader uses one saved in your Keychain
(`python3 scripts/versions.py token store`).

The plugin starts the installed runtime through `scripts/launch.sh`; it doesn't
contain its own copy. Update the runtime with `versions.py update` as usual, and
the plugin with `/plugin` (or `claude plugin marketplace update canvas-reader`).

### Try it

Start a new session and ask "What's due this week?" Claude should call
`get_upcoming_deadlines` once and answer with due times, status and links.

## Claude Desktop

### Option A: one-click extension

1. Download `canvas-reader-X.Y.Z.mcpb` from the
   [latest release](https://github.com/NeerAeron/canvas-reader/releases/latest)
   (a clone also has it in `dist/`).
2. Double-click it. Claude Desktop opens an install dialog.
3. Fill in the settings, or leave them blank to use your credential file, and
   install.

The extension holds only a small launcher; it starts the runtime you installed
in SETUP. Claude Desktop keeps a sensitive setting such as the token in its own
secure storage.

### Option B: the config file

Claude Desktop reads
`~/Library/Application Support/Claude/claude_desktop_config.json`. It needs the
full path to the launcher; `~` and `$HOME` don't work there.

1. Print the entry with your real path:

   ```zsh
   python3 -c 'import json, os; print(json.dumps({"mcpServers": {"canvas-reader": {"command": os.path.expanduser("~/.local/share/canvas-reader/current/scripts/start.sh")}}}, indent=2))'
   ```

   It looks like this, with your user name in place of `<you>`:

   ```json
   {
     "mcpServers": {
       "canvas-reader": {
         "command": "/Users/<you>/.local/share/canvas-reader/current/scripts/start.sh"
       }
     }
   }
   ```

2. Open the file: in Claude Desktop choose **Settings → Developer → Edit Config**,
   or run:

   ```zsh
   open -e "$HOME/Library/Application Support/Claude/claude_desktop_config.json"
   ```

   If the file is empty or doesn't exist, paste the whole entry. If it already
   has `"mcpServers"`, add only the `"canvas-reader": { … }` part inside it,
   with a comma between entries.
3. Quit Claude completely (Claude → Quit Claude) and open it again.
4. Check **Settings → Connectors**: Canvas Reader should be listed and enabled.

### Try it

Ask "What's due this week?" in a new chat. Claude asks for permission the first
time it uses a tool.

## Claude cloud tasks linked to your Mac

The Claude app can relay MCP servers that are set up in Claude Desktop on your
Mac to cloud tasks that are linked to that Mac. Anthropic doesn't document this
publicly, so treat it as **should work; verify**:

1. Set up Claude Desktop as above and leave it running, with the Mac awake.
2. Start a cloud task linked to this Mac.
3. Check that the Canvas Reader tools appear, then ask "What's due this week?"

## Not supported

- **claude.ai on the web and phone (custom connectors).** These connect only to
  remote HTTPS servers. Canvas Reader runs on your Mac, and putting it on the
  internet would expose your Canvas account. There is no hosted version.
- **Claude Code on the web.** It runs in Anthropic's cloud. It would need your
  Canvas token stored in a cloud environment, with your school's Canvas address
  allowed on its network. Canvas Reader is designed to keep the token on your
  Mac.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| "Canvas Reader is not installed yet" | Install the runtime: [SETUP.md](../SETUP.md) step 1. |
| Not connected, or "failed" in `/mcp` | Run `~/.local/share/canvas-reader/current/scripts/start.sh --check`; it names the problem. |
| Claude Desktop shows an error for Canvas Reader | Read `~/Library/Logs/Claude/mcp-server-canvas-reader.log`. |
| Still the old version after an update | Restart Claude Code or quit and reopen Claude Desktop. |
| `auth_error` | Make a new Canvas token and put it in the credential file (or plugin setting). |

## Remove

- Option A in Claude Code: `claude mcp remove --scope user canvas-reader`, and
  `rm ~/.claude/skills/canvas-reader` (this removes only the link).
- The plugin: `/plugin uninstall canvas-reader@canvas-reader`, then
  `/plugin marketplace remove canvas-reader`.
- Claude Desktop: remove the extension in **Settings → Extensions**, or delete the
  `canvas-reader` entry from the config file. Then quit and reopen Claude.
