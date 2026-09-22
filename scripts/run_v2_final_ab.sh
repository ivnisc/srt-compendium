#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
runs=${1:-20}

if (( runs < 20 )); then
  echo "El A/B final no pareado requiere al menos veinte corridas por variante" >&2
  exit 2
fi
"$script_dir/check_v2_final_freeze.sh"

exec "$script_dir/run_v2_screening.sh" \
  final_ab finalvanilla assistantv2final assistantv2finalcheck \
  equivalence_frozen detector_selection_pilot2 "$runs" 20260909
