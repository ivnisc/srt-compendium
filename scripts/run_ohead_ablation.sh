#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
protocol="$project_dir/experiments/ohead_ablation_protocol.json"
results="$project_dir/results/v2/ohead_ablation"
plan="$results/execution_plan.csv"
progress="$results/progress.csv"
logs="$results/logs"
audits="$results/run_audits"
equivalence_dir="$results/equivalence"
disposition_dir="$results/equivalence_dispositions"
controller_disposition_dir="$results/controller_dispositions"
failure_disposition_dir="$results/failure_dispositions"

if [[ ${V2_CPU_POLICY_ACTIVE:-no} != yes ]]; then
  exec "$script_dir/with_v2_cpu_policy.sh" "$0" "$@"
fi
if [[ $(uname -s) != Linux ]]; then
  echo "La ablación requiere Linux" >&2
  exit 2
fi
if [[ -z ${SRT_EXPERIMENT_BIN:-} || ! -x $SRT_EXPERIMENT_BIN ]]; then
  echo "Defina SRT_EXPERIMENT_BIN con el binario institucional ejecutable" >&2
  exit 2
fi
if [[ -z ${SRT_ROOT:-} || ! -d $SRT_ROOT/.git ]]; then
  echo "Defina SRT_ROOT con el repositorio SRT congelado" >&2
  exit 2
fi
if [[ -n $(git -C "$project_dir" status --porcelain --untracked-files=no) ]]; then
  echo "El árbol versionado debe estar limpio antes de la campaña" >&2
  exit 1
fi

hash_file() {
  if command -v sha256sum >/dev/null; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

read_protocol() {
  python3 - "$protocol" "$1" <<'PY'
import json
import sys
value = json.load(open(sys.argv[1]))[sys.argv[2]]
print(value)
PY
}

expected_binary=$(read_protocol expected_binary_sha256)
actual_binary=$(hash_file "$SRT_EXPERIMENT_BIN")
if [[ $actual_binary != "$expected_binary" ]]; then
  echo "El binario no coincide con el SHA-256 institucional congelado" >&2
  echo "esperado=$expected_binary" >&2
  echo "observado=$actual_binary" >&2
  exit 1
fi
expected_srt=$(read_protocol expected_srt_git_commit)
actual_srt=$(git -C "$SRT_ROOT" rev-parse HEAD)
if [[ $actual_srt != "$expected_srt" ]]; then
  echo "SRT no coincide con el commit congelado" >&2
  exit 1
fi
"$script_dir/check_v2_final_freeze.sh"

mkdir -p "$results" "$logs" "$audits" "$equivalence_dir" \
  "$disposition_dir" "$controller_disposition_dir" "$failure_disposition_dir"
python3 "$project_dir/analysis/generate_ohead_ablation_plan.py" \
  --config "$protocol" --output "$plan"
EXPERIMENT_MATRIX_SHA256=$(hash_file "$protocol")
EXPERIMENT_PLAN_SHA256=$(hash_file "$plan")
export EXPERIMENT_MATRIX_SHA256 EXPERIMENT_PLAN_SHA256

if [[ ! -f $progress ]]; then
  printf '%s\n' \
    'event_id,completed_at_utc,kind,plan_execution_order,block,arm,variant,run_id,outcome,run_dir,audit_report,plan_sha256,protocol_sha256' \
    > "$progress"
fi

record_progress() {
  local event_id=$1 kind=$2 order=$3 block=$4 arm=$5 variant=$6
  local run_id=$7 outcome=$8 run_dir=$9 audit_report=${10}
  if awk -F, -v event="$event_id" \
    'NR > 1 && $1 == event {found=1} END {exit !found}' "$progress"; then
    return
  fi
  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
    "$event_id" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$kind" "$order" \
    "$block" "$arm" "$variant" "$run_id" "$outcome" "$run_dir" \
    "$audit_report" "$EXPERIMENT_PLAN_SHA256" "$EXPERIMENT_MATRIX_SHA256" \
    >> "$progress"
}

run_control() {
  local run_id=$1
  local run_dir="$results/clean/ablation_guard/run_$run_id"
  local event_id="control_$run_id"
  if awk -F, -v event="$event_id" \
    'NR > 1 && $1 == event && $9 == "complete" {found=1} END {exit !found}' \
    "$progress"; then
    return
  fi
  if [[ -f $run_dir/run_metadata.csv ]]; then
    record_progress "$event_id" control '' '' '' ablation_guard \
      "$run_id" complete "$run_dir" ''
    return
  fi
  if [[ -e $run_dir ]]; then
    echo "Control incompleto; debe revisarse antes de reanudar: $run_dir" >&2
    exit 1
  fi
  local log="$logs/control_${run_id}.log"
  if ! "$script_dir/run_ohead_ablation_experiment.sh" \
      clean ablation_guard "$run_id" >"$log" 2>&1; then
    record_progress "$event_id" control '' '' '' ablation_guard \
      "$run_id" runtime_failure "$run_dir" ''
    echo "Falló el control $run_id; consulte $log" >&2
    exit 1
  fi
  record_progress "$event_id" control '' '' '' ablation_guard \
    "$run_id" complete "$run_dir" ''
}

slot_is_complete() {
  local order=$1
  awk -F, -v order="$order" \
    'NR > 1 && $3 == "experimental" && $4 == order && $9 == "valid" \
      {found=1} END {exit !found}' "$progress"
}

replacement_count() {
  local order=$1
  awk -F, -v order="$order" \
    'NR > 1 && $3 == "experimental" && $4 == order && \
      ($9 == "invalid_integrity" || $9 == "replacement_authorized") \
      {count++} END {print count+0}' \
    "$progress"
}

blocking_outcome() {
  local order=$1 attempt=$2
  local prefix="plan_${order}_attempt_${attempt}_"
  awk -F, -v prefix="$prefix" \
    'NR > 1 && index($1, prefix) == 1 && \
      ($9 == "runtime_failure" || $9 == "audit_error" || \
       $9 == "actuator_application_error") {outcome=$9} \
      END {print outcome}' "$progress"
}

report_status() {
  python3 - "$1" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1])).get("status", "audit_error"))
PY
}

report_field() {
  python3 - "$1" "$2" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))
PY
}

promote_audit_report() {
  local temporary=$1 destination=$2
  if [[ ! -f $temporary ]]; then
    return 1
  fi
  mv -f "$temporary" "$destination"
}

run_slot() {
  local order=$1 block=$2 arm=$3 variant=$4 planned_id=$5
  if slot_is_complete "$order"; then
    echo "trabajo $order ya aceptado"
    return
  fi
  local attempt
  attempt=$(replacement_count "$order")
  while (( attempt < 10 )); do
    local run_id=$planned_id
    if (( attempt > 0 )); then
      run_id=$((planned_id + attempt * 1000))
    fi
    local event_prefix="plan_${order}_attempt_${attempt}"
    local run_dir="$results/dynamic/$variant/run_$run_id"
    local audit_report="$audits/${variant}_run_${run_id}.json"
    local integrity_report="$audits/${variant}_run_${run_id}_integrity.json"
    local equivalence=
    local disposition=
    local controller_disposition=
    local controller_status=
    local log="$logs/${variant}_run_${run_id}.log"

    local blocked
    blocked=$(blocking_outcome "$order" "$attempt")
    if [[ -n $blocked ]]; then
      local failure_disposition
      failure_disposition="$failure_disposition_dir/${event_prefix}.json"
      if ! python3 "$project_dir/analysis/analyze_ohead_ablation.py" \
          --protocol "$protocol" \
          --validate-failure-disposition "$failure_disposition" \
          --plan-execution-order "$order" --attempt "$attempt" \
          --run-id "$run_id" >/dev/null; then
        echo "El intento $event_prefix sigue bloqueado por $blocked" >&2
        echo "Falta una disposición válida: $failure_disposition" >&2
        exit 1
      fi
      record_progress "${event_prefix}_replacement_authorized" experimental \
        "$order" "$block" "$arm" "$variant" "$run_id" \
        replacement_authorized "$run_dir" "$failure_disposition"
      attempt=$((attempt + 1))
      continue
    fi

    if [[ ! -e $run_dir ]]; then
      echo "[$order/400] $arm, corrida $run_id"
      if ! "$script_dir/run_ohead_ablation_experiment.sh" \
          dynamic "$variant" "$run_id" >"$log" 2>&1; then
        record_progress "${event_prefix}_runtime_failure" experimental \
          "$order" "$block" \
          "$arm" "$variant" "$run_id" runtime_failure "$run_dir" ''
        echo "Falló la ejecución; la campaña se detiene para diagnóstico: $log" >&2
        exit 1
      fi
    elif [[ ! -f $run_dir/run_metadata.csv ]]; then
      record_progress "${event_prefix}_runtime_failure" experimental \
        "$order" "$block" "$arm" "$variant" "$run_id" \
        runtime_failure "$run_dir" ''
      echo "La ejecución previa quedó incompleta: $run_dir" >&2
      echo "Se requiere diagnóstico antes de autorizar un reemplazo" >&2
      exit 1
    fi

    if [[ $arm == assistant ]]; then
      controller_disposition="$controller_disposition_dir/run_${run_id}.json"
    fi
    integrity_args=(
      --protocol "$protocol" --plan "$plan" --audit-run "$run_dir"
      --arm "$arm" --variant "$variant" --run-id "$run_id"
      --integrity-only
    )
    if [[ $arm == assistant && -f $controller_disposition ]]; then
      integrity_args+=(--controller-disposition "$controller_disposition")
    fi
    local integrity_temporary="${integrity_report}.tmp.$$"
    if [[ -e $integrity_temporary ]]; then
      echo "Existe un informe temporal inesperado: $integrity_temporary" >&2
      exit 1
    fi
    set +e
    python3 "$project_dir/analysis/analyze_ohead_ablation.py" \
      "${integrity_args[@]}" --audit-output "$integrity_temporary" \
      >>"$log" 2>&1
    integrity_rc=$?
    set -e
    if ! promote_audit_report "$integrity_temporary" "$integrity_report"; then
      record_progress "${event_prefix}_audit_error" experimental "$order" \
        "$block" "$arm" "$variant" "$run_id" audit_error "$run_dir" ''
      echo "El auditor no produjo un informe; se detiene la campaña" >&2
      exit 1
    fi
    integrity_status=$(report_status "$integrity_report")
    if [[ $integrity_status == invalid_integrity ]]; then
      record_progress "${event_prefix}_invalid_integrity" experimental \
        "$order" "$block" "$arm" "$variant" "$run_id" \
        invalid_integrity "$run_dir" "$integrity_report"
      attempt=$((attempt + 1))
      continue
    fi
    if [[ $integrity_rc -ne 0 || $integrity_status != valid_integrity ]]; then
      record_progress "${event_prefix}_${integrity_status}" experimental \
        "$order" "$block" "$arm" "$variant" "$run_id" \
        "$integrity_status" "$run_dir" "$integrity_report"
      echo "La auditoría exige diagnóstico: $integrity_report" >&2
      exit 1
    fi

    if [[ $arm == assistant ]]; then
      equivalence="$equivalence_dir/run_${run_id}.json"
      disposition="$disposition_dir/run_${run_id}.json"
      controller_status=$(report_field "$integrity_report" \
        controller_crosscheck_status)
      if [[ $controller_status != readiness_failure_retained && \
            ! -f $equivalence ]]; then
        set +e
        python3 "$project_dir/analysis/validate_v2_equivalence.py" \
            --run-dir "$run_dir" \
            --selection "$project_dir/experiments/frozen/detector_selection_pilot2.json" \
            --output "$equivalence" >>"$log" 2>&1
        equivalence_rc=$?
        set -e
        if [[ $equivalence_rc -ne 0 && ! -f $equivalence ]]; then
          if [[ ! -f $disposition ]]; then
            record_progress "${event_prefix}_equivalence_discrepancy" \
              experimental "$order" "$block" "$arm" "$variant" \
              "$run_id" equivalence_discrepancy "$run_dir" ''
            echo "El replay requiere diagnóstico y disposición: $log" >&2
            exit 1
          fi
          record_progress "${event_prefix}_reference_failure_retained" \
            experimental "$order" "$block" "$arm" "$variant" "$run_id" \
            reference_failure_retained "$run_dir" "$disposition"
        fi
      fi
    fi

    audit_args=(
      --protocol "$protocol" --plan "$plan" --audit-run "$run_dir"
      --arm "$arm" --variant "$variant" --run-id "$run_id"
    )
    if [[ -n $equivalence ]]; then
      audit_args+=(--equivalence "$equivalence")
    fi
    if [[ -n $disposition && -f $disposition ]]; then
      audit_args+=(--equivalence-disposition "$disposition")
    fi
    if [[ -n $controller_disposition && -f $controller_disposition ]]; then
      audit_args+=(--controller-disposition "$controller_disposition")
    fi
    local audit_temporary="${audit_report}.tmp.$$"
    if [[ -e $audit_temporary ]]; then
      echo "Existe un informe temporal inesperado: $audit_temporary" >&2
      exit 1
    fi
    set +e
    python3 "$project_dir/analysis/analyze_ohead_ablation.py" \
      "${audit_args[@]}" --audit-output "$audit_temporary" \
      >>"$log" 2>&1
    audit_rc=$?
    set -e
    if ! promote_audit_report "$audit_temporary" "$audit_report"; then
      record_progress "${event_prefix}_audit_error" experimental "$order" \
        "$block" "$arm" "$variant" "$run_id" audit_error "$run_dir" ''
      echo "El auditor no produjo el informe final; se detiene la campaña" >&2
      exit 1
    fi
    audit_status=$(report_status "$audit_report")
    if [[ $audit_rc -eq 0 && $audit_status == valid ]]; then
      record_progress "${event_prefix}_valid" experimental "$order" "$block" \
        "$arm" "$variant" "$run_id" valid "$run_dir" "$audit_report"
      return
    fi
    if [[ $audit_status == invalid_integrity ]]; then
      record_progress "${event_prefix}_invalid_integrity" experimental \
        "$order" "$block" "$arm" "$variant" "$run_id" \
        invalid_integrity "$run_dir" "$audit_report"
      attempt=$((attempt + 1))
      continue
    fi
    record_progress "${event_prefix}_${audit_status}" experimental "$order" \
      "$block" "$arm" "$variant" "$run_id" "$audit_status" "$run_dir" \
      "$audit_report"
    echo "La campaña se detiene para diagnóstico: $audit_report" >&2
    exit 1
  done
  echo "El trabajo $order agotó diez intentos; se detiene la campaña" >&2
  exit 1
}

for index in $(seq 1 5); do run_control "pre_$index"; done

while IFS=, read -r order block _position arm variant run_id; do
  [[ $order == execution_order ]] && continue
  run_slot "$order" "$block" "$arm" "$variant" "$run_id"
  if (( order % 100 == 0 )); then
    run_control "mid_$order"
  fi
done < "$plan"

for index in $(seq 1 5); do run_control "post_$index"; done

python3 "$project_dir/analysis/analyze_ohead_ablation.py" \
  --protocol "$protocol" --plan "$plan" --results "$results" \
  --progress "$progress" --output "$results/comparison.json" \
  --runs-csv "$results/runs.csv"
