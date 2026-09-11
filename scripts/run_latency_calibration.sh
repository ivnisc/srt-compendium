#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
runs=${1:-3}
latencies=${LATENCY_CANDIDATES_MS:-80,120,160,200}
variant_prefix=${LATENCY_VARIANT_PREFIX:-latencycal}

if [[ ! -f $script_dir/../experiments/frozen_profile.env ]]; then
  echo "Falta experiments/frozen_profile.env; calibre primero la capacidad" >&2
  exit 1
fi

IFS=',' read -r -a values <<< "$latencies"
for run_id in $(seq 1 "$runs"); do
  for latency in "${values[@]}"; do
    LATENCY_MS=$latency DEGRADATION_SEED=$run_id \
      "$script_dir/run_experiment.sh" dynamic "${variant_prefix}${latency}" "$run_id"
  done
done
