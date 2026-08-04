"""Strategy-specific tests for the staple-velocity candidate."""

from __future__ import annotations

import unittest

from kaggle_environments import make

from main import (
    CROPS,
    MAX_HANDS,
    FieldPlan,
    _field_tasks,
    _market_orders,
    agent,
)


def make_farm(size: int = 5) -> dict:
    return {
        "money": 3000.0,
        "tiles": [[None for _ in range(size)] for _ in range(size)],
        "farmer": [2, 2],
        "hands": [],
        "hires_today": 0,
        "unlocked_quadrants": ["NW"],
    }


def filled_plan(**overrides: object) -> FieldPlan:
    """A fully planted NW plan with no pending seed purchases."""
    base = dict(
        tasks=[],
        field_capacity=25,
        crop_active={"WHEAT": 18, "CARROT": 7},
        crop_maturing={"WHEAT": 0, "CARROT": 0},
        crop_planted_today={"WHEAT": 0, "CARROT": 0},
        crop_targets={"WHEAT": 18, "CARROT": 7},
    )
    base.update(overrides)
    return FieldPlan(**base)


class HarvestTimingTests(unittest.TestCase):
    def test_wheat_waits_for_the_bonus_window_to_close(self) -> None:
        farm = make_farm()
        farm["tiles"][0][0] = {
            "kind": "PLANT",
            "crop": "WHEAT",
            "planted_day": 0,
            "watered_today": True,
            "yield_units": 2,
        }

        plan = _field_tasks(farm, {"seeds": {}}, day=2)
        actions = [task.action[0] for task in plan.tasks if task.position == (0, 0)]

        self.assertNotIn("HARVEST", actions)

    def test_wheat_harvests_after_its_bonus_window(self) -> None:
        farm = make_farm()
        farm["tiles"][0][0] = {
            "kind": "PLANT",
            "crop": "WHEAT",
            "planted_day": 0,
            "watered_today": True,
            "yield_units": 5,
        }

        plan = _field_tasks(farm, {"seeds": {}}, day=5)
        actions = [task.action[0] for task in plan.tasks if task.position == (0, 0)]

        self.assertIn("HARVEST", actions)

    def test_carrot_waits_for_its_bonus_window_to_close(self) -> None:
        farm = make_farm()
        farm["tiles"][0][0] = {
            "kind": "PLANT",
            "crop": "CARROT",
            "planted_day": 0,
            "watered_today": True,
            "yield_units": 2,
        }

        plan = _field_tasks(farm, {"seeds": {}}, day=2)
        actions = [task.action[0] for task in plan.tasks if task.position == (0, 0)]

        self.assertNotIn("HARVEST", actions)

    def test_carrot_harvests_after_its_bonus_window(self) -> None:
        farm = make_farm()
        farm["tiles"][0][0] = {
            "kind": "PLANT",
            "crop": "CARROT",
            "planted_day": 0,
            "watered_today": True,
            "yield_units": 4,
        }

        plan = _field_tasks(farm, {"seeds": {}}, day=4)
        actions = [task.action[0] for task in plan.tasks if task.position == (0, 0)]

        self.assertIn("HARVEST", actions)


class StapleAllocationTests(unittest.TestCase):
    def test_only_wheat_and_carrot_are_active_crop_types(self) -> None:
        farm = make_farm()
        plan = _field_tasks(
            farm,
            {"seeds": {"WHEAT": 25, "CARROT": 25, "TOMATO": 25}},
            day=0,
        )

        planted = {task.action[1] for task in plan.tasks if task.action[0] == "PLANT"}

        self.assertEqual(set(CROPS), {"WHEAT", "CARROT"})
        self.assertEqual(planted, {"WHEAT", "CARROT"})
        self.assertFalse(
            any(task.action[0].startswith("BUILD_") for task in plan.tasks)
        )

    def test_first_day_is_limited_to_a_safe_harvest_cohort(self) -> None:
        farm = make_farm()
        plan = _field_tasks(
            farm,
            {"seeds": {"WHEAT": 25, "CARROT": 25}},
            day=0,
        )
        planted = [
            task.action[1] for task in plan.tasks if task.action[0] == "PLANT"
        ]

        self.assertEqual(planted.count("WHEAT"), 5)
        self.assertEqual(planted.count("CARROT"), 2)

    def test_final_day_creates_no_unsellable_field_work(self) -> None:
        farm = make_farm()
        farm["tiles"][0][0] = {
            "kind": "PLANT",
            "crop": "WHEAT",
            "planted_day": 23,
            "watered_today": True,
            "yield_units": 5,
        }

        plan = _field_tasks(farm, {"seeds": {"WHEAT": 1}}, day=29)

        self.assertEqual(plan.tasks, [])


class MarketPolicyTests(unittest.TestCase):
    def test_wheat_sells_a_small_lot_at_its_liquid_threshold(self) -> None:
        farm = make_farm()
        farm["hands"] = [[2, 2]] * MAX_HANDS
        farm["unlocked_quadrants"] = ["NW", "NE"]
        orders = _market_orders(
            farm,
            {"shed": {"WHEAT": 3}, "seeds": {}},
            {"prices": {"WHEAT": 15, "CARROT": 35}},
            day=5,
            hour=1,
            plan=filled_plan(),
        )

        self.assertIn(["SELL", "WHEAT", 3], orders)

    def test_carrot_holds_below_its_glut_protection_threshold(self) -> None:
        farm = make_farm()
        farm["hands"] = [[2, 2]] * MAX_HANDS
        farm["unlocked_quadrants"] = ["NW", "NE"]
        orders = _market_orders(
            farm,
            {"shed": {"CARROT": 16}, "seeds": {}},
            {"prices": {"WHEAT": 25, "CARROT": 29}},
            day=5,
            hour=1,
            plan=filled_plan(),
        )

        self.assertFalse(any(order[:2] == ["SELL", "CARROT"] for order in orders))

    def test_carrot_sells_in_eight_unit_batches_once_price_recovers(self) -> None:
        farm = make_farm()
        farm["hands"] = [[2, 2]] * MAX_HANDS
        farm["unlocked_quadrants"] = ["NW", "NE"]
        orders = _market_orders(
            farm,
            {"shed": {"CARROT": 16}, "seeds": {}},
            {"prices": {"WHEAT": 25, "CARROT": 30}},
            day=5,
            hour=1,
            plan=filled_plan(),
        )

        self.assertIn(["SELL", "CARROT", 8], orders)

    def test_shed_pressure_overrides_carrot_throttle(self) -> None:
        farm = make_farm()
        farm["hands"] = [[2, 2]] * MAX_HANDS
        farm["unlocked_quadrants"] = ["NW", "NE"]
        orders = _market_orders(
            farm,
            {"shed": {"CARROT": 80}, "seeds": {}},
            {"prices": {"WHEAT": 25, "CARROT": 1}},
            day=5,
            hour=1,
            plan=filled_plan(),
        )

        self.assertIn(["SELL", "CARROT", 16], orders)


class LandExpansionTests(unittest.TestCase):
    def test_ne_land_requires_documented_cash_and_saturation(self) -> None:
        farm = make_farm()
        farm["money"] = 2300.0
        farm["hands"] = [[2, 2]] * MAX_HANDS

        orders = _market_orders(
            farm,
            {"shed": {}, "seeds": {}},
            {"prices": {}},
            day=25,
            hour=1,
            plan=filled_plan(),
        )

        self.assertIn(["BUY_LAND"], orders)

    def test_ne_land_does_not_buy_below_cash_trigger(self) -> None:
        farm = make_farm()
        farm["money"] = 2299.0
        farm["hands"] = [[2, 2]] * MAX_HANDS

        orders = _market_orders(
            farm,
            {"shed": {}, "seeds": {}},
            {"prices": {}},
            day=25,
            hour=1,
            plan=filled_plan(),
        )

        self.assertNotIn(["BUY_LAND"], orders)

    def test_ne_land_does_not_buy_before_saturation(self) -> None:
        farm = make_farm()
        farm["money"] = 5000.0
        farm["hands"] = [[2, 2]] * MAX_HANDS
        sparse_plan = filled_plan(
            crop_active={"WHEAT": 14, "CARROT": 5},
            crop_targets={"WHEAT": 18, "CARROT": 7},
        )

        orders = _market_orders(
            farm,
            {"shed": {}, "seeds": {}},
            {"prices": {}},
            day=25,
            hour=1,
            plan=sparse_plan,
        )

        self.assertNotIn(["BUY_LAND"], orders)

    def test_strategy_never_buys_a_second_land_quadrant(self) -> None:
        farm = make_farm()
        farm["money"] = 10000.0
        farm["hands"] = [[2, 2]] * MAX_HANDS
        farm["unlocked_quadrants"] = ["NW", "NE"]

        orders = _market_orders(
            farm,
            {"shed": {}, "seeds": {}},
            {"prices": {}},
            day=25,
            hour=1,
            plan=filled_plan(),
        )

        self.assertNotIn(["BUY_LAND"], orders)


class AgentIntegrationTests(unittest.TestCase):
    def test_agent_returns_one_action_per_hand_without_animal_operations(self) -> None:
        farm = make_farm()
        farm["hands"] = [[1, 2], [2, 1]]
        response = agent(
            {
                "player": 0,
                "day": 0,
                "hour": 0,
                "farms": [farm],
                "private": {"shed": {}, "seeds": {}, "inventories": [{}, {}, {}]},
                "market": {"prices": {"WHEAT": 25, "CARROT": 35}},
            }
        )

        forbidden = {
            "BUILD_COOP",
            "BUILD_PASTURE",
            "PLACE",
            "FEED",
            "CARE",
            "COLLECT_FERTILIZER",
        }
        actions = [response["farmer"], *response["hands"]]
        self.assertEqual(len(response["hands"]), 2)
        self.assertFalse(any(action[0] in forbidden for action in actions))
        self.assertFalse(any(order[0] == "BUY_ANIMAL" for order in response["market"]))
        self.assertLessEqual(len(response["market"]), 10)


class AgentEnvironmentSmokeTests(unittest.TestCase):
    def test_agent_runs_a_short_episode(self) -> None:
        env = make(
            "kaggriculture",
            configuration={"episodeSteps": 48, "seed": 11},
            debug=True,
        )
        env.run([agent, "pass"])

        final = env.steps[-1]
        self.assertEqual(final[0].status, "DONE")
        self.assertEqual(final[1].status, "DONE")
        self.assertIsInstance(final[0].reward, float)

    def test_agent_runs_a_full_season_without_error(self) -> None:
        env = make(
            "kaggriculture",
            configuration={"episodeSteps": 720, "seed": 11},
            debug=True,
        )
        env.run([agent, "pass"])

        final = env.steps[-1]
        self.assertEqual(final[0].status, "DONE")
        self.assertGreater(final[0].reward, 3000.0)


if __name__ == "__main__":
    unittest.main()
