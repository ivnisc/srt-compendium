#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
if [[ ${V2_CPU_POLICY_ACTIVE:-no} != yes ]]; then
  exec "$script_dir/with_v2_cpu_policy.sh" "$0" "$@"
fi
runs=${1:-20}
if (( runs < 20 )); then
  echo "La compuerta no pareada requiere veinte corridas por variante" >&2
  exit 2
fi

host_report="$project_dir/results/v2/host_validation.json"
python3 - "$host_report" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists() or json.loads(path.read_text()).get("status") != "accepted":
    raise SystemExit("La validación limpia V2 no está aceptada")
PY

python3 - "$runs" <<'PY' | while IFS=: read -r variant run_id; do
import random
import sys

runs = int(sys.argv[1])
jobs = [(variant, index) for variant in ("vanilla", "oracle10")
        for index in range(1, runs + 1)]
random.Random(20260906).shuffle(jobs)
for variant, index in jobs:
    print(f"{variant}:{index}")
PY
  run_dir="$project_dir/results/v2/dynamic/$variant/run_$run_id"
  if [[ -f $run_dir/run_metadata.csv ]]; then
    echo "corrida V2 ya completa: $run_dir"
    continue
  fi
  if [[ -e $run_dir ]]; then
    echo "corrida V2 incompleta; revisar antes de continuar: $run_dir" >&2
    exit 1
  fi
  "$script_dir/run_v2_experiment.sh" dynamic "$variant" "$run_id"
done

"$script_dir/analyze_v2_oracle_gate.sh"
