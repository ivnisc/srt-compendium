#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
runs=${1:-10}
exec "$script_dir/run_v2_screening.sh" \
  pilot2 pilot2vanilla assistantv2pilot2 assistantv2check2 \
  equivalence_confirmed detector_selection_pilot2 "$runs" 20260908
