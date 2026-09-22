#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
if [[ ${V2_CPU_POLICY_ACTIVE:-no} != yes ]]; then
  exec "$script_dir/with_v2_cpu_policy.sh" "$0" "$@"
fi
runs=${1:-5}
if (( runs < 5 )); then
  echo "V2 requiere al menos cinco controles limpios" >&2
  exit 2
fi
for run_id in $(seq 1 "$runs"); do
  run_dir="$project_dir/results/v2/clean/vanilla/run_$run_id"
  if [[ -f $run_dir/run_metadata.csv ]]; then
    echo "control V2 ya completo: $run_dir"
    continue
  fi
  if [[ -e $run_dir ]]; then
    echo "control V2 incompleto; revisar antes de continuar: $run_dir" >&2
    exit 1
  fi
  "$script_dir/run_v2_experiment.sh" clean vanilla "$run_id"
done

"$script_dir/analyze_v2_controls.sh"
