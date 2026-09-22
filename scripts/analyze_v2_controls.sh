#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
results="$project_dir/results/v2"

python3 "$project_dir/analysis/analyze.py" collect \
  --results "$results" --output "$results/summary.csv"
python3 "$project_dir/analysis/analyze.py" baseline \
  --summary "$results/summary.csv" --output "$results/baseline.json" --min-runs 5
python3 "$project_dir/analysis/validate_v2_host.py" \
  --results "$results" --output "$results/host_validation.json" --min-runs 5
