"""Execute a reproducible benchmark tier across isolated strategy worktrees.

Example:
    uv run python experiments/run_matrix.py --tier tier1 \
      --worktree multicrop-control=../Kaggriculture-multicrop-control \
      --worktree staple-velocity=../Kaggriculture-staple-velocity \
      --worktree livestock-stack=../Kaggriculture-livestock-stack \
      --worktree town-oracle=../Kaggriculture-town-oracle \
      --worktree ongoing-fertilizer=../Kaggriculture-ongoing-fertilizer \
      --parallel 5 --run-id tier1-2026-08-04
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "experiments" / "manifest.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Kaggriculture experiment tier.")
    parser.add_argument("--tier", required=True, help="Tier key from manifest.json.")
    parser.add_argument(
        "--worktree",
        action="append",
        required=True,
        metavar="STRATEGY=PATH",
        help="Strategy ID and absolute or relative worktree path; repeat per candidate.",
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=ROOT / "results",
        help="Directory for JSONL results and subprocess status files.",
    )
    parser.add_argument(
        "--run-id",
        default=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        help="Stable identifier grouping all results from this invocation.",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="Maximum worktrees to benchmark concurrently.",
    )
    parser.add_argument(
        "--replace-results",
        action="store_true",
        help="Delete existing output files for this run ID before execution.",
    )
    return parser.parse_args()


def load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.expanduser().read_text(encoding="utf-8"))


def parse_worktrees(specs: list[str]) -> dict[str, Path]:
    worktrees: dict[str, Path] = {}
    for spec in specs:
        strategy, separator, raw_path = spec.partition("=")
        if not separator or not strategy or not raw_path:
            raise ValueError(f"Invalid --worktree value {spec!r}; expected STRATEGY=PATH")
        path = Path(raw_path).expanduser().resolve()
        if not (path / "main.py").is_file():
            raise FileNotFoundError(f"Worktree {strategy!r} has no main.py: {path}")
        worktrees[strategy] = path
    return worktrees


def run_command(command: list[str], cwd: Path) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def run_strategy(
    strategy: str,
    worktree: Path,
    tier: str,
    tier_config: dict[str, Any],
    seed_file: Path,
    results_dir: Path,
    run_id: str,
    replace_results: bool,
) -> dict[str, Any]:
    strategy_dir = results_dir / run_id / strategy
    strategy_dir.mkdir(parents=True, exist_ok=True)
    result_path = strategy_dir / f"{tier}.jsonl"
    status_path = strategy_dir / f"{tier}.status.json"
    if replace_results:
        for path in (result_path, status_path):
            if path.exists():
                path.unlink()

    outcomes: list[dict[str, Any]] = []
    if tier == "tier0":
        outcomes.append(
            run_command(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                worktree,
            )
        )

    runner = ROOT / "run_local.py"
    base_command = [
        sys.executable,
        str(runner),
        "--agent-path",
        str(worktree / "main.py"),
        "--seed-file",
        str(seed_file),
        "--steps",
        str(tier_config["steps"]),
        "--output",
        str(result_path),
        "--run-id",
        run_id,
        "--strategy",
        strategy,
        "--quiet",
    ]
    for opponent in tier_config["opponents"]:
        outcomes.append(run_command([*base_command, "--opponent", opponent], worktree))

    if tier_config.get("self_play"):
        outcomes.append(
            run_command(
                [
                    *base_command,
                    "--opponent-agent",
                    str(worktree / "main.py"),
                ],
                worktree,
            )
        )

    status = {
        "strategy": strategy,
        "tier": tier,
        "worktree": str(worktree),
        "run_id": run_id,
        "passed": all(result["returncode"] == 0 for result in outcomes),
        "commands": outcomes,
    }
    status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
    return status


def main() -> None:
    args = parse_args()
    if args.parallel <= 0:
        raise SystemExit("--parallel must be positive")

    manifest = load_manifest(args.manifest)
    tier_config = manifest.get("tiers", {}).get(args.tier)
    if not isinstance(tier_config, dict):
        raise SystemExit(f"Unknown tier {args.tier!r} in {args.manifest}")
    seed_file = args.manifest.parent / tier_config["seed_file"]
    worktrees = parse_worktrees(args.worktree)

    statuses: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as executor:
        futures = {
            executor.submit(
                run_strategy,
                strategy,
                worktree,
                args.tier,
                tier_config,
                seed_file,
                args.results_dir.expanduser().resolve(),
                args.run_id,
                args.replace_results,
            ): strategy
            for strategy, worktree in worktrees.items()
        }
        for future in concurrent.futures.as_completed(futures):
            status = future.result()
            statuses.append(status)
            outcome = "PASS" if status["passed"] else "FAIL"
            print(f"{outcome} {status['strategy']} ({args.tier})")

    failed = [status["strategy"] for status in statuses if not status["passed"]]
    if failed:
        raise SystemExit(f"Failed strategies: {', '.join(sorted(failed))}")


if __name__ == "__main__":
    main()
