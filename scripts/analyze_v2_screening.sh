#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 7 ]]; then
  echo "uso: analyze_v2_screening.sh LABEL CONTROL ASSIST CHECK CHECK_ID SELECTION_DIR MIN_RUNS" >&2
  exit 2
fi
label=$1
control_variant=$2
assistant_variant=$3
check_variant=$4
check_id=$5
selection_dir=$6
minimum_runs=$7
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
results="$project_dir/results/v2"

"$script_dir/analyze_v2_controls.sh"
python3 "$project_dir/analysis/analyze.py" compare \
  --summary "$results/summary.csv" --baseline "$results/baseline.json" \
  --reference-variant "$control_variant" --treatment-variant "$assistant_variant" \
  --min-unpaired "$minimum_runs" --output "$results/${label}_comparison.json" \
  --svg "$results/${label}_comparison.svg"

mkdir -p "$results/equivalence"
for run_dir in "$results/dynamic/$check_variant/run_$check_id" \
               "$results/dynamic/$assistant_variant"/run_*; do
  [[ -f $run_dir/run_metadata.csv ]] || continue
  run_name="$(basename "$(dirname "$run_dir")")_$(basename "$run_dir")"
  python3 "$project_dir/analysis/validate_v2_equivalence.py" \
    --run-dir "$run_dir" --selection "$results/$selection_dir/selection.json" \
    --output "$results/equivalence/$run_name.json"
done

audit_args=(
  --results "$results" --label "$label"
  --reference-variant "$control_variant"
  --treatment-variant "$assistant_variant"
  --minimum-runs "$minimum_runs"
)
if [[ $assistant_variant == assistantv2final ]]; then
  audit_args+=(--controller-config "$project_dir/experiments/controller_v2_final.env")
fi
python3 "$project_dir/analysis/audit_v2_screening.py" "${audit_args[@]}"
