#!/usr/bin/env bash
# Copy the analysis code from the experiment harness into the analyze Lambda package.
# Usage (from back/): bash scripts/sync_analysis.sh /path/to/06_분석실험
set -euo pipefail
src="${1:?path to 06_분석실험}"
dest="$(cd "$(dirname "$0")/.." && pwd)/lambda/analyze"
for f in run.py grade.py prompt_map.md prompt_reduce.md prompt_verify.md schema_map.json schema_reduce.json schema_verify.json; do
  cp "$src/$f" "$dest/$f"
done
echo "synced analysis code into $dest"
