#!/usr/bin/env python3
"""Genera el orden congelado para la matriz V2 de retardo y pérdida."""

from __future__ import annotations

import argparse
import csv
import json
import random
from pathlib import Path


FIELDS = [
    "execution_order", "condition", "arm", "run_id", "phase_high_s",
    "phase_low_s", "duration_s", "delay_ms", "loss_percent", "variant",
]


def build(config: dict) -> list[dict[str, object]]:
    runs = int(config["runs_per_variant"])
    candidates = [int(value) for value in config["phase_high_candidates_s"]]
    rng = random.Random(int(config["plan_seed"]))
    jobs: list[dict[str, object]] = []
    for condition in config["conditions"]:
        schedule = (candidates * ((runs + len(candidates) - 1) // len(candidates)))[:runs]
        rng.shuffle(schedule)
        for arm in ("vanilla", "assistant"):
            for run_id, phase_high_s in enumerate(schedule, start=1):
                jobs.append({
                    "condition": condition["id"],
                    "arm": arm,
                    "run_id": run_id,
                    "phase_high_s": phase_high_s,
                    "phase_low_s": int(config["phase_low_s"]),
                    "duration_s": int(config["duration_s"]),
                    "delay_ms": condition["delay_ms"],
                    "loss_percent": condition["loss_percent"],
                    "variant": f"network_{condition['id']}_{arm}",
                })
    rng.shuffle(jobs)
    for execution_order, job in enumerate(jobs, start=1):
        job["execution_order"] = execution_order
    return jobs


def normalized(rows: list[dict[str, object]]) -> list[dict[str, str]]:
    return [{field: str(row[field]) for field in FIELDS} for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    expected = normalized(build(config))
    output = Path(args.output)
    if output.exists():
        with output.open(newline="") as stream:
            observed = list(csv.DictReader(stream))
        if observed != expected:
            raise SystemExit("El plan existente no coincide con la matriz congelada")
        print(f"plan existente verificado: {output} ({len(observed)} trabajos)")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(expected)
    print(f"plan generado: {output} ({len(expected)} trabajos)")


if __name__ == "__main__":
    main()
