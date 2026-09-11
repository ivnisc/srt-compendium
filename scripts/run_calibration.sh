#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
results_root=${RESULTS_ROOT:-"$project_dir/results"}
if [[ ! -f $project_dir/experiments/frozen_latency.env ]]; then
  echo "Falta experiments/frozen_latency.env; calibre TSBPD antes del actuador" >&2
  exit 1
fi
python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["status"] == "selected"' \
  "$results_root/latency_calibration.json" || {
  echo "La calibración de TSBPD no tiene estado selected" >&2
  exit 1
}
runs=${1:-5}

for run_id in $(seq 1 "$runs"); do
  for variant in fixed10 fixed25 fixed40; do
    "$script_dir/run_experiment.sh" dynamic "$variant" "$run_id"
  done
done
