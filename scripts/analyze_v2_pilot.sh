#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
results="$project_dir/results/v2"

"$script_dir/analyze_v2_controls.sh"
python3 "$project_dir/analysis/analyze.py" compare \
  --summary "$results/summary.csv" --baseline "$results/baseline.json" \
  --reference-variant pilotvanilla --treatment-variant assistantv2pilot \
  --min-unpaired 10 --output "$results/pilot_comparison.json" \
  --svg "$results/pilot_comparison.svg"

mkdir -p "$results/equivalence"
for run_dir in "$results/dynamic/assistantv2check/run_equivalence_signed" \
               "$results/dynamic/assistantv2pilot"/run_*; do
  [[ -f $run_dir/run_metadata.csv ]] || continue
  run_name="$(basename "$(dirname "$run_dir")")_$(basename "$run_dir")"
  python3 "$project_dir/analysis/validate_v2_equivalence.py" \
    --run-dir "$run_dir" \
    --selection "$results/detector_selection/selection.json" \
    --output "$results/equivalence/$run_name.json"
done
