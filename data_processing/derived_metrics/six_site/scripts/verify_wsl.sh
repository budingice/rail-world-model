#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: bash scripts/verify_wsl.sh INPUT_ROOT" >&2
    exit 2
fi
input_root=$(realpath -- "$1")
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"
output_dir=$(mktemp -d "${TMPDIR:-/tmp}/auto-train-six-site.XXXXXXXX")
echo "Output: $output_dir"
python main.py --input-root "$input_root" --output-dir "$output_dir"
python evals/verify.py "$output_dir"
