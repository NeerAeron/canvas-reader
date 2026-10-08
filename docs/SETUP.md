# Set up Canvas Reader on your Mac

This guide installs the Canvas Reader runtime, adds your Canvas credentials and
checks the connection. Then you connect the assistants you use.

```text
Your clone:   ~/canvas-reader                        the code, scripts and release files
Installed:    ~/.local/share/canvas-reader/current   the active version; every app runs this
Credentials:  ~/.config/canvas-reader/canvas.env     read automatically
```

Your credentials and the installed versions live outside the clone, so you can
update or move the clone freely.

## 1. Install the runtime

You need Python 3.11 or newer and an internet connection. Check with
`python3 --version`; if it's older, install a newer one from
[python.org](https://www.python.org/downloads/) or with `brew install python`.
The installer finds it automatically.

**From git (recommended):**

```zsh
git clone https://github.com/NeerAeron/canvas-reader.git ~/canvas-reader
cd ~/canvas-reader
python3 scripts/versions.py install .
```

**From a downloaded release:** download `canvas-reader-X.Y.Z.zip` from the
[releases page](https://github.com/NeerAeron/canvas-reader/releases), then:

```zsh
mkdir -p ~/canvas-reader
cd ~/canvas-reader
unzip ~/Downloads/canvas-reader-X.Y.Z.zip
python3 scripts/versions.py install ~/Downloads/canvas-reader-X.Y.Z.zip
```

The unzipped folder gives you the scripts; the zip itself is what gets installed.

Installing creates the version's own Python environment, checks that it starts,
and only then makes it active. It worked if the last line reads
`Installed and checked X.Y.Z (30 tools). Active: X.Y.Z.` A failed install
changes nothing.

## 2. Canvas credentials

Create the credential file once, from your clone folder:

```zsh
mkdir -p ~/.config/canvas-reader
cp -n .env.example ~/.config/canvas-reader/canvas.env
chmod 600 ~/.config/canvas-reader/canvas.env
open -e ~/.config/canvas-reader/canvas.env
```

`cp -n` never overwrites a file you already have. Fill in:

| Setting | What to put |
| --- | --- |
| `CANVAS_API_URL` | Your school's Canvas address, such as `https://your-school.instructure.com` |
| `CANVAS_API_TOKEN` | A token from Canvas → Account → Settings → **New access token** |
| `TIMEZONE` | Your time zone, such as `America/New_York` or `Europe/London`. Due dates are shown in it. The default is `America/Los_Angeles`. |

To find your Mac's time zone name, run:

```zsh
readlink /etc/localtime | sed 's|.*zoneinfo/||'
```

**Optional: keep the token in your Keychain** instead of the file. Run this, paste
the token at both hidden prompts, then leave `CANVAS_API_TOKEN` blank in the
file:

```zsh
python3 scripts/versions.py token store
```

**Which value wins:** a value in the credential file wins over a client's
setting (such as the Claude plugin's), and blank lines in the file are ignored.
A token that is still missing comes from the Keychain.

To keep the file somewhere else, pass `--env-file /path/to/canvas.env` in the
client's command, or set `CANVAS_ENV_FILE` for the tunnel.

## 3. Check it

```zsh
python3 scripts/versions.py status
~/.local/share/canvas-reader/current/.venv/bin/python ~/.local/share/canvas-reader/current/scripts/smoke.py --live
```

`status` should show your version as active and the credential file as
private. The live check signs in to Canvas and reads a few items. It prints one
line of counts and statuses only, never course content. It worked if you see
`"authentication": "passed"` and `"course_discovery": "passed"`. A `partial`
status means something couldn't be read completely.

## 4. Connect your assistants

| Assistant | Guide |
| --- | --- |
| Claude Code, Claude Desktop, Claude cloud tasks linked to your Mac | [clients/claude-apps.md](clients/claude-apps.md) |
| ChatGPT (web, desktop, phone), Work, Codex in the ChatGPT app | [clients/chatgpt.md](clients/chatgpt.md) |
| Codex CLI, IDE extension and the ChatGPT app's Codex settings | [clients/codex.md](clients/codex.md) |

## Versions and updates

**From git:**

```zsh
cd ~/canvas-reader
git pull
python3 scripts/versions.py update
```

`update` installs the newest version it can find (a release zip in `dist/`, or
your clone's own source when that is newer), checks it, and switches to it.

**Without git:** `python3 scripts/versions.py update --github` downloads the
newest published release, checks it against the release's `SHA256SUMS`, and
installs it.

After switching, restart what runs the reader: restart the tunnel, start a new
Claude Code or Codex session, or quit and reopen Claude Desktop. Until then a
running reader keeps the old version, and `status` points that out.

| To | Run |
| --- | --- |
| See what's active, what's running and what needs a restart | `python3 scripts/versions.py status` |
| Update | `python3 scripts/versions.py update` (or `update --github`) |
| Install a specific zip | `python3 scripts/versions.py install ~/Downloads/canvas-reader-X.Y.Z.zip` |
| Install an earlier release from your clone | `python3 scripts/versions.py install dist/canvas-reader-X.Y.Z.zip` (`dist/` keeps every release) |
| Also read Canvas before switching | add `--live` (and `--env-file …` if needed) to `update` or `install` |
| Switch back after a bad update | `python3 scripts/versions.py rollback` |
| Switch to any installed version | `python3 scripts/versions.py use 0.3.3` |
| List installed versions | `python3 scripts/versions.py list` |
| Delete old versions | `python3 scripts/versions.py prune` (keeps the two newest, the active and the previous) |
| Keep the token in the Keychain | `python3 scripts/versions.py token store` (also `status`, `forget`) |

Each version lives in `~/.local/share/canvas-reader/versions/X.Y.Z`, and
`current` links to the active one, so switching is one step. Set
`CANVAS_READER_HOME` to use another folder.

For development, `python3 scripts/versions.py link-tunnel --to checkout` makes
the tunnel run your clone's own `.venv` instead.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| "No Python 3.11 or later was found" | Install one (see step 1), or pass `--python /path/to/python3` to `install`. |
| An install or update fails | Read the error, then run `versions.py status`. Check your internet connection. Nothing changed. |
| `status` shows a version as broken | Its Python was removed (for example by pyenv). Reinstall: `python3 scripts/versions.py install --force dist/canvas-reader-X.Y.Z.zip` |
| "Another versions.py command is running" | Wait for the other command to finish. |
| The reader won't start | Run `~/.local/share/canvas-reader/current/scripts/start.sh --check`; it names the problem. |
| `auth_error` or "Canvas rejected the API token" | Make a new Canvas token, put it in the credential file, and restart the assistant or tunnel. |
| An answer says `PARTIAL RESULT` | It says what is missing. Try again later, or open that item in Canvas yourself. |
| Due times are in the wrong time zone | Set `TIMEZONE` in the credential file (step 2) and restart. |
| A course or file is missing | Check that your Canvas account can see it. |
| A scanned PDF or images aren't read | There's no OCR; ask for a text version. |

Client-specific problems are covered in each client guide.

## Remove everything

1. Disconnect your assistants: [Claude](clients/claude-apps.md#remove),
   [ChatGPT](clients/chatgpt.md#remove), [Codex](clients/codex.md#remove).
2. Delete the installed versions and your credentials:

   ```zsh
   python3 scripts/versions.py uninstall --credentials
   ```

   This deletes the installed versions, `~/.config/canvas-reader/canvas.env` and a
   Canvas token saved in your Keychain. Add `--key` to also delete the tunnel's
   OpenAI runtime key. It lists what it will do and asks first.
3. In Canvas (Account → Settings), delete the access token.
4. Delete your clone folder.

## New Mac

Follow the same steps. Your credential file, Keychain items and tunnel profile
aren't copied by git or the release zip, so create them again on the new Mac.
