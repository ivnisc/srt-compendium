#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
results_root=${RESULTS_ROOT:-"$project_dir/results"}
capabilities="$project_dir/experiments/linux_capabilities.env"
if [[ ! -f $capabilities ]]; then
  echo "Falta $capabilities; ejecute scripts/check_linux_capabilities.sh" >&2
  exit 1
fi
source "$capabilities"
if [[ ! -f $project_dir/experiments/frozen_latency.env ]]; then
  echo "Falta experiments/frozen_latency.env" >&2
  exit 1
fi
if [[ ! -f $project_dir/experiments/controller.env ]]; then
  echo "Falta experiments/controller.env; el predictor aún no fue validado" >&2
  exit 1
fi
python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["status"] == "validated"' \
  "$results_root/controller_calibration.json" || {
  echo "La calibración del predictor no tiene estado validated" >&2
  exit 1
}

if [[ $NETEM_SEED_REPRODUCIBLE == yes ]]; then
  runs=${1:-10}
  if (( runs < 10 )); then
    echo "El diseño pareado requiere al menos 10 pares" >&2
    exit 2
  fi
  for run_id in $(seq 1 "$runs"); do
    if (( run_id % 2 == 1 )); then
      variants=(vanilla assistant)
    else
      variants=(assistant vanilla)
    fi
    for variant in "${variants[@]}"; do
      DEGRADATION_SEED=$run_id "$script_dir/run_experiment.sh" dynamic "$variant" "$run_id"
    done
  done
else
  runs=${1:-20}
  if (( runs < 20 )); then
    echo "El diseño no pareado requiere al menos 20 corridas por variante" >&2
    exit 2
  fi
  python3 - "$runs" <<'PY' | while IFS=: read -r variant run_id; do
import random
import sys
runs = int(sys.argv[1])
jobs = [(variant, index) for variant in ("vanilla", "assistant")
        for index in range(1, runs + 1)]
random.Random(20260906).shuffle(jobs)
for variant, index in jobs:
    print(f"{variant}:{variant[0]}{index}")
PY
    "$script_dir/run_experiment.sh" dynamic "$variant" "$run_id"
  done
fi
