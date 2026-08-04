"""Run reproducible local Kaggriculture benchmarks.

Examples:
    uv run python run_local.py --opponent starter --episodes 20
    uv run python run_local.py --opponent random --episodes 10 --replay-dir replays
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from typing import Any

from kaggle_environments import make

from main import agent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark the Kaggriculture agent locally.")
    parser.add_argument(
        "--opponent",
        choices=("pass", "random", "starter"),
        default="starter",
        help="Built-in Kaggriculture opponent.",
    )
    parser.add_argument(
        "--episodes",
        type=int,
        default=10,
        help="Number of episodes to run.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Seed for the first episode; subsequent episodes increment it.",
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=720,
        help="Episode length in turns.",
    )
    parser.add_argument(
        "--replay-dir",
        type=Path,
        help="Optional directory in which to save replay JSON files.",
    )
    return parser.parse_args()


def run_episode(opponent: str, seed: int, steps: int) -> tuple[float, float, dict[str, Any]]:
    env = make(
        "kaggriculture",
        configuration={"episodeSteps": steps, "seed": seed},
        debug=True,
    )
    env.run([agent, opponent])
    final = env.steps[-1]
    my_reward = float(final[0].reward or 0)
    opponent_reward = float(final[1].reward or 0)
    return my_reward, opponent_reward, env.toJSON()


def main() -> None:
    args = parse_args()
    if args.episodes <= 0:
        raise SystemExit("--episodes must be positive")
    if args.steps <= 0:
        raise SystemExit("--steps must be positive")

    if args.replay_dir:
        args.replay_dir.mkdir(parents=True, exist_ok=True)

    results: list[tuple[float, float]] = []
    for offset in range(args.episodes):
        seed = args.seed + offset
        my_reward, opponent_reward, replay = run_episode(args.opponent, seed, args.steps)
        results.append((my_reward, opponent_reward))
        outcome = "win" if my_reward > opponent_reward else "loss" if my_reward < opponent_reward else "tie"
        print(
            f"seed={seed} result={outcome:4} "
            f"agent=${my_reward:.0f} opponent=${opponent_reward:.0f}"
        )

        if args.replay_dir:
            replay_path = args.replay_dir / f"{args.opponent}-seed-{seed}.json"
            replay_path.write_text(json.dumps(replay), encoding="utf-8")

    wins = sum(my_reward > opponent_reward for my_reward, opponent_reward in results)
    losses = sum(my_reward < opponent_reward for my_reward, opponent_reward in results)
    ties = len(results) - wins - losses
    print(
        "\nSummary: "
        f"{wins}W-{losses}L-{ties}T | "
        f"agent mean=${mean(my for my, _ in results):.1f} | "
        f"opponent mean=${mean(opponent for _, opponent in results):.1f}"
    )


if __name__ == "__main__":
    main()
