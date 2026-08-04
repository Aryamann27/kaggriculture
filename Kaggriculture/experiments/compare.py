"""Create paired seed-by-seed comparisons against an experiment baseline."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare strategy experiment results.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--baseline", required=True, help="Strategy ID used as paired control.")
    parser.add_argument("--results-dir", type=Path, default=ROOT / "results")
    parser.add_argument("--output", type=Path, help="Optional Markdown report path.")
    return parser.parse_args()


def load_rows(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(run_dir.glob("**/*.jsonl")):
        rows.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    return rows


def paired_report(rows: list[dict[str, Any]], baseline: str) -> list[dict[str, Any]]:
    by_strategy: dict[str, dict[tuple[str, int], dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        by_strategy[row["strategy"]][(row["opponent"], int(row["seed"]))] = row

    baseline_rows = by_strategy.get(baseline)
    if not baseline_rows:
        raise ValueError(f"Baseline {baseline!r} has no result rows")

    reports: list[dict[str, Any]] = []
    for strategy, strategy_rows in sorted(by_strategy.items()):
        if strategy == baseline:
            continue
        keys = sorted(set(baseline_rows) & set(strategy_rows))
        deltas = [
            float(strategy_rows[key]["agent_reward"])
            - float(baseline_rows[key]["agent_reward"])
            for key in keys
        ]
        if not deltas:
            continue
        wins = sum(delta > 0 for delta in deltas)
        losses = sum(delta < 0 for delta in deltas)
        reports.append(
            {
                "strategy": strategy,
                "paired_episodes": len(deltas),
                "better_seeds": wins,
                "worse_seeds": losses,
                "tied_seeds": len(deltas) - wins - losses,
                "mean_delta": round(mean(deltas), 3),
                "median_delta": round(median(deltas), 3),
                "std_delta": round(pstdev(deltas), 3) if len(deltas) > 1 else 0.0,
                "min_delta": min(deltas),
                "max_delta": max(deltas),
            }
        )
    return reports


def render_markdown(run_id: str, baseline: str, reports: list[dict[str, Any]]) -> str:
    lines = [
        f"# Strategy comparison: {run_id}",
        "",
        f"Paired final-bank deltas use `{baseline}` on the same `(opponent, seed)`.",
        "",
        "| Strategy | Pairs | Better | Worse | Mean delta | Median delta | Std dev |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for report in reports:
        lines.append(
            "| {strategy} | {paired_episodes} | {better_seeds} | {worse_seeds} | "
            "${mean_delta:,.1f} | ${median_delta:,.1f} | ${std_delta:,.1f} |".format(
                **report
            )
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    args = parse_args()
    run_dir = args.results_dir.expanduser().resolve() / args.run_id
    rows = load_rows(run_dir)
    if not rows:
        raise SystemExit(f"No JSONL rows found under {run_dir}")
    reports = paired_report(rows, args.baseline)
    markdown = render_markdown(args.run_id, args.baseline, reports)
    output = args.output or run_dir / f"compare-vs-{args.baseline}.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown, encoding="utf-8")
    print(markdown, end="")


if __name__ == "__main__":
    main()
