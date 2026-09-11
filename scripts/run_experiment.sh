#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "uso: run_experiment.sh clean|static|dynamic VARIANT RUN_ID" >&2
  exit 2
fi

scenario=$1
variant=$2
run_id=$3
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)

if [[ -z ${LATENCY_MS+x} && -f $project_dir/experiments/frozen_latency.env ]]; then
  source "$project_dir/experiments/frozen_latency.env"
fi
if [[ -f $project_dir/experiments/frozen_profile.env ]]; then
  source "$project_dir/experiments/frozen_profile.env"
fi
source "$project_dir/experiments/default.env"
PROTOCOL_VERSION=${PROTOCOL_VERSION:-v1}
NETEM_SEED_SUPPORTED=no
NETEM_SEED_REPRODUCIBLE=no
if [[ -f $project_dir/experiments/linux_capabilities.env ]]; then
  source "$project_dir/experiments/linux_capabilities.env"
fi
source_file=${SOURCE_FILE:-"$project_dir/assets/source_8_5mbps.ts"}
binary=${SRT_EXPERIMENT_BIN:-"$project_dir/build/srt_experiment"}
results_root=${RESULTS_ROOT:-"$project_dir/results"}
if [[ -n ${SRT_ROOT:-} ]]; then
  srt_root=$SRT_ROOT
elif [[ -f $project_dir/../../srtcore/srt.h ]]; then
  srt_root=$(cd "$project_dir/../.." && pwd)
else
  srt_root=/Users/ivnisc/Projects/srt
fi

if [[ ! -x $binary ]]; then
  cmake -S "$project_dir" -B "$project_dir/build" -DSRT_ROOT="$srt_root" -DCMAKE_BUILD_TYPE=Release
  cmake --build "$project_dir/build" -j "${BUILD_JOBS:-2}"
fi
if [[ ! -f $source_file ]]; then
  echo "No existe $source_file. Ejecute scripts/generate_source.sh" >&2
  exit 1
fi
source_probe_bps=$(ffprobe -v error -show_entries format=bit_rate \
  -of default=noprint_wrappers=1:nokey=1 "$source_file")
if [[ ! $source_probe_bps =~ ^[0-9]+$ ]]; then
  echo "ffprobe no informó un bitrate válido para $source_file" >&2
  exit 1
fi
if ! awk -v measured="$source_probe_bps" -v expected="$INPUT_RATE_BPS" \
  -v tolerance="$SOURCE_RATE_TOLERANCE_PERCENT" \
  'BEGIN {difference=measured-expected; if (difference<0) difference=-difference; exit !(100*difference/expected <= tolerance)}'; then
  echo "El bitrate medido ($source_probe_bps) excede la tolerancia respecto de $INPUT_RATE_BPS" >&2
  exit 1
fi
if command -v sha256sum >/dev/null; then
  source_sha256=$(sha256sum "$source_file" | awk '{print $1}')
else
  source_sha256=$(shasum -a 256 "$source_file" | awk '{print $1}')
fi
srt_git_commit=$(git -C "$srt_root" rev-parse HEAD 2>/dev/null || printf unknown)
if command -v sha256sum >/dev/null; then
  experiment_binary_sha256=$(sha256sum "$binary" | awk '{print $1}')
else
  experiment_binary_sha256=$(shasum -a 256 "$binary" | awk '{print $1}')
fi
if [[ $scenario != clean && $(uname -s) != Linux ]]; then
  echo "Los escenarios static y dynamic requieren Linux" >&2
  exit 2
fi
if [[ $scenario != clean && $scenario != static && $scenario != dynamic ]]; then
  echo "Escenario inválido: $scenario" >&2
  exit 2
fi

if [[ $(uname -s) == Linux ]]; then
  sudo -v
fi

controller_selection_sha256=
controller_config_sha256=
case "$variant" in
  vanilla|pilotvanilla|pilot2vanilla|finalvanilla|robust_*_vanilla|robust_*_clean|robust_*_guard|network_*_vanilla|network_*_guard)
    sender_mode=vanilla
    active_ohead=$NOMINAL_OHEAD
    ;;
  fixed10|fixed25|fixed40)
    sender_mode=fixed
    active_ohead=${variant#fixed}
    ;;
  oracle10)
    sender_mode=fixed
    active_ohead=10
    ;;
  latency*)
    sender_mode=vanilla
    active_ohead=$NOMINAL_OHEAD
    ;;
  profile*)
    sender_mode=vanilla
    active_ohead=$NOMINAL_OHEAD
    ;;
  modelval25|modelval25v2)
    sender_mode=vanilla
    active_ohead=$NOMINAL_OHEAD
    ;;
  assistant)
    sender_mode=assistant
    controller_file="$project_dir/experiments/controller.env"
    if [[ ! -f $controller_file ]]; then
      echo "Falta $controller_file; debe generarse después de calibrar" >&2
      exit 1
    fi
    source "$controller_file"
    active_ohead=${ACTIVE_OHEAD:?falta ACTIVE_OHEAD}
    ;;
  assistantv2pilot|assistantv2check)
    sender_mode=assistant_v2
    controller_file="$project_dir/experiments/controller_v2_pilot.env"
    if [[ ! -f $controller_file ]]; then
      echo "Falta $controller_file; debe generarse después de seleccionar V2" >&2
      exit 1
    fi
    source "$controller_file"
    active_ohead=${ACTIVE_OHEAD:?falta ACTIVE_OHEAD}
    controller_selection_sha256=${V2_SELECTION_SHA256:?falta V2_SELECTION_SHA256}
    ;;
  assistantv2pilot2|assistantv2check2)
    sender_mode=assistant_v2
    controller_file="$project_dir/experiments/controller_v2_pilot2.env"
    if [[ ! -f $controller_file ]]; then
      echo "Falta $controller_file; debe generarse después de revisar el piloto" >&2
      exit 1
    fi
    source "$controller_file"
    active_ohead=${ACTIVE_OHEAD:?falta ACTIVE_OHEAD}
    controller_selection_sha256=${V2_SELECTION_SHA256:?falta V2_SELECTION_SHA256}
    ;;
  assistantv2final|assistantv2finalcheck)
    sender_mode=assistant_v2
    controller_file="$project_dir/experiments/controller_v2_final.env"
    if [[ ! -f $controller_file ]]; then
      echo "Falta $controller_file; el segundo piloto debe aprobarse y congelarse" >&2
      exit 1
    fi
    source "$controller_file"
    active_ohead=${ACTIVE_OHEAD:?falta ACTIVE_OHEAD}
    controller_selection_sha256=${V2_SELECTION_SHA256:?falta V2_SELECTION_SHA256}
    ;;
  robust_*_assistant|robust_*_check|network_*_assistant|network_*_check)
    sender_mode=assistant_v2
    controller_file="$project_dir/experiments/controller_v2_final.env"
    if [[ ! -f $controller_file ]]; then
      echo "Falta $controller_file; el A/B final debe estar congelado" >&2
      exit 1
    fi
    source "$controller_file"
    active_ohead=${ACTIVE_OHEAD:?falta ACTIVE_OHEAD}
    controller_selection_sha256=${V2_SELECTION_SHA256:?falta V2_SELECTION_SHA256}
    ;;
  *)
    echo "Variante inválida: $variant" >&2
    exit 2
    ;;
esac

if [[ -n ${controller_file:-} ]]; then
  if command -v sha256sum >/dev/null; then
    controller_config_sha256=$(sha256sum "$controller_file" | awk '{print $1}')
  else
    controller_config_sha256=$(shasum -a 256 "$controller_file" | awk '{print $1}')
  fi
fi

run_dir="$results_root/$scenario/$variant/run_$run_id"
if [[ -e $run_dir ]]; then
  echo "El directorio ya existe: $run_dir" >&2
  exit 1
fi
mkdir -p "$run_dir"

ready_signal="$run_dir/receiver.ready"
start_signal="$run_dir/sender.started"
network_events="$run_dir/network_events.csv"
network_script="$project_dir/scripts/network_profile.sh"
namespace_script="$project_dir/scripts/network_namespace.sh"
receiver_pid=
scheduler_pid=
network_installed=no
topology_installed=no
network_device=$INTERFACE
receiver_host=127.0.0.1

cleanup() {
  if [[ -n ${scheduler_pid:-} ]]; then kill "$scheduler_pid" 2>/dev/null || true; fi
  if [[ -n ${receiver_pid:-} ]]; then kill "$receiver_pid" 2>/dev/null || true; fi
  if [[ $network_installed == yes ]]; then
    sudo -n "$network_script" cleanup "$network_device" || true
  fi
  if [[ $topology_installed == yes ]]; then
    sudo -n "$namespace_script" cleanup "$NETWORK_NAMESPACE" "$NAMESPACE_ROOT_IF" \
      "$NAMESPACE_PEER_IF" "$NAMESPACE_ROOT_CIDR" "$NAMESPACE_PEER_CIDR" || true
    sudo -n chown -R "$(id -u):$(id -g)" "$run_dir" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

seed=
if [[ $NETEM_SEED_REPRODUCIBLE == yes ]]; then
  seed=${DEGRADATION_SEED:-$run_id}
fi
high_rate_kbit=$((INPUT_RATE_BPS * HIGH_CAPACITY_PERCENT / 100 / 1000))
low_rate_kbit=$((INPUT_RATE_BPS * LOW_CAPACITY_PERCENT / 100 / 1000))
packet_bits=$((1500 * 8))
queue_window_ms=$((2 * DELAY_MS + 100))
queue_formula_capacity_percent=${QUEUE_FORMULA_CAPACITY_PERCENT:-$LOW_CAPACITY_PERCENT}
queue_limit=${QUEUE_LIMIT_PACKETS:-$(((INPUT_RATE_BPS * queue_formula_capacity_percent / 100 * queue_window_ms / 1000 + packet_bits - 1) / packet_bits))}
effective_reorder_percent=0
if [[ $scenario == static ]]; then
  effective_reorder_percent=$REORDER_PERCENT
fi

cpu_governors=unavailable
cpu_frequencies_khz=unavailable
if [[ $(uname -s) == Linux ]]; then
  cpu_governors=$(for path in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    [[ -f $path ]] && cat "$path"
  done | sort -u | paste -sd+ -)
  cpu_frequencies_khz=$(for path in /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq; do
    [[ -f $path ]] && cat "$path"
  done | paste -sd+ -)
  cpu_governors=${cpu_governors:-unavailable}
  cpu_frequencies_khz=${cpu_frequencies_khz:-unavailable}
fi

if [[ $(uname -s) == Linux && $TOPOLOGY == namespace ]]; then
  sudo -n "$namespace_script" setup "$NETWORK_NAMESPACE" "$NAMESPACE_ROOT_IF" \
    "$NAMESPACE_PEER_IF" "$NAMESPACE_ROOT_CIDR" "$NAMESPACE_PEER_CIDR"
  topology_installed=yes
  network_device=$NAMESPACE_ROOT_IF
  receiver_host=$NAMESPACE_PEER_IP
elif [[ $TOPOLOGY != loopback && $(uname -s) == Linux ]]; then
  echo "TOPOLOGY debe ser namespace o loopback" >&2
  exit 2
fi

if [[ $scenario == clean ]]; then
  if [[ $(uname -s) == Linux ]]; then
    sudo -n "$network_script" cleanup "$network_device"
  fi
elif [[ $scenario == static ]]; then
  sudo -n "$network_script" setup_static "$network_device" "$DELAY_MS" "$LOSS_PERCENT" \
    "$REORDER_PERCENT" "$REORDER_CORRELATION_PERCENT" 1000 "$seed"
  network_installed=yes
else
  sudo -n "$network_script" setup_dynamic "$network_device" "$high_rate_kbit" "$DELAY_MS" \
    "$LOSS_PERCENT" "$queue_limit" "$seed"
  network_installed=yes
fi
if [[ $scenario != clean && $topology_installed == yes ]]; then
  sudo -n ip netns exec "$NETWORK_NAMESPACE" tc qdisc replace \
    dev "$NAMESPACE_PEER_IF" root netem limit 1000 delay "${DELAY_MS}ms"
fi

printf 'phase,monotonic_s,sender_elapsed_ms,rate_kbit\n' > "$network_events"

receiver_args=(
  recv --bind 0.0.0.0 --port "$PORT"
  --stats "$run_dir/rx_stats.csv" --metadata "$run_dir/rx_metadata.csv"
  --variant "$variant" --latency-ms "$LATENCY_MS" --sample-ms "$SAMPLE_MS"
  --max-duration-s "$((DURATION_S + 10))" --ready-signal "$ready_signal"
)
if [[ $topology_installed == yes ]]; then
  receiver_command=("$binary" "${receiver_args[@]}")
  if [[ -n ${RECEIVER_CPU:-} ]]; then
    receiver_command=(taskset -c "$RECEIVER_CPU" "${receiver_command[@]}")
  fi
  sudo -n ip netns exec "$NETWORK_NAMESPACE" "${receiver_command[@]}" &
else
  receiver_command=("$binary" "${receiver_args[@]}")
  if [[ -n ${RECEIVER_CPU:-} ]]; then
    receiver_command=(taskset -c "$RECEIVER_CPU" "${receiver_command[@]}")
  fi
  "${receiver_command[@]}" &
fi
receiver_pid=$!

for _ in $(seq 1 100); do
  [[ -f $ready_signal ]] && break
  sleep 0.05
done
if [[ ! -f $ready_signal ]]; then
  echo "El receptor no quedó listo" >&2
  exit 1
fi

if [[ $scenario == dynamic ]]; then
  (
    for _ in $(seq 1 200); do
      [[ -f $start_signal ]] && break
      sleep 0.01
    done
    if [[ ! -f $start_signal ]]; then
      echo "El emisor no publicó su reloj de inicio" >&2
      exit 1
    fi
    sender_start_us=$(<"$start_signal")
    monotonic=$(cut -d' ' -f1 /proc/uptime)
    sender_elapsed_ms=$(awk -v now="$monotonic" -v start="$sender_start_us" \
      'BEGIN {printf "%.3f", now * 1000 - start / 1000}')
    printf 'high,%s,%s,%s\n' "$monotonic" "$sender_elapsed_ms" "$high_rate_kbit" >> "$network_events"
    sleep "$PHASE_HIGH_S"
    sudo -n "$network_script" change_rate "$network_device" "$low_rate_kbit"
    monotonic=$(cut -d' ' -f1 /proc/uptime)
    sender_elapsed_ms=$(awk -v now="$monotonic" -v start="$sender_start_us" \
      'BEGIN {printf "%.3f", now * 1000 - start / 1000}')
    printf 'low,%s,%s,%s\n' "$monotonic" "$sender_elapsed_ms" "$low_rate_kbit" >> "$network_events"
    sleep "$PHASE_LOW_S"
    sudo -n "$network_script" change_rate "$network_device" "$high_rate_kbit"
    monotonic=$(cut -d' ' -f1 /proc/uptime)
    sender_elapsed_ms=$(awk -v now="$monotonic" -v start="$sender_start_us" \
      'BEGIN {printf "%.3f", now * 1000 - start / 1000}')
    printf 'recovery,%s,%s,%s\n' "$monotonic" "$sender_elapsed_ms" "$high_rate_kbit" >> "$network_events"
  ) &
  scheduler_pid=$!
fi

sender_args=(
  send --host "$receiver_host" --port "$PORT" --input "$source_file"
  --stats "$run_dir/tx_stats.csv" --metadata "$run_dir/tx_metadata.csv"
  --variant "$variant" --duration-s "$DURATION_S" --latency-ms "$LATENCY_MS"
  --sample-ms "$SAMPLE_MS" --input-rate-bps "$INPUT_RATE_BPS"
  --mode "$sender_mode" --nominal-ohead "$NOMINAL_OHEAD"
  --active-ohead "$active_ohead" --active-from-ms "$((PHASE_HIGH_S * 1000))"
  --active-until-ms "$(((PHASE_HIGH_S + PHASE_LOW_S) * 1000))"
  --start-signal "$start_signal"
)

if [[ $sender_mode == assistant ]]; then
  sender_args+=(
    --trigger-rtt-ms "$TRIGGER_RTT_MS" --release-rtt-ms "$RELEASE_RTT_MS"
    --horizon-ms "$HORIZON_MS" --kalman-q "$KALMAN_Q" --kalman-r "$KALMAN_R"
    --trigger-samples "$TRIGGER_SAMPLES" --release-samples "$RELEASE_SAMPLES"
    --cooldown-ms "$COOLDOWN_MS" --warmup-ms "$WARMUP_MS"
  )
fi
if [[ $sender_mode == assistant_v2 ]]; then
  sender_args+=(
    --horizon-ms "$V2_HORIZON_MS"
    --v2-baseline-begin-ms "$V2_BASELINE_BEGIN_MS"
    --v2-baseline-end-ms "$V2_BASELINE_END_MS"
    --v2-q-rtt "$V2_KALMAN_Q_RTT" --v2-q-sndbuf "$V2_KALMAN_Q_SNDBUF"
    --v2-rtt-weight "$V2_RTT_WEIGHT" --v2-trigger-score "$V2_TRIGGER_SCORE"
    --v2-trigger-samples "$V2_TRIGGER_SAMPLES"
    --v2-release-ratio "$V2_RELEASE_SCORE_RATIO"
    --v2-release-samples "$V2_RELEASE_SAMPLES"
    --v2-confirmation-ratio "$V2_CONFIRMATION_RATIO"
    --v2-confirmation-timeout-ms "$V2_CONFIRMATION_TIMEOUT_MS"
    --v2-retry-cooldown-ms "$V2_RETRY_COOLDOWN_MS"
    --v2-actuate-on-confirmation "${V2_ACTUATE_ON_CONFIRMATION:-0}"
  )
fi

sender_command=("$binary" "${sender_args[@]}")
if [[ -n ${SENDER_CPU:-} ]]; then
  sender_command=(taskset -c "$SENDER_CPU" "${sender_command[@]}")
fi
"${sender_command[@]}"
wait "$receiver_pid"
receiver_pid=
if [[ -n ${scheduler_pid:-} ]]; then
  wait "$scheduler_pid"
  scheduler_pid=
fi

printf 'key,value\nprotocol_version,%s\nscenario,%s\nvariant,%s\nrun_id,%s\nseed,%s\nseed_reproducible,%s\ninput_rate_bps,%s\nsource_probe_bps,%s\nsource_rate_tolerance_percent,%s\nduration_s,%s\nphase_high_s,%s\nphase_low_s,%s\nlatency_ms,%s\ndelay_ms,%s\nloss_percent,%s\nreorder_percent,%s\nreorder_percent_configured,%s\nreorder_percent_effective,%s\nsample_ms,%s\ntopology,%s\ninterface,%s\nqueue_limit_packets,%s\nqueue_formula_capacity_percent,%s\nhigh_capacity_percent,%s\nlow_capacity_percent,%s\nhigh_rate_kbit,%s\nlow_rate_kbit,%s\nsender_cpu,%s\nreceiver_cpu,%s\ncpu_governors,%s\ncpu_frequencies_khz_at_start,%s\ncontroller_selection_sha256,%s\ncontroller_config_sha256,%s\nexperiment_matrix_sha256,%s\nexperiment_plan_sha256,%s\nexperiment_binary_sha256,%s\nsource_sha256,%s\nsrt_git_commit,%s\n' \
  "$PROTOCOL_VERSION" "$scenario" "$variant" "$run_id" "$seed" "$NETEM_SEED_REPRODUCIBLE" "$INPUT_RATE_BPS" \
  "$source_probe_bps" "$SOURCE_RATE_TOLERANCE_PERCENT" \
  "$DURATION_S" "$PHASE_HIGH_S" "$PHASE_LOW_S" "$LATENCY_MS" \
  "$DELAY_MS" "$LOSS_PERCENT" "$effective_reorder_percent" "$REORDER_PERCENT" "$effective_reorder_percent" "$SAMPLE_MS" \
  "$TOPOLOGY" "$network_device" "$queue_limit" "$queue_formula_capacity_percent" \
  "$HIGH_CAPACITY_PERCENT" "$LOW_CAPACITY_PERCENT" "$high_rate_kbit" "$low_rate_kbit" \
  "${SENDER_CPU:-}" "${RECEIVER_CPU:-}" "$cpu_governors" "$cpu_frequencies_khz" \
  "$controller_selection_sha256" "$controller_config_sha256" \
  "${EXPERIMENT_MATRIX_SHA256:-}" "${EXPERIMENT_PLAN_SHA256:-}" \
  "$experiment_binary_sha256" "$source_sha256" "$srt_git_commit" \
  > "$run_dir/run_metadata.csv"

if [[ $network_installed == yes ]]; then
  sudo -n "$network_script" cleanup "$network_device"
  network_installed=no
fi
if [[ $topology_installed == yes ]]; then
  sudo -n "$namespace_script" cleanup "$NETWORK_NAMESPACE" "$NAMESPACE_ROOT_IF" \
    "$NAMESPACE_PEER_IF" "$NAMESPACE_ROOT_CIDR" "$NAMESPACE_PEER_CIDR"
  topology_installed=no
  sudo -n chown -R "$(id -u):$(id -g)" "$run_dir"
fi
trap - EXIT INT TERM
echo "$run_dir"
