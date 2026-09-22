#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
controller="$project_dir/experiments/controller_v2_final.env"
selection="$project_dir/results/v2/detector_selection_pilot2/selection.json"
audit="$project_dir/results/v2/pilot2_audit.json"

if [[ ! -f $controller || ! -f $selection || ! -f $audit ]]; then
  echo "Faltan artefactos congelados del segundo piloto" >&2
  exit 1
fi
source "$controller"

hash_file() {
  if command -v sha256sum >/dev/null; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

actual_selection_sha256=$(hash_file "$selection")
actual_audit_sha256=$(hash_file "$audit")
if [[ $actual_selection_sha256 != "$V2_SELECTION_SHA256" ]]; then
  echo "La selección V2 cambió después de congelarse" >&2
  exit 1
fi
if [[ $actual_audit_sha256 != "$PILOT2_AUDIT_SHA256" ]]; then
  echo "La auditoría del segundo piloto cambió después de congelarse" >&2
  exit 1
fi
python3 - "$audit" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text())
if report.get("status") != "ready_to_freeze":
    raise SystemExit("El segundo piloto no autoriza el A/B final")
PY

printf 'freeze_status=accepted\nselection_sha256=%s\naudit_sha256=%s\n' \
  "$actual_selection_sha256" "$actual_audit_sha256"
