#!/usr/bin/env python3
"""Genera el orden congelado para la ablación de SRTO_OHEADBW."""

from __future__ import annotations

import argparse
import csv
import json
import random
from pathlib import Path


FIELDS = [
    "execution_order", "block", "position", "arm", "variant", "run_id",
]


def build(config: dict) -> list[dict[str, object]]:
    runs = int(config["runs_per_arm"])
    arms = [(str(item["id"]), str(item["variant"]))
            for item in config["arms"]]
    if len(arms) != int(config["block_size"]):
        raise ValueError("block_size debe coincidir con el número de brazos")
    if len({arm for arm, _ in arms}) != len(arms):
        raise ValueError("Los identificadores de brazo deben ser únicos")
    if len({variant for _, variant in arms}) != len(arms):
        raise ValueError("Las variantes deben ser únicas")
    if runs * len(arms) != int(config["valid_experimental_runs"]):
        raise ValueError("valid_experimental_runs no coincide con el diseño")

    rng = random.Random(int(config["plan_seed"]))
    jobs: list[dict[str, object]] = []
    execution_order = 0
    for block in range(1, runs + 1):
        scheduled = list(arms)
        rng.shuffle(scheduled)
        for position, (arm, variant) in enumerate(scheduled, start=1):
            execution_order += 1
            jobs.append({
                "execution_order": execution_order,
                "block": block,
                "position": position,
                "arm": arm,
                "variant": variant,
                "run_id": block,
            })
    return jobs


def normalized(rows: list[dict[str, object]]) -> list[dict[str, str]]:
    return [{field: str(row[field]) for field in FIELDS} for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text())
    if config.get("status") != "frozen_before_execution":
        raise SystemExit("El protocolo de ablación no está congelado")
    expected = normalized(build(config))
    output = Path(args.output)
    if output.exists():
        with output.open(newline="") as stream:
            observed = list(csv.DictReader(stream))
        if observed != expected:
            raise SystemExit("El plan existente no coincide con el protocolo")
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
