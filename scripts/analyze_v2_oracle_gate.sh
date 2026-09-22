#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
results="$project_dir/results/v2"

"$script_dir/analyze_v2_controls.sh"
python3 "$project_dir/analysis/analyze.py" compare \
  --summary "$results/summary.csv" --baseline "$results/baseline.json" \
  --reference-variant vanilla --treatment-variant oracle10 \
  --min-unpaired 20 --output "$results/oracle_gate.json" \
  --svg "$results/oracle_gate.svg"
