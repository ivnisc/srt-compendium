#!/usr/bin/env bash
set -euo pipefail

if [[ $(uname -s) != Linux ]]; then
  echo "network_profile.sh requiere Linux" >&2
  exit 2
fi
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "ejecutar network_profile.sh como root" >&2
  exit 2
fi

command_name=${1:-}
device=${2:-}
if [[ -z $command_name || -z $device ]]; then
  echo "uso: network_profile.sh setup_dynamic|setup_static|change_rate|cleanup DEVICE ..." >&2
  exit 2
fi
if [[ $device == */* || $device == *..* ]]; then
  echo "interfaz inválida: $device" >&2
  exit 2
fi

case "$command_name" in
  setup_dynamic)
    rate_kbit=${3:?falta rate_kbit}
    delay_ms=${4:?falta delay_ms}
    loss_pct=${5:?falta loss_pct}
    queue_limit=${6:?falta queue_limit}
    seed=${7:-}

    tc qdisc replace dev "$device" root handle 1: htb default 1
    tc class replace dev "$device" parent 1: classid 1:1 htb \
      rate "${rate_kbit}kbit" ceil "${rate_kbit}kbit" burst 32k cburst 32k
    if [[ -n $seed ]]; then
      tc qdisc replace dev "$device" parent 1:1 handle 10: netem \
        limit "$queue_limit" delay "${delay_ms}ms" loss random "${loss_pct}%" seed "$seed"
    else
      tc qdisc replace dev "$device" parent 1:1 handle 10: netem \
        limit "$queue_limit" delay "${delay_ms}ms" loss random "${loss_pct}%"
    fi
    ;;
  setup_static)
    delay_ms=${3:?falta delay_ms}
    loss_pct=${4:?falta loss_pct}
    reorder_pct=${5:?falta reorder_pct}
    reorder_corr=${6:?falta reorder_corr}
    queue_limit=${7:-1000}
    seed=${8:-}
    if [[ -n $seed ]]; then
      tc qdisc replace dev "$device" root netem limit "$queue_limit" \
        delay "${delay_ms}ms" loss random "${loss_pct}%" \
        reorder "${reorder_pct}%" "${reorder_corr}%" seed "$seed"
    else
      tc qdisc replace dev "$device" root netem limit "$queue_limit" \
        delay "${delay_ms}ms" loss random "${loss_pct}%" \
        reorder "${reorder_pct}%" "${reorder_corr}%"
    fi
    ;;
  change_rate)
    rate_kbit=${3:?falta rate_kbit}
    tc class change dev "$device" parent 1: classid 1:1 htb \
      rate "${rate_kbit}kbit" ceil "${rate_kbit}kbit" burst 32k cburst 32k
    ;;
  cleanup)
    tc qdisc del dev "$device" root 2>/dev/null || true
    ;;
  *)
    echo "comando desconocido: $command_name" >&2
    exit 2
    ;;
esac

