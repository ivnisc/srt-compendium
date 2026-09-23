#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 6 ]]; then
  echo "uso: run_v2_network_experiment.sh SCENARIO VARIANT RUN_ID PHASE_HIGH DELAY LOSS" >&2
  exit 2
fi
scenario=$1
variant=$2
run_id=$3
phase_high_s=$4
delay_ms=$5
loss_percent=$6
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
source "$project_dir/experiments/v2.env"

PROTOCOL_VERSION=v2n1
INPUT_RATE_BPS=8500000
HIGH_CAPACITY_PERCENT=140
LOW_CAPACITY_PERCENT=104
PHASE_HIGH_S=$phase_high_s
PHASE_LOW_S=30
DURATION_S=60
DELAY_MS=$delay_ms
LOSS_PERCENT=$loss_percent
QUEUE_FORMULA_CAPACITY_PERCENT=112
unset QUEUE_LIMIT_PACKETS
SOURCE_FILE="$project_dir/assets/source_8_5mbps.ts"
RESULTS_ROOT="$project_dir/results/v2/network_matrix"
EXPERIMENT_MATRIX_SHA256=${EXPERIMENT_MATRIX_SHA256:-}
EXPERIMENT_PLAN_SHA256=${EXPERIMENT_PLAN_SHA256:-}

export PROTOCOL_VERSION INPUT_RATE_BPS HIGH_CAPACITY_PERCENT LOW_CAPACITY_PERCENT
export DURATION_S PHASE_HIGH_S PHASE_LOW_S LATENCY_MS DELAY_MS LOSS_PERCENT
export REORDER_PERCENT REORDER_CORRELATION_PERCENT SAMPLE_MS NOMINAL_OHEAD
export QUEUE_FORMULA_CAPACITY_PERCENT SENDER_CPU RECEIVER_CPU SOURCE_FILE RESULTS_ROOT
export EXPERIMENT_MATRIX_SHA256 EXPERIMENT_PLAN_SHA256

"$script_dir/run_experiment.sh" "$scenario" "$variant" "$run_id"
