#!/usr/bin/env python3
"""Genera y congela el orden de ejecución de un screening V2."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import random
from pathlib import Path


FIELDS = (
    "step",
    "kind",
    "scenario",
    "variant",
    "run_id",
    "ab_ordinal",
    "runs_per_variant",
    "random_seed",
    "checkpoint_every",
)


def build_rows(label: str, control_variant: str, treatment_variant: str,
               runs: int, random_seed: int,
               checkpoint_every: int) -> list[dict[str, object]]:
    if runs < 1:
        raise ValueError("runs debe ser positivo")
    if checkpoint_every < 0:
        raise ValueError("checkpoint_every no puede ser negativo")

    jobs = [
        (variant, run_id)
        for variant in (control_variant, treatment_variant)
        for run_id in range(1, runs + 1)
    ]
    random.Random(random_seed).shuffle(jobs)

    rows: list[dict[str, object]] = []
    step = 0
    for ordinal, (variant, run_id) in enumerate(jobs, start=1):
        step += 1
        rows.append({
            "step": step,
            "kind": "experiment",
            "scenario": "dynamic",
            "variant": variant,
            "run_id": run_id,
            "ab_ordinal": ordinal,
            "runs_per_variant": runs,
            "random_seed": random_seed,
            "checkpoint_every": checkpoint_every,
        })
        if (checkpoint_every > 0
                and ordinal % checkpoint_every == 0
                and ordinal < len(jobs)):
            step += 1
            rows.append({
                "step": step,
                "kind": "checkpoint",
                "scenario": "clean",
                "variant": "vanilla",
                "run_id": f"{label}_checkpoint_{ordinal:04d}",
                "ab_ordinal": ordinal,
                "runs_per_variant": runs,
                "random_seed": random_seed,
                "checkpoint_every": checkpoint_every,
            })
    return rows


def render(rows: list[dict[str, object]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def freeze(path: Path, content: str) -> str:
    if path.exists():
        if path.read_text() != content:
            raise ValueError(
                f"El plan existente no coincide con la solicitud: {path}")
        return "existing"

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content)
    temporary.replace(path)
    return "created"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--control-variant", required=True)
    parser.add_argument("--treatment-variant", required=True)
    parser.add_argument("--runs", type=int, required=True)
    parser.add_argument("--random-seed", type=int, required=True)
    parser.add_argument("--checkpoint-every", type=int, default=0)
    args = parser.parse_args()

    rows = build_rows(
        args.label, args.control_variant, args.treatment_variant,
        args.runs, args.random_seed, args.checkpoint_every)
    content = render(rows)
    path = Path(args.output)
    try:
        state = freeze(path, content)
    except ValueError as error:
        raise SystemExit(str(error)) from error

    print(json.dumps({
        "status": state,
        "path": str(path),
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "rows": len(rows),
        "experimental_runs": sum(row["kind"] == "experiment" for row in rows),
        "checkpoints": sum(row["kind"] == "checkpoint" for row in rows),
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
