#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
model_file="$project_dir/results/model_development/provisional_model.json"
runs=${1:-10}

python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["status"] == "development_candidate_not_validated"' \
  "$model_file" || {
  echo "No existe un modelo provisional válido" >&2
  exit 1
}

for run_id in $(seq 1 "$runs"); do
  "$script_dir/run_experiment.sh" dynamic modelval25 "$run_id"
done

PYTHONPATH="$project_dir/analysis" python3 \
  "$project_dir/analysis/validate_state_model.py"
