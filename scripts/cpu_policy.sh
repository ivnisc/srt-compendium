#!/usr/bin/env bash
set -euo pipefail

if [[ $(uname -s) != Linux || ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "cpu_policy.sh requiere Linux y privilegios root" >&2
  exit 2
fi

command_name=${1:-}
state_file=${2:-}
if [[ -z $command_name || -z $state_file ]]; then
  echo "uso: cpu_policy.sh prepare|restore STATE_FILE" >&2
  exit 2
fi

case "$command_name" in
  prepare)
    : > "$state_file"
    for governor_path in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
      [[ -f $governor_path ]] || continue
      available_path=${governor_path%/scaling_governor}/scaling_available_governors
      if [[ ! -f $available_path ]] || ! grep -qw performance "$available_path"; then
        echo "performance no está disponible para $governor_path" >&2
        exit 1
      fi
      printf '%s,%s\n' "$governor_path" "$(<"$governor_path")" >> "$state_file"
      printf 'performance\n' > "$governor_path"
    done
    ;;
  restore)
    [[ -f $state_file ]] || exit 0
    while IFS=, read -r governor_path previous; do
      [[ -n $governor_path && -f $governor_path ]] || continue
      printf '%s\n' "$previous" > "$governor_path"
    done < "$state_file"
    rm -f -- "$state_file"
    ;;
  *)
    echo "comando desconocido: $command_name" >&2
    exit 2
    ;;
esac
