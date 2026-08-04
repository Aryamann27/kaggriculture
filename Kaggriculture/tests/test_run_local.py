"""Tests for the strategy-agnostic local benchmark runner."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from run_local import _farm_snapshot, append_result, load_agent, read_seeds


class RunLocalTests(unittest.TestCase):
    def test_load_agent_from_an_arbitrary_python_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            agent_path = Path(temporary_directory) / "candidate.py"
            agent_path.write_text(
                "from dataclasses import dataclass\n"
                "\n"
                "@dataclass\n"
                "class StrategyConfig:\n"
                "    action: str = 'PASS'\n"
                "\n"
                "def agent(obs):\n"
                "    return {'farmer': [StrategyConfig().action], 'hands': [], 'market': []}\n",
                encoding="utf-8",
            )

            candidate = load_agent(agent_path)

        self.assertEqual(
            candidate({}),
            {"farmer": ["PASS"], "hands": [], "market": []},
        )

    def test_read_seed_file_ignores_comments_and_blank_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            seed_path = Path(temporary_directory) / "seeds.txt"
            seed_path.write_text("# benchmark\n100\n\n101\n", encoding="utf-8")

            self.assertEqual(read_seeds(seed_path, seed=0, episodes=0), [100, 101])

    def test_append_result_writes_json_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "result.jsonl"
            row = {"strategy": "control", "seed": 100, "agent_reward": 123.0}

            append_result(output_path, row)

            self.assertEqual(
                json.loads(output_path.read_text(encoding="utf-8")),
                row,
            )

    def test_farm_snapshot_captures_strategy_diagnostics(self) -> None:
        observation = {
            "farms": [
                {
                    "money": 1234.0,
                    "unlocked_quadrants": ["NW", "NE"],
                    "tiles": [
                        [
                            None,
                            {"kind": "WEED"},
                            {"kind": "PLANT", "crop": "TOMATO"},
                            {"kind": "PASTURE", "animal": "COW"},
                        ]
                    ],
                }
            ],
            "private": {"shed": {"WHEAT": 3, "MILK": 0}},
        }

        snapshot = _farm_snapshot(observation, player=0)

        self.assertEqual(snapshot["bank"], 1234.0)
        self.assertEqual(snapshot["unlocked_quadrants"], 2)
        self.assertEqual(snapshot["counts"]["weeds"], 1)
        self.assertEqual(snapshot["crops"], {"TOMATO": 1})
        self.assertEqual(snapshot["animals"], {"COW": 1})
        self.assertEqual(snapshot["shed"], {"WHEAT": 3})


if __name__ == "__main__":
    unittest.main()
