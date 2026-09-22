#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 7 ]]; then
  echo "uso: run_v2_robustness_experiment.sh SCENARIO VARIANT RUN_ID INPUT_BPS LOW_CAPACITY PHASE_HIGH SOURCE" >&2
  exit 2
fi
scenario=$1
variant=$2
run_id=$3
input_rate_bps=$4
low_capacity_percent=$5
phase_high_s=$6
source_relative=$7
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
source "$project_dir/experiments/v2.env"

PROTOCOL_VERSION=v2r1
INPUT_RATE_BPS=$input_rate_bps
LOW_CAPACITY_PERCENT=$low_capacity_percent
PHASE_HIGH_S=$phase_high_s
PHASE_LOW_S=30
DURATION_S=60
QUEUE_FORMULA_CAPACITY_PERCENT=112
unset QUEUE_LIMIT_PACKETS
SOURCE_FILE="$project_dir/$source_relative"
RESULTS_ROOT="$project_dir/results/v2/robustness"
EXPERIMENT_MATRIX_SHA256=${EXPERIMENT_MATRIX_SHA256:-}
EXPERIMENT_PLAN_SHA256=${EXPERIMENT_PLAN_SHA256:-}

export PROTOCOL_VERSION INPUT_RATE_BPS HIGH_CAPACITY_PERCENT LOW_CAPACITY_PERCENT
export DURATION_S PHASE_HIGH_S PHASE_LOW_S LATENCY_MS DELAY_MS LOSS_PERCENT
export REORDER_PERCENT REORDER_CORRELATION_PERCENT SAMPLE_MS NOMINAL_OHEAD
export QUEUE_FORMULA_CAPACITY_PERCENT SENDER_CPU RECEIVER_CPU SOURCE_FILE RESULTS_ROOT
export EXPERIMENT_MATRIX_SHA256 EXPERIMENT_PLAN_SHA256

"$script_dir/run_experiment.sh" "$scenario" "$variant" "$run_id"
