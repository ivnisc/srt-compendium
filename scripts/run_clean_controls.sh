#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
runs=${1:-5}
if (( runs < 5 )); then
  echo "Se requieren al menos cinco controles limpios" >&2
  exit 2
fi
for run_id in $(seq 1 "$runs"); do
  "$script_dir/run_experiment.sh" clean vanilla "$run_id"
done
