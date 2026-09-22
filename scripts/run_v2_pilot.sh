#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
if [[ ${V2_CPU_POLICY_ACTIVE:-no} != yes ]]; then
  exec "$script_dir/with_v2_cpu_policy.sh" "$0" "$@"
fi

runs=${1:-10}
if (( runs < 10 )); then
  echo "El piloto no pareado requiere diez corridas por variante" >&2
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

run_once clean vanilla pilot_pre
run_once dynamic assistantv2check equivalence_signed
python3 "$project_dir/analysis/validate_v2_equivalence.py" \
  --run-dir "$project_dir/results/v2/dynamic/assistantv2check/run_equivalence_signed" \
  --selection "$project_dir/results/v2/detector_selection/selection.json" \
  --output "$project_dir/results/v2/equivalence_preflight.json"

python3 - "$runs" <<'PY' | while IFS=: read -r variant run_id; do
import random
import sys

runs = int(sys.argv[1])
jobs = [(variant, index) for variant in ("pilotvanilla", "assistantv2pilot")
        for index in range(1, runs + 1)]
random.Random(20260907).shuffle(jobs)
for variant, index in jobs:
    print(f"{variant}:{index}")
PY
  run_once dynamic "$variant" "$run_id"
done

run_once clean vanilla pilot_post
"$script_dir/analyze_v2_pilot.sh"
