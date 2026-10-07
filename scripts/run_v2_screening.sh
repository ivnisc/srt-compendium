#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 8 ]]; then
  echo "uso: run_v2_screening.sh LABEL CONTROL ASSIST CHECK CHECK_ID SELECTION_DIR RUNS RANDOM_SEED" >&2
  exit 2
fi
label=$1
control_variant=$2
assistant_variant=$3
check_variant=$4
check_id=$5
selection_dir=$6
runs=$7
random_seed=$8
checkpoint_every=${V2_CHECKPOINT_EVERY:-0}
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
selection_file=${V2_SELECTION_FILE:-"$project_dir/results/v2/$selection_dir/selection.json"}
if [[ ${V2_CPU_POLICY_ACTIVE:-no} != yes ]]; then
  exec "$script_dir/with_v2_cpu_policy.sh" "$0" "$@"
fi
if (( runs < 10 )); then
  echo "El screening no pareado requiere al menos diez corridas por variante" >&2
  exit 2
fi
if [[ ! $checkpoint_every =~ ^[0-9]+$ ]]; then
  echo "V2_CHECKPOINT_EVERY debe ser un entero no negativo" >&2
  exit 2
fi

python3 - "$project_dir/results/v2/host_validation.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists() or json.loads(path.read_text()).get("status") != "accepted":
    raise SystemExit("La validación limpia V2 no está aceptada")
PY

results="$project_dir/results/v2"
plan_file="$results/${label}_execution_plan.csv"
progress_file="$results/${label}_progress.csv"
python3 "$project_dir/analysis/generate_v2_screening_plan.py" \
  --output "$plan_file" --label "$label" \
  --control-variant "$control_variant" \
  --treatment-variant "$assistant_variant" \
  --runs "$runs" --random-seed "$random_seed" \
  --checkpoint-every "$checkpoint_every"
if command -v sha256sum >/dev/null; then
  EXPERIMENT_PLAN_SHA256=$(sha256sum "$plan_file" | awk '{print $1}')
else
  EXPERIMENT_PLAN_SHA256=$(shasum -a 256 "$plan_file" | awk '{print $1}')
fi
export EXPERIMENT_PLAN_SHA256

if [[ ! -f $progress_file ]]; then
  printf 'event_id,completed_at_utc,kind,scenario,variant,run_id,outcome,run_dir,plan_sha256\n' \
    > "$progress_file"
fi

record_progress() {
  local event_id=$1 kind=$2 scenario=$3 variant=$4 run_id=$5
  local outcome=$6 run_dir=$7
  if awk -F, -v event="$event_id" \
    'NR > 1 && $1 == event {found=1} END {exit !found}' "$progress_file"; then
    return
  fi
  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
    "$event_id" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$kind" "$scenario" \
    "$variant" "$run_id" "$outcome" "$run_dir" "$EXPERIMENT_PLAN_SHA256" \
    >> "$progress_file"
}

run_once() {
  local scenario=$1 variant=$2 run_id=$3
  local run_dir="$project_dir/results/v2/$scenario/$variant/run_$run_id"
  if [[ -f $run_dir/run_metadata.csv ]]; then
    echo "corrida V2 ya completa: $run_dir"
    run_outcome=existing
    last_run_dir=$run_dir
    return
  fi
  if [[ -e $run_dir ]]; then
    echo "corrida V2 incompleta; revisar antes de continuar: $run_dir" >&2
    exit 1
  fi
  "$script_dir/run_v2_experiment.sh" "$scenario" "$variant" "$run_id"
  run_outcome=executed
  last_run_dir=$run_dir
}

run_once clean vanilla "${label}_pre"
record_progress pre control clean vanilla "${label}_pre" \
  "$run_outcome" "$last_run_dir"
run_once dynamic "$check_variant" "$check_id"
record_progress equivalence_run equivalence dynamic "$check_variant" "$check_id" \
  "$run_outcome" "$last_run_dir"
python3 "$project_dir/analysis/validate_v2_equivalence.py" \
  --run-dir "$project_dir/results/v2/dynamic/$check_variant/run_$check_id" \
  --selection "$selection_file" \
  --output "$project_dir/results/v2/${label}_equivalence_preflight.json"
record_progress equivalence_check equivalence dynamic "$check_variant" "$check_id" \
  equivalent "$project_dir/results/v2/${label}_equivalence_preflight.json"

while IFS=, read -r step kind scenario variant run_id _; do
  [[ $step == step ]] && continue
  run_once "$scenario" "$variant" "$run_id"
  record_progress "plan_$step" "$kind" "$scenario" "$variant" "$run_id" \
    "$run_outcome" "$last_run_dir"
done < "$plan_file"

run_once clean vanilla "${label}_post"
record_progress post control clean vanilla "${label}_post" \
  "$run_outcome" "$last_run_dir"
"$script_dir/analyze_v2_screening.sh" "$label" "$control_variant" \
  "$assistant_variant" "$check_variant" "$check_id" "$selection_dir" "$runs"
record_progress analysis analysis derived "$assistant_variant" "$label" \
  accepted "$results/${label}_audit.json"
