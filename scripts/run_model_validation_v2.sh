#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
model_file="$project_dir/results/model_development_v2/frozen_model.json"
runs=${1:-10}

python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["status"] == "threshold_recalibrated_requires_fresh_validation"' \
  "$model_file" || {
  echo "No existe un modelo K3-v2 congelado" >&2
  exit 1
}

for run_id in $(seq 1 "$runs"); do
  "$script_dir/run_experiment.sh" dynamic modelval25v2 "$run_id"
done

PYTHONPATH="$project_dir/analysis" python3 \
  "$project_dir/analysis/validate_state_model.py" \
  --variant modelval25v2 \
  --model "$model_file" \
  --output-dir "$project_dir/results/model_validation_v2"
