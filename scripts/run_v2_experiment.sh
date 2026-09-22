#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "uso: run_v2_experiment.sh clean|dynamic VARIANT RUN_ID" >&2
  exit 2
fi

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
source "$project_dir/experiments/v2.env"

export PROTOCOL_VERSION INPUT_RATE_BPS HIGH_CAPACITY_PERCENT LOW_CAPACITY_PERCENT
export LATENCY_MS DELAY_MS LOSS_PERCENT REORDER_PERCENT REORDER_CORRELATION_PERCENT
export SAMPLE_MS NOMINAL_OHEAD QUEUE_LIMIT_PACKETS QUEUE_FORMULA_CAPACITY_PERCENT
export SENDER_CPU RECEIVER_CPU
export RESULTS_ROOT="$project_dir/results/$V2_RESULTS_SUBDIR"

"$script_dir/run_experiment.sh" "$@"
