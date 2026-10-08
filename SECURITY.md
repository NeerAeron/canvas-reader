# Security

## Report a problem privately

Please don't open a public issue for a security problem. Use GitHub's private
vulnerability reporting instead:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability**.
3. Describe what you found and how to reproduce it.

Never include a real Canvas token, OpenAI key, tunnel ID or course content in a
report. Synthetic examples are enough.

Fixes go into the newest release. Older versions don't get separate patches;
update with `python3 scripts/versions.py update`.

## What Canvas Reader protects, and how

Canvas Reader is a local MCP server. Each person runs their own copy on their own
Mac, with their own Canvas API token. There is no hosted service.

| Promise | How it is kept |
| --- | --- |
| **The token stays on your Mac.** | It lives in a credential file you create (`~/.config/canvas-reader/canvas.env`, `chmod 600`), in your macOS Keychain (`versions.py token store`), or in the Claude Code plugin's secure storage. It is never part of the release zip, tool output or logs. File downloads send it only to your Canvas address, never to file storage or redirects. |
| **Read-only, always.** | At startup the server forces the student role, removes every tool that submits, edits, sends messages or runs code, and ignores settings that try to turn them back on. The tests check the full tool list. |
| **Course content is untrusted.** | Text from Canvas comes wrapped in `UNTRUSTED CANVAS CONTENT` markers, and the server tells the assistant to treat it as data, never as instructions. It also tells the assistant to ask before opening Canvas in a browser. |
| **Nothing is written to disk.** | Downloaded files and extracted text stay in memory, expire after 15 minutes and are wiped when the reader stops. Upstream audit logs are off. Logs hold warnings and errors, not course text. |
| **No telemetry.** | Canvas Reader contacts only your Canvas address and the file-storage links Canvas sends downloads to. The upstream library's update check and tracing are turned off. |

## What it can't protect

- **Your Canvas token can do more than Canvas Reader does.** Canvas tokens carry
  your full account permissions. Canvas Reader only reads, but anyone who gets the
  token could do anything you can. Keep the credential file private and delete
  the token in Canvas (Account → Settings) if it leaks.
- **Your assistant sees what it reads.** Course content, Inbox messages and grades
  that a tool returns go to the client and model provider you use. Their data
  policies apply.
- **Whoever can use your connection can read your Canvas.** If you connect
  ChatGPT through OpenAI's Secure MCP Tunnel, link the tunnel only to your
  personal workspace.
- **Your Mac's own security.** Anything that can read your files or run as you
  can read the credential file.
