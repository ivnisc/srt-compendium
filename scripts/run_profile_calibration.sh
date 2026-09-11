#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
runs=${1:-3}
candidates=${PROFILE_CAPACITY_CANDIDATES_PERCENT:-106,104,102}
profile_latency=${PROFILE_LATENCY_MS:-120}

if (( runs < 3 )); then
  echo "Se requieren al menos tres corridas por capacidad" >&2
  exit 2
fi

IFS=',' read -r -a values <<< "$candidates"
for run_id in $(seq 1 "$runs"); do
  for capacity in "${values[@]}"; do
    LATENCY_MS=$profile_latency LOW_CAPACITY_PERCENT=$capacity \
      DEGRADATION_SEED=$run_id \
      "$script_dir/run_experiment.sh" dynamic "profile${capacity}" "$run_id"
  done
done
