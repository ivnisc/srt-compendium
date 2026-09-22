#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
config="$project_dir/experiments/v2_robustness.json"
root="$project_dir/results/v2/robustness"
plan="$root/execution_plan.csv"
selection="$project_dir/results/v2/detector_selection_pilot2/selection.json"

if [[ ${V2_CPU_POLICY_ACTIVE:-no} != yes ]]; then
  exec "$script_dir/with_v2_cpu_policy.sh" "$0" "$@"
fi
if (( $# != 0 )); then
  echo "uso: run_v2_robustness.sh" >&2
  exit 2
fi

"$script_dir/check_v2_final_freeze.sh"
mkdir -p "$root/equivalence"
"$script_dir/generate_cbr_source.sh" "$project_dir/assets/source_4mbps.ts" 4000000 60
"$script_dir/generate_cbr_source.sh" "$project_dir/assets/source_12mbps.ts" 12000000 60
python3 "$project_dir/analysis/generate_v2_robustness_plan.py" \
  --config "$config" --output "$plan"

hash_file() {
  if command -v sha256sum >/dev/null; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}
export EXPERIMENT_MATRIX_SHA256
export EXPERIMENT_PLAN_SHA256
EXPERIMENT_MATRIX_SHA256=$(hash_file "$config")
EXPERIMENT_PLAN_SHA256=$(hash_file "$plan")

run_once() {
  local scenario=$1 variant=$2 run_id=$3 input_bps=$4 low_capacity=$5
  local high_s=$6 source_file=$7
  local run_dir="$root/$scenario/$variant/run_$run_id"
  if [[ -f $run_dir/run_metadata.csv ]]; then
    echo "corrida de robustez ya completa: $run_dir"
    return
  fi
  if [[ -e $run_dir ]]; then
    echo "corrida incompleta; revisar antes de reanudar: $run_dir" >&2
    exit 1
  fi
  "$script_dir/run_v2_robustness_experiment.sh" \
    "$scenario" "$variant" "$run_id" "$input_bps" "$low_capacity" \
    "$high_s" "$source_file"
}

for index in $(seq 1 5); do
  run_once clean robust_b4_clean "$index" 4000000 104 15 assets/source_4mbps.ts
  run_once clean robust_b12_clean "$index" 12000000 104 15 assets/source_12mbps.ts
done
run_once clean robust_b85_guard pre 8500000 104 15 assets/source_8_5mbps.ts

python3 - "$config" <<'PY' |
import json
import sys
for item in json.load(open(sys.argv[1]))["conditions"]:
    print(item["id"], item["input_rate_bps"], item["low_capacity_percent"], item["source_file"])
PY
while read -r condition input_bps low_capacity source_file; do
  variant="robust_${condition}_check"
  run_once dynamic "$variant" equivalence "$input_bps" "$low_capacity" 15 "$source_file"
  python3 "$project_dir/analysis/validate_v2_equivalence.py" \
    --run-dir "$root/dynamic/$variant/run_equivalence" \
    --selection "$selection" \
    --output "$root/equivalence/$condition.json"
done

tail -n +2 "$plan" |
while IFS=, read -r _ condition arm run_id high_s _ _ input_bps low_capacity source_file variant; do
  variant=${variant%$'\r'}
  run_once dynamic "$variant" "$run_id" "$input_bps" "$low_capacity" \
    "$high_s" "$source_file"
done

run_once clean robust_b4_guard post 4000000 104 15 assets/source_4mbps.ts
run_once clean robust_b85_guard post 8500000 104 15 assets/source_8_5mbps.ts
run_once clean robust_b12_guard post 12000000 104 15 assets/source_12mbps.ts

python3 "$project_dir/analysis/analyze_v2_robustness.py" \
  --config "$config" --plan "$plan" --results "$root" \
  --base-v2 "$project_dir/results/v2" \
  --output "$root/comparison.json" --csv "$root/condition_runs.csv" \
  --svg "$root/comparison.svg"
