#!/usr/bin/env bash
set -euo pipefail

if [[ $(uname -s) != Linux || ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "network_namespace.sh requiere Linux y privilegios root" >&2
  exit 2
fi

command_name=${1:-}
namespace=${2:-srt_rxns}
root_device=${3:-srt_tx0}
peer_device=${4:-srt_rx0}
root_address=${5:-10.203.0.1/30}
peer_address=${6:-10.203.0.2/30}

for name in "$namespace" "$root_device" "$peer_device"; do
  if [[ -z $name || $name == */* || $name == *..* ]]; then
    echo "nombre inválido: $name" >&2
    exit 2
  fi
done

case "$command_name" in
  setup)
    if ip netns list | awk '{print $1}' | awk -v name="$namespace" '$0 == name {found=1} END {exit !found}'; then
      echo "El namespace $namespace ya existe; no se modificó" >&2
      exit 1
    fi
    if ip link show "$root_device" >/dev/null 2>&1; then
      echo "La interfaz $root_device ya existe; no se modificó" >&2
      exit 1
    fi
    rollback() {
      ip link del "$root_device" 2>/dev/null || true
      ip netns del "$namespace" 2>/dev/null || true
    }
    ip netns add "$namespace"
    trap rollback ERR
    ip link add "$root_device" type veth peer name "$peer_device"
    ip link set "$peer_device" netns "$namespace"
    ip addr add "$root_address" dev "$root_device"
    ip link set "$root_device" up
    ip netns exec "$namespace" ip link set lo up
    ip netns exec "$namespace" ip addr add "$peer_address" dev "$peer_device"
    ip netns exec "$namespace" ip link set "$peer_device" up
    trap - ERR
    ;;
  cleanup)
    ip link del "$root_device" 2>/dev/null || true
    ip netns del "$namespace" 2>/dev/null || true
    ;;
  *)
    echo "uso: network_namespace.sh setup|cleanup NAMESPACE ROOT_IF PEER_IF ROOT_CIDR PEER_CIDR" >&2
    exit 2
    ;;
esac
