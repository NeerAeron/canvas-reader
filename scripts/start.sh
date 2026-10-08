#!/bin/sh
set -eu
canvas_reader_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
if [ ! -x "$canvas_reader_root/.venv/bin/canvas-reader" ]; then
  echo "Canvas Reader is not installed yet; see docs/SETUP.md, step 1." >&2
  exit 1
fi
# The tunnel's OpenAI runtime key is not needed by the reader.
unset CONTROL_PLANE_API_KEY
exec "$canvas_reader_root/.venv/bin/canvas-reader" "$@"
