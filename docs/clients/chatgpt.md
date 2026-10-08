# Canvas Reader in ChatGPT

ChatGPT runs in OpenAI's cloud, so it reaches the reader on your Mac through
OpenAI's **Secure MCP Tunnel**. The tunnel client on your Mac connects out to
OpenAI and starts Canvas Reader when ChatGPT needs it. Nothing listens on your
network, and your Canvas token stays on the Mac.

```text
ChatGPT (web, desktop, mobile), Work, Codex in the ChatGPT app
  └─► Secure MCP Tunnel ─► tunnel client on your Mac ─► Canvas Reader ─► your Canvas
```

One connection covers ChatGPT on the web, desktop and phone, **Work**, and
**Codex in the ChatGPT desktop app**. dot may also use it; see
[Test where you use it](#6-test-where-you-use-it).

Before you start:

- Set up the runtime and your Canvas credentials:
  [SETUP.md](../SETUP.md) steps 1–3.
- Run the commands below from your clone folder (`cd ~/canvas-reader`).
- **Developer mode is required.** Its availability depends on your plan and on
  your workspace's policy, and OpenAI's pages describe it differently. Check
  under **Settings → Security and login** in ChatGPT before you begin.

## 1. Install the tunnel client (once)

```zsh
brew install openai/tools/tunnel-client
tunnel-client --version
```

The Homebrew version is notarized by Apple. The zip downloads on OpenAI's
release page aren't, so macOS may block them. `scripts/tunnel.sh` finds the
client on your `PATH`. To use a copy elsewhere, set `TUNNEL_CLIENT_BIN` to its
full path.

## 2. Save the OpenAI runtime key (once)

The tunnel client signs in to OpenAI with a **runtime** API key (not an admin
key) from your Platform organization's
[API keys page](https://platform.openai.com/settings/organization/api-keys).
Store it in your login Keychain:

```zsh
scripts/tunnel.sh --store-key
```

Paste the key at both hidden prompts. `--run` and `--doctor` load it from the
Keychain, and `--forget-key` removes it. Never paste it into a chat or a plugin
file. An exported `CONTROL_PLANE_API_KEY` takes priority if you prefer your own
secret manager.

## 3. Create the tunnel profile (once)

1. In [Platform tunnel settings](https://platform.openai.com/settings/organization/tunnels),
   create a tunnel and copy its ID (it starts with `tunnel_`).
2. **Link the tunnel only to your personal ChatGPT workspace.** Anyone who can
   use the tunnel can read your Canvas account. Running it needs Tunnels
   **Read + Use**; editing it needs **Read + Manage**. New roles can take about
   30 minutes to apply.
3. Create the profile, replacing the placeholder with your ID:

   ```zsh
   scripts/tunnel.sh --setup tunnel_XXXXXXXX
   ```

   Setup should say the profile "will run the installed version and follow
   updates". It writes `~/.config/tunnel-client/canvas-reader.yaml`; it doesn't
   create a tunnel or a ChatGPT connection.

If you set up the profile before installing the runtime, or with an older
version, point it at the installed version once:

```zsh
python3 scripts/versions.py link-tunnel --dry-run
python3 scripts/versions.py link-tunnel
```

The first command shows the change; the second makes it and keeps a backup next
to the profile.

## 4. Run the tunnel

```zsh
scripts/tunnel.sh --doctor
caffeinate -i scripts/tunnel.sh --run
```

`--doctor` should pass. Leave `--run` going in its own Terminal window while you
use ChatGPT. `caffeinate -i` stops idle sleep, but closing a MacBook's lid
still sleeps it unless it's plugged in with an external display.

Control-C stops the tunnel and the reader, and the reader wipes the course text
it held in memory. The tunnel client's status page is
[127.0.0.1:8080/ui](http://127.0.0.1:8080/ui); that address isn't the server URL
for ChatGPT.

Running the tunnel in the background at login (with launchd) isn't covered by
this guide.

## 5. Connect ChatGPT (once)

1. In ChatGPT, turn on **Settings → Security and login → Developer mode**.
2. Open [Plugins](https://chatgpt.com/plugins), select **+**, then **Add MCP
   server**:
   - Name: **Canvas Reader**
   - Description: *Read Canvas courses, deadlines, assignments, submission
     status, syllabi, lecture slides and documents, with links to the original
     Canvas sources.*
   - Connection: **Tunnel**, then pick your tunnel (or enter its ID).
   - Authentication, if asked: **None**. The tunnel controls access, and the
     reader uses the token on your Mac.
3. Create it, then check the tool list: `get_upcoming_deadlines`,
   `get_course_assignment_data`, `read_course_document` and
   `search_course_document` are there, and nothing submits or edits. If the form
   offers icons, upload `assets/icon-light.png` and `assets/icon-dark.png`.
4. Open **Personal → Canvas Reader** and install it.
   ([OpenAI's guide](https://developers.openai.com/plugins/deploy/connect-chatgpt))

Already connected from an earlier version? Don't create a second connection.
Restart the tunnel, open the connection in Plugins, select **Refresh**, and
start a new chat.

## 6. Test where you use it

With the tunnel running, start a new chat in each place you use:

| Where | Ask | You should see |
| --- | --- | --- |
| ChatGPT (web, desktop, phone) | "Use Canvas Reader: what's due in the next 7 days?" | One `get_upcoming_deadlines` call; due times with weekday and time zone, status and links |
| Work | Type `@`, pick Canvas Reader, ask the same | The same answer |
| Codex in the ChatGPT desktop app | "Use Canvas Reader to list what's due this week" | The same answer |
| dot | "Use Canvas Reader to list my upcoming assignments. Don't open Canvas in a browser." | Not confirmed: dot can use plugins enabled on your account, but tunnel plugins haven't been tested. ([dot and apps](https://learn.chatgpt.com/docs/dots/computers-and-apps)) |

Then try an assignment's instructions, a syllabus grading policy, a slide deck
summary and "find late work in the syllabus PDF". A tool list alone doesn't
prove live access; check that real answers come back.

Codex in the ChatGPT desktop app can also start Canvas Reader directly, with no
tunnel: see [codex.md](codex.md).

## Updating

After `python3 scripts/versions.py update`, restart the tunnel (Control-C, then
`caffeinate -i scripts/tunnel.sh --run`). Until then the running reader keeps
the old version, and `versions.py status` points that out. If the tool list
changed, select **Refresh** on the connection in ChatGPT.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| "Install tunnel-client" | `brew install openai/tools/tunnel-client` |
| "No OpenAI runtime key" | `scripts/tunnel.sh --store-key` |
| Doctor passes but running fails to sign in | The key is invalid or lacks tunnel access; check its organization and permissions. |
| Tunnel missing in ChatGPT | Check the workspace link, Tunnels Read + Use, and developer mode. |
| Calls time out | The Mac is asleep or the tunnel stopped; restart `--run`. |
| Old tools still listed | Restart the tunnel, then **Refresh** the connection in ChatGPT. |
| Answers come from an old version | Restart the tunnel; `python3 scripts/versions.py status` shows what's running. |
| The reader won't start | Run `~/.local/share/canvas-reader/current/scripts/start.sh --check`; it names the problem. |

## Remove

1. Stop the tunnel (Control-C).
2. `scripts/tunnel.sh --forget-key` removes the runtime key from your Keychain.
3. Delete the profile `~/.config/tunnel-client/canvas-reader.yaml` and its
   `.bak-*` backups.
4. In ChatGPT, delete the Canvas Reader connection. In Platform, delete the
   tunnel and the runtime key.
