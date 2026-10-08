#!/bin/sh
# Start the installed, active Canvas Reader (see scripts/versions.py). MCP
# clients run this file, so a client keeps working across updates and never
# depends on a source checkout's own .venv.
set -eu

# The same folder versions.py manages.
if [ -n "${CANVAS_READER_HOME:-}" ]; then
  case "$CANVAS_READER_HOME" in
    "~") canvas_reader_home=$HOME ;;
    "~/"*) canvas_reader_home=$HOME/${CANVAS_READER_HOME#"~/"} ;;
    *) canvas_reader_home=$CANVAS_READER_HOME ;;
  esac
else
  case "${XDG_DATA_HOME:-}" in
    /*) canvas_reader_home=$XDG_DATA_HOME/canvas-reader ;;
    *) canvas_reader_home=${HOME:-}/.local/share/canvas-reader ;;
  esac
fi
canvas_reader_start=$canvas_reader_home/current/scripts/start.sh

if [ ! -x "$canvas_reader_start" ]; then
  echo "Canvas Reader is not installed yet. From a clone of the repository, run: python3 scripts/versions.py install . (docs/SETUP.md)" >&2
  exit 1
fi

# A client may pass settings it was never given as empty values or as
# unexpanded placeholders such as ${user_config.canvas_token}. Drop those so the
# credential file (or a clear "missing credentials" message) applies instead.
for canvas_reader_name in CANVAS_API_URL CANVAS_API_TOKEN TIMEZONE CANVAS_ENV_FILE; do
  eval "canvas_reader_value=\${$canvas_reader_name:-}"
  case "$canvas_reader_value" in
    ''|*'${'*) unset "$canvas_reader_name" ;;
  esac
done
unset canvas_reader_name canvas_reader_value

exec "$canvas_reader_start" "$@"
