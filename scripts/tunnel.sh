#!/bin/bash
# Manage the private Secure MCP Tunnel for Canvas Reader. Secrets stay outside the project.
set -euo pipefail

canvas_reader_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
canvas_reader_profile=${CANVAS_READER_TUNNEL_PROFILE:-canvas-reader}
keychain_service=${CANVAS_READER_KEYCHAIN_SERVICE:-canvas-reader-tunnel}
security_bin=${CANVAS_READER_SECURITY_BIN:-/usr/bin/security}

usage() {
  cat <<'USAGE'
Usage: scripts/tunnel.sh ACTION

  --run         Run the tunnel in the foreground. Keep the Mac awake and online.
  --status      Show local readiness without contacting OpenAI.
  --doctor      Check the tunnel profile (tunnel-client doctor --explain).
  --store-key   Save the OpenAI runtime key in your macOS Keychain (hidden prompt).
  --forget-key  Remove the saved key from the Keychain.
  --setup [ID]  Create the tunnel profile for a tunnel ID (or CANVAS_READER_TUNNEL_ID).

The runtime key comes from CONTROL_PLANE_API_KEY when set, otherwise from the
Keychain; it is never written to files or passed as an argument. Canvas
credentials come from CANVAS_ENV_FILE, else ~/.config/canvas-reader/canvas.env.
Optional: TUNNEL_CLIENT_BIN, CANVAS_READER_TUNNEL_PROFILE, TUNNEL_CLIENT_PROFILE_DIR.
See docs/clients/chatgpt.md.
USAGE
}

fail() { printf 'Canvas Reader tunnel: %s\n' "$1" >&2; exit 1; }

case "${1:---help}" in
  --help|-h) usage; exit 0 ;;
  --setup) [ "$#" -le 2 ] || fail 'Use --setup with at most one tunnel ID.' ;;
  --doctor|--run|--status|--store-key|--forget-key) [ "$#" -eq 1 ] || fail 'This action accepts no extra arguments.' ;;
  *) usage >&2; exit 2 ;;
esac

case "$canvas_reader_profile" in
  ''|*[!a-zA-Z0-9_-]*) fail 'Use letters, numbers, underscores, or hyphens for CANVAS_READER_TUNNEL_PROFILE.' ;;
esac

keychain_available() { [ -x "$security_bin" ]; }

case "$1" in
  --store-key)
    keychain_available || fail 'The macOS Keychain tool is unavailable here; export CONTROL_PLANE_API_KEY instead.'
    printf 'Paste the OpenAI runtime key when asked, twice. Typing stays hidden.\n'
    "$security_bin" add-generic-password -U -a "${USER:-canvas-reader}" -s "$keychain_service" \
      -l 'Canvas Reader tunnel runtime key' -w
    printf 'Saved in your login Keychain as "%s".\n' "$keychain_service"
    exit 0
    ;;
  --forget-key)
    keychain_available || fail 'The macOS Keychain tool is unavailable here.'
    if "$security_bin" delete-generic-password -s "$keychain_service" >/dev/null 2>&1; then
      printf 'Removed "%s" from your Keychain.\n' "$keychain_service"
    else
      printf 'No saved key named "%s".\n' "$keychain_service"
    fi
    exit 0
    ;;
esac

if [ -n "${TUNNEL_CLIENT_BIN:-}" ]; then
  canvas_reader_tunnel_bin=$TUNNEL_CLIENT_BIN
elif [ -x "$canvas_reader_root/.tools/tunnel-client" ]; then
  canvas_reader_tunnel_bin=$canvas_reader_root/.tools/tunnel-client
else
  canvas_reader_tunnel_bin=$(command -v tunnel-client || true)
fi
[ -n "$canvas_reader_tunnel_bin" ] && [ -x "$canvas_reader_tunnel_bin" ] ||
  fail 'Install tunnel-client with: brew install openai/tools/tunnel-client (docs/clients/chatgpt.md). Set TUNNEL_CLIENT_BIN if it is outside PATH.'

canvas_reader_profile_args=(--profile "$canvas_reader_profile")
if [ -n "${TUNNEL_CLIENT_PROFILE_DIR:-}" ]; then
  canvas_reader_profile_args+=(--profile-dir "$TUNNEL_CLIENT_PROFILE_DIR")
fi

canvas_env_file=${CANVAS_ENV_FILE:-}
if [ -z "$canvas_env_file" ]; then
  if [ -n "${XDG_CONFIG_HOME:-}" ]; then
    default_env_file=$XDG_CONFIG_HOME/canvas-reader/canvas.env
  elif [ -n "${HOME:-}" ]; then
    default_env_file=$HOME/.config/canvas-reader/canvas.env
  else
    default_env_file=
  fi
  if [ -n "$default_env_file" ] && [ -f "$default_env_file" ]; then
    canvas_env_file=$default_env_file
  fi
fi

if [ "$1" = --status ]; then
  "$canvas_reader_tunnel_bin" --version
  printf 'Selected profile: %s\n' "$canvas_reader_profile"
  if [ -n "${CONTROL_PLANE_API_KEY:-}" ]; then
    printf 'Runtime key: present in environment\n'
  elif keychain_available && "$security_bin" find-generic-password -s "$keychain_service" >/dev/null 2>&1; then
    printf 'Runtime key: saved in Keychain\n'
  else
    printf 'Runtime key: missing; run scripts/tunnel.sh --store-key\n'
  fi
  if [ -n "$canvas_env_file" ] && [ -f "$canvas_env_file" ] && [ -r "$canvas_env_file" ]; then
    printf 'Canvas credential file: %s\n' "$canvas_env_file"
  else
    printf 'Canvas credential file: missing; create ~/.config/canvas-reader/canvas.env or set CANVAS_ENV_FILE\n'
  fi
  printf 'Transport and profile health: not checked; run scripts/tunnel.sh --doctor\n'
  exit 0
fi

if [ -z "${CONTROL_PLANE_API_KEY:-}" ] && keychain_available; then
  if canvas_reader_key=$("$security_bin" find-generic-password -s "$keychain_service" -w 2>/dev/null) &&
      [ -n "$canvas_reader_key" ]; then
    export CONTROL_PLANE_API_KEY=$canvas_reader_key
    printf 'Canvas Reader tunnel: using the runtime key saved in your Keychain.\n' >&2
  fi
  unset canvas_reader_key
fi
[ -n "${CONTROL_PLANE_API_KEY:-}" ] ||
  fail 'No OpenAI runtime key. Run scripts/tunnel.sh --store-key once, or export CONTROL_PLANE_API_KEY. Never put it in plugin files.'

case "$1" in
  --setup)
    canvas_reader_tunnel_id=${2:-${CANVAS_READER_TUNNEL_ID:-}}
    case "$canvas_reader_tunnel_id" in
      tunnel_?*) ;;
      *) fail 'Supply your actual tunnel ID to --setup or set CANVAS_READER_TUNNEL_ID. Create/select it in Platform tunnel settings.' ;;
    esac
    [ -n "$canvas_env_file" ] && [ -f "$canvas_env_file" ] && [ -r "$canvas_env_file" ] ||
      fail 'Create ~/.config/canvas-reader/canvas.env or set CANVAS_ENV_FILE to a readable Canvas credential file before setup.'
    canvas_reader_env_dir=$(CDPATH='' cd -- "$(dirname -- "$canvas_env_file")" && pwd -P)
    canvas_reader_env_file=$canvas_reader_env_dir/$(basename -- "$canvas_env_file")
    canvas_reader_real_root=$(CDPATH='' cd -- "$canvas_reader_root" && pwd -P)
    case "$canvas_reader_env_file" in
      "$canvas_reader_real_root"/*) fail 'Keep the Canvas credential file outside the plugin project.' ;;
    esac
    # Prefer the installed, active version (the folder versions.py manages), so the tunnel
    # follows updates without a separate link-tunnel step.
    if [ -n "${CANVAS_READER_HOME:-}" ]; then
      canvas_reader_managed=$CANVAS_READER_HOME
      case "$canvas_reader_managed" in "~"|"~/"*) canvas_reader_managed=${HOME:-}${canvas_reader_managed#"~"} ;; esac
    elif [[ "${XDG_DATA_HOME:-}" == /* ]]; then
      canvas_reader_managed=$XDG_DATA_HOME/canvas-reader
    elif [ -n "${HOME:-}" ]; then
      canvas_reader_managed=$HOME/.local/share/canvas-reader
    else
      canvas_reader_managed=
    fi
    if [ -n "$canvas_reader_managed" ] && [ -x "$canvas_reader_managed/current/scripts/start.sh" ]; then
      canvas_reader_managed=$(CDPATH='' cd -- "$canvas_reader_managed" && pwd)
      canvas_reader_start=$canvas_reader_managed/current/scripts/start.sh
      printf 'Canvas Reader tunnel: the profile will run the installed version and follow updates.\n' >&2
    else
      canvas_reader_start=$canvas_reader_root/scripts/start.sh
      printf 'Canvas Reader tunnel: nothing is installed yet, so the profile runs this folder; after installing, run scripts/versions.py link-tunnel.\n' >&2
    fi
    [ -x "$canvas_reader_start" ] ||
      fail 'The scripts/start.sh launcher is missing or not executable.'
    # POSIX single-quote escaping preserves spaces, quotes, and shell metacharacters.
    quote_arg() { local value=${1//\'/\'\\\'\'}; printf "'%s'" "$value"; }
    canvas_reader_command="$(quote_arg "$canvas_reader_start") --env-file $(quote_arg "$canvas_reader_env_file")"
    exec "$canvas_reader_tunnel_bin" init \
      --sample sample_mcp_stdio_local "${canvas_reader_profile_args[@]}" \
      --tunnel-id "$canvas_reader_tunnel_id" --mcp-command "$canvas_reader_command"
    ;;
  --doctor) exec "$canvas_reader_tunnel_bin" doctor "${canvas_reader_profile_args[@]}" --explain ;;
  --run) exec "$canvas_reader_tunnel_bin" run "${canvas_reader_profile_args[@]}" ;;
esac
