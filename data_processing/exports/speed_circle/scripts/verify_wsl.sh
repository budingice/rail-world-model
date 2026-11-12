#!/usr/bin/env bash
set -euo pipefail
if [[ $# != 2 ]]; then
  printf 'Usage: %s EXCEL_DIR NEW_OUTPUT_DIR\n' "$0" >&2
  exit 2
fi
module=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
python_bin=${PYTHON:-python3}
"$python_bin" -c 'import sys, numpy, pandas, openpyxl; assert sys.platform == "linux", sys.platform'
"$python_bin" "$module/run.py" --raw "$1" --output "$2"
