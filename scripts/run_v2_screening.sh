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
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
if [[ ${V2_CPU_POLICY_ACTIVE:-no} != yes ]]; then
  exec "$script_dir/with_v2_cpu_policy.sh" "$0" "$@"
fi
if (( runs < 10 )); then
  echo "El screening no pareado requiere al menos diez corridas por variante" >&2
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

run_once() {
  local scenario=$1 variant=$2 run_id=$3
  local run_dir="$project_dir/results/v2/$scenario/$variant/run_$run_id"
  if [[ -f $run_dir/run_metadata.csv ]]; then
    echo "corrida V2 ya completa: $run_dir"
    return
  fi
  if [[ -e $run_dir ]]; then
    echo "corrida V2 incompleta; revisar antes de continuar: $run_dir" >&2
    exit 1
  fi
  "$script_dir/run_v2_experiment.sh" "$scenario" "$variant" "$run_id"
}

run_once clean vanilla "${label}_pre"
run_once dynamic "$check_variant" "$check_id"
python3 "$project_dir/analysis/validate_v2_equivalence.py" \
  --run-dir "$project_dir/results/v2/dynamic/$check_variant/run_$check_id" \
  --selection "$project_dir/results/v2/$selection_dir/selection.json" \
  --output "$project_dir/results/v2/${label}_equivalence_preflight.json"

python3 - "$runs" "$control_variant" "$assistant_variant" "$random_seed" <<'PY' |
import random
import sys

runs = int(sys.argv[1])
jobs = [(variant, index) for variant in sys.argv[2:4]
        for index in range(1, runs + 1)]
random.Random(int(sys.argv[4])).shuffle(jobs)
for variant, index in jobs:
    print(f"{variant}:{index}")
PY
while IFS=: read -r variant run_id; do
  run_once dynamic "$variant" "$run_id"
done

run_once clean vanilla "${label}_post"
"$script_dir/analyze_v2_screening.sh" "$label" "$control_variant" \
  "$assistant_variant" "$check_variant" "$check_id" "$selection_dir" "$runs"
