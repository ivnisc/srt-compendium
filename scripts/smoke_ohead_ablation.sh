#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
protocol="$project_dir/experiments/ohead_ablation_protocol.json"
results="$project_dir/results/v2/ohead_ablation_smoke"
plan="$results/execution_plan.csv"

if [[ ${V2_CPU_POLICY_ACTIVE:-no} != yes ]]; then
  exec "$script_dir/with_v2_cpu_policy.sh" "$0" "$@"
fi
if [[ -z ${SRT_EXPERIMENT_BIN:-} || ! -x $SRT_EXPERIMENT_BIN ]]; then
  echo "Defina SRT_EXPERIMENT_BIN con el binario institucional" >&2
  exit 2
fi
if [[ -z ${SRT_ROOT:-} || ! -d $SRT_ROOT/.git ]]; then
  echo "Defina SRT_ROOT con el repositorio SRT congelado" >&2
  exit 2
fi

hash_file() {
  sha256sum "$1" | awk '{print $1}'
}
expected_binary=$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["expected_binary_sha256"])' \
  "$protocol")
if [[ $(hash_file "$SRT_EXPERIMENT_BIN") != "$expected_binary" ]]; then
  echo "El binario no coincide con el SHA-256 institucional congelado" >&2
  exit 1
fi
if [[ -n $(git -C "$project_dir" status --porcelain --untracked-files=no) ]]; then
  echo "El árbol versionado debe estar limpio antes del humo" >&2
  exit 1
fi

mkdir -p "$results/equivalence" "$results/audits" "$results/logs"
python3 "$project_dir/analysis/generate_ohead_ablation_plan.py" \
  --config "$protocol" --output "$plan"
export EXPERIMENT_MATRIX_SHA256=$(hash_file "$protocol")
export EXPERIMENT_PLAN_SHA256=$(hash_file "$plan")
export ABLATION_RESULTS_ROOT="$results"

while IFS=, read -r order _block _position arm variant run_id; do
  [[ $order == execution_order ]] && continue
  (( order <= 4 )) || break
  run_dir="$results/dynamic/$variant/run_$run_id"
  log="$results/logs/${variant}.log"
  if [[ ! -f $run_dir/run_metadata.csv ]]; then
    "$script_dir/run_ohead_ablation_experiment.sh" \
      dynamic "$variant" "$run_id" >"$log" 2>&1
  fi
  equivalence_args=()
  if [[ $arm == assistant ]]; then
    equivalence="$results/equivalence/run_${run_id}.json"
    python3 "$project_dir/analysis/validate_v2_equivalence.py" \
      --run-dir "$run_dir" \
      --selection "$project_dir/experiments/frozen/detector_selection_pilot2.json" \
      --output "$equivalence" >>"$log" 2>&1
    equivalence_args=(--equivalence "$equivalence")
  fi
  python3 "$project_dir/analysis/analyze_ohead_ablation.py" \
    --protocol "$protocol" --plan "$plan" --audit-run "$run_dir" \
    --arm "$arm" --variant "$variant" --run-id "$run_id" \
    --audit-output "$results/audits/${variant}.json" \
    "${equivalence_args[@]}" >>"$log" 2>&1
done < "$plan"

echo "smoke_status=accepted"
echo "results=$results"
