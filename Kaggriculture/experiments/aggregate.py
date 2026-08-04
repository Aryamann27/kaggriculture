"""Aggregate JSONL benchmark rows into machine-readable strategy summaries."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aggregate Kaggriculture experiment results.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--results-dir", type=Path, default=ROOT / "results")
    parser.add_argument("--output", type=Path, help="Optional summary JSON output path.")
    return parser.parse_args()


def load_rows(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(run_dir.glob("**/*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["strategy"], row["opponent"])].append(row)

    summaries: list[dict[str, Any]] = []
    for (strategy, opponent), group in sorted(grouped.items()):
        rewards = [float(row["agent_reward"]) for row in group]
        margins = [float(row["margin"]) for row in group]
        outcomes = [row["outcome"] for row in group]
        summaries.append(
            {
                "strategy": strategy,
                "opponent": opponent,
                "episodes": len(group),
                "wins": outcomes.count("win"),
                "losses": outcomes.count("loss"),
                "ties": outcomes.count("tie"),
                "errors": sum(row["error"] is not None for row in group),
                "mean_bank": round(mean(rewards), 3),
                "median_bank": round(median(rewards), 3),
                "std_bank": round(pstdev(rewards), 3) if len(rewards) > 1 else 0.0,
                "min_bank": min(rewards),
                "max_bank": max(rewards),
                "mean_margin": round(mean(margins), 3),
            }
        )
    return {"episodes": len(rows), "summaries": summaries}


def main() -> None:
    args = parse_args()
    run_dir = args.results_dir.expanduser().resolve() / args.run_id
    rows = load_rows(run_dir)
    if not rows:
        raise SystemExit(f"No JSONL rows found under {run_dir}")
    summary = summarize(rows)
    output = args.output or run_dir / "summary.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    for item in summary["summaries"]:
        print(
            f"{item['strategy']:20} vs {item['opponent']:12} "
            f"{item['wins']}W-{item['losses']}L-{item['ties']}T "
            f"mean=${item['mean_bank']:.1f} margin=${item['mean_margin']:.1f} "
            f"std=${item['std_bank']:.1f}"
        )


if __name__ == "__main__":
    main()
