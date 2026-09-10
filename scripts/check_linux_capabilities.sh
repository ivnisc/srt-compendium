#!/usr/bin/env bash
set -euo pipefail

if [[ $(uname -s) != Linux ]]; then
  echo "Este chequeo requiere Linux" >&2
  exit 2
fi

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
output=${1:-"$project_dir/experiments/linux_capabilities.env"}

for binary in tc ip ping cmake c++ python3 ffmpeg awk paste; do
  command -v "$binary" >/dev/null || {
    echo "missing=$binary"
    exit 1
  }
done

namespace=srtseedns0
root_device=srtseedtx0
peer_device=srtseedrx0

if ip link show "$root_device" >/dev/null 2>&1 ||
   ip netns list | awk '{print $1}' | awk -v name="$namespace" '$0 == name {found=1} END {exit !found}'; then
  echo "La topología temporal de comprobación ya existe; no se modificó" >&2
  exit 1
fi

sudo -v

cleanup() {
  sudo -n tc qdisc del dev "$root_device" root 2>/dev/null || true
  sudo -n ip link del "$root_device" 2>/dev/null || true
  sudo -n ip netns del "$namespace" 2>/dev/null || true
}
trap cleanup EXIT

sudo -n ip netns add "$namespace"
sudo -n ip link add "$root_device" type veth peer name "$peer_device"
sudo -n ip link set "$peer_device" netns "$namespace"
sudo -n ip addr add 198.18.0.1/30 dev "$root_device"
sudo -n ip link set "$root_device" up
sudo -n ip netns exec "$namespace" ip link set lo up
sudo -n ip netns exec "$namespace" ip addr add 198.18.0.2/30 dev "$peer_device"
sudo -n ip netns exec "$namespace" ip link set "$peer_device" up
ping -c 1 -W 1 198.18.0.2 >/dev/null

seed_supported=no
seed_reproducible=no
if sudo -n tc qdisc replace dev "$root_device" root netem loss random 30% seed 12345; then
  first=$(ping -c 100 -i 0.01 -W 1 198.18.0.2 2>/dev/null |
    awk -F'icmp_seq=' '/icmp_seq=/{split($2, value, " "); print value[1]}' |
    paste -sd, - || true)
  sudo -n tc qdisc replace dev "$root_device" root netem loss random 30% seed 12345
  second=$(ping -c 100 -i 0.01 -W 1 198.18.0.2 2>/dev/null |
    awk -F'icmp_seq=' '/icmp_seq=/{split($2, value, " "); print value[1]}' |
    paste -sd, - || true)
  seed_supported=yes
  if [[ -n $first && $first == "$second" ]]; then
    seed_reproducible=yes
  fi
fi

mkdir -p "$(dirname "$output")"
printf 'NETEM_SEED_SUPPORTED=%s\nNETEM_SEED_REPRODUCIBLE=%s\n' \
  "$seed_supported" "$seed_reproducible" > "$output"
printf 'netem_seed_syntax=%s\nnetem_seed_reproducible=%s\noutput=%s\n' \
  "$seed_supported" "$seed_reproducible" "$output"
tc -V
uname -a
