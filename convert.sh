#!/usr/bin/env sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON_BIN=${PYTHON_BIN:-python}

if [ "$#" -eq 0 ]; then
    cat >&2 <<'EOF'
Usage:
  ./convert.sh INPUT [OUTPUT] [converter options]

Examples:
  ./convert.sh input.jsonl output.jsonl
  ./convert.sh input_directory output_directory
  ./convert.sh input.jsonl output.jsonl --project-id my-project

Environment:
  PYTHON_BIN   Python command to use (default: python)
EOF
    exit 2
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/convertToHaloTrace.py" "$@"
