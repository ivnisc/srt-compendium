#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
config="$project_dir/experiments/v2_network_matrix.json"
root="$project_dir/results/v2/network_matrix"
plan="$root/execution_plan.csv"
selection="$project_dir/results/v2/detector_selection_pilot2/selection.json"

if [[ ${V2_CPU_POLICY_ACTIVE:-no} != yes ]]; then
  exec "$script_dir/with_v2_cpu_policy.sh" "$0" "$@"
fi
if (( $# != 0 )); then
  echo "uso: run_v2_network_matrix.sh" >&2
  exit 2
fi

"$script_dir/check_v2_final_freeze.sh"
mkdir -p "$root/equivalence"
python3 "$project_dir/analysis/generate_v2_network_plan.py" \
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
  local scenario=$1 variant=$2 run_id=$3 high_s=$4 delay_ms=$5 loss_percent=$6
  local run_dir="$root/$scenario/$variant/run_$run_id"
  if [[ -f $run_dir/run_metadata.csv ]]; then
    echo "corrida de matriz ya completa: $run_dir"
    return
  fi
  if [[ -e $run_dir ]]; then
    echo "corrida incompleta; revisar antes de reanudar: $run_dir" >&2
    exit 1
  fi
  "$script_dir/run_v2_network_experiment.sh" \
    "$scenario" "$variant" "$run_id" "$high_s" "$delay_ms" "$loss_percent"
}

run_once clean network_matrix_guard pre 15 45 1

python3 - "$config" <<'PY' |
import json
import sys
for item in json.load(open(sys.argv[1]))["conditions"]:
    print(item["id"], item["delay_ms"], item["loss_percent"])
PY
while read -r condition delay_ms loss_percent; do
  variant="network_${condition}_check"
  run_once dynamic "$variant" equivalence 15 "$delay_ms" "$loss_percent"
  python3 "$project_dir/analysis/validate_v2_equivalence.py" \
    --run-dir "$root/dynamic/$variant/run_equivalence" \
    --selection "$selection" \
    --output "$root/equivalence/$condition.json"
done

job_count=0
tail -n +2 "$plan" |
while IFS=, read -r _ condition arm run_id high_s _ _ delay_ms loss_percent variant; do
  variant=${variant%$'\r'}
  run_once dynamic "$variant" "$run_id" "$high_s" "$delay_ms" "$loss_percent"
  job_count=$((job_count + 1))
  if (( job_count == 100 )); then
    run_once clean network_matrix_guard mid 15 45 1
  fi
done

run_once clean network_matrix_guard post 15 45 1

python3 "$project_dir/analysis/analyze_v2_network_matrix.py" \
  --config "$config" --plan "$plan" --results "$root" \
  --baseline "$project_dir/results/v2/baseline.json" \
  --output "$root/comparison.json" --csv "$root/condition_runs.csv" \
  --svg "$root/comparison.svg"
