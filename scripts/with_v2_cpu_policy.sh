#!/usr/bin/env bash
set -euo pipefail

if (( $# == 0 )); then
  echo "uso: with_v2_cpu_policy.sh COMANDO [ARGUMENTOS...]" >&2
  exit 2
fi

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
state_dir=$(mktemp -d /tmp/srt-v2-cpu-policy.XXXXXX)
state_file="$state_dir/state.csv"
restore() {
  sudo -n "$script_dir/cpu_policy.sh" restore "$state_file" || true
  rmdir "$state_dir" 2>/dev/null || true
}
trap restore EXIT INT TERM

sudo -v
sudo -n "$script_dir/cpu_policy.sh" prepare "$state_file"
export V2_CPU_POLICY_ACTIVE=yes
"$@"
