#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
runs=${1:-5}
for run_id in $(seq 1 "$runs"); do
  "$script_dir/run_experiment.sh" static vanilla "$run_id"
done
