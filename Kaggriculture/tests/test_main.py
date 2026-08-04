"""Unit and integration smoke tests for the crop-only submission agent."""

from __future__ import annotations

import unittest

from kaggle_environments import make

from main import (
    CROPS,
    FieldPlan,
    _allocate_by_ratio,
    _field_tasks,
    _market_orders,
    _move_toward,
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


def empty_plan(**overrides) -> FieldPlan:
    """A crop-only field plan with harmless, overridable defaults."""
    base = dict(
        tasks=[],
        field_capacity=25,
        crop_active={"WHEAT": 0, "CARROT": 0, "TOMATO": 0, "MELON": 0},
        crop_maturing={"WHEAT": 0, "CARROT": 0, "TOMATO": 0, "MELON": 0},
        crop_targets={"WHEAT": 8, "CARROT": 6, "TOMATO": 7, "MELON": 4},
    )
    base.update(overrides)
    return FieldPlan(**base)


class HarvestTimingTests(unittest.TestCase):
    def test_wheat_is_not_harvested_at_first_yield_day(self) -> None:
        farm = make_farm()
        farm["tiles"][0][0] = {
            "kind": "PLANT",
            "crop": "WHEAT",
            "planted_day": 0,
            "watered_today": True,
            "yield_units": 2,
        }

        plan = _field_tasks(farm, {"seeds": {}}, day=2)

        actions_here = [task.action[0] for task in plan.tasks if task.position == (0, 0)]
        self.assertNotIn("HARVEST", actions_here)

    def test_wheat_harvests_after_the_bonus_window(self) -> None:
        farm = make_farm()
        farm["tiles"][0][0] = {
            "kind": "PLANT",
            "crop": "WHEAT",
            "planted_day": 0,
            "watered_today": True,
            "yield_units": 4,
        }

        plan = _field_tasks(farm, {"seeds": {}}, day=5)

        actions_here = [task.action[0] for task in plan.tasks if task.position == (0, 0)]
        self.assertIn("HARVEST", actions_here)

    def test_wheat_harvests_early_at_its_hard_cap(self) -> None:
        farm = make_farm()
        farm["tiles"][0][0] = {
            "kind": "PLANT",
            "crop": "WHEAT",
            "planted_day": 0,
            "watered_today": True,
            "yield_units": CROPS["WHEAT"].max_yield,
        }

        plan = _field_tasks(farm, {"seeds": {}}, day=3)

        actions_here = [task.action[0] for task in plan.tasks if task.position == (0, 0)]
        self.assertIn("HARVEST", actions_here)

    def test_carrot_waits_through_its_final_bonus_day(self) -> None:
        farm = make_farm()
        farm["tiles"][0][0] = {
            "kind": "PLANT",
            "crop": "CARROT",
            "planted_day": 0,
            "watered_today": True,
            "yield_units": 3,
        }

        on_final_bonus_day = _field_tasks(farm, {"seeds": {}}, day=3)
        after_bonus_window = _field_tasks(farm, {"seeds": {}}, day=4)
        final_day_actions = [
            task.action[0]
            for task in on_final_bonus_day.tasks
            if task.position == (0, 0)
        ]
        after_window_actions = [
            task.action[0]
            for task in after_bonus_window.tasks
            if task.position == (0, 0)
        ]

        self.assertNotIn("HARVEST", final_day_actions)
        self.assertIn("HARVEST", after_window_actions)


class FieldPlanningTests(unittest.TestCase):
    def test_largest_remainder_allocation_fills_the_field(self) -> None:
        allocation = _allocate_by_ratio(
            25,
            ["WHEAT", "CARROT", "TOMATO", "MELON"],
            {crop: CROPS[crop].allocation_ratio for crop in CROPS},
        )

        self.assertEqual(allocation, {"WHEAT": 8, "CARROT": 6, "TOMATO": 7, "MELON": 4})
        self.assertEqual(sum(allocation.values()), 25)

    def test_empty_field_plants_all_four_crops_without_structures(self) -> None:
        farm = make_farm()
        private = {
            "seeds": {"WHEAT": 25, "CARROT": 25, "TOMATO": 25, "MELON": 25},
        }

        plan = _field_tasks(farm, private, day=0)

        planted = [task.action for task in plan.tasks if task.action[0] == "PLANT"]
        planted_crops = {action[1] for action in planted}
        self.assertEqual(planted_crops, set(CROPS))
        self.assertEqual(len(planted), 25)
        self.assertEqual(sum(plan.crop_targets.values()), plan.field_capacity)
        self.assertFalse(any(action[0].startswith("BUILD_") for action in planted))

    def test_weeds_are_cleared_before_new_planting_tasks(self) -> None:
        farm = make_farm()
        farm["tiles"][0][0] = {"kind": "WEED"}

        plan = _field_tasks(
            farm,
            {"seeds": {"WHEAT": 25, "CARROT": 25, "TOMATO": 25, "MELON": 25}},
            day=0,
        )

        weed_task = next(task for task in plan.tasks if task.position == (0, 0))
        self.assertEqual(weed_task.action, ["DIG"])
        self.assertGreater(weed_task.priority, 40)


class MarketOrderTests(unittest.TestCase):
    def test_sell_thresholds_are_crop_specific(self) -> None:
        farm = make_farm()
        private = {"shed": {"WHEAT": 5, "MELON": 5}, "seeds": {}}
        market = {"prices": {"WHEAT": 20, "MELON": 100}}

        orders = _market_orders(farm, private, market, day=5, hour=1, plan=empty_plan())

        self.assertIn(["SELL", "WHEAT", 5], orders)
        self.assertFalse(any(order[:2] == ["SELL", "MELON"] for order in orders))

    def test_melon_sales_are_capped_before_endgame(self) -> None:
        farm = make_farm()
        private = {"shed": {"MELON": 12}, "seeds": {}}
        market = {"prices": {"MELON": 250}}

        orders = _market_orders(farm, private, market, day=10, hour=1, plan=empty_plan())

        self.assertIn(["SELL", "MELON", 5], orders)

    def test_daily_worker_schedule_fills_six_hand_roster(self) -> None:
        farm = make_farm()
        market = {"prices": {}}

        orders = _market_orders(farm, {"shed": {}, "seeds": {}}, market, day=0, hour=0, plan=empty_plan())

        self.assertEqual(sum(order == ["HIRE"] for order in orders), 6)
        self.assertLessEqual(len(orders), 10)

    def test_market_never_queues_animal_or_feed_orders(self) -> None:
        farm = make_farm()
        private = {"shed": {}, "seeds": {}}
        market = {"prices": {"WHEAT": 25}}

        orders = _market_orders(farm, private, market, day=0, hour=1, plan=empty_plan())

        forbidden = {"BUY_ANIMAL", "BUY_PRODUCT"}
        self.assertFalse(any(order[0] in forbidden for order in orders))

    def test_land_purchase_requires_a_full_crop_field_and_reserve(self) -> None:
        farm = make_farm()
        farm["money"] = 1600.0
        plan = empty_plan(
            crop_active={"WHEAT": 8, "CARROT": 6, "TOMATO": 7, "MELON": 4},
        )

        orders = _market_orders(
            farm,
            {"shed": {}, "seeds": {"WHEAT": 8, "CARROT": 6, "TOMATO": 7, "MELON": 4}},
            {"prices": {}},
            day=5,
            hour=1,
            plan=plan,
        )

        self.assertIn(["BUY_LAND"], orders)


class AgentIntegrationTests(unittest.TestCase):
    def test_agent_returns_one_action_per_current_hand(self) -> None:
        farm = make_farm()
        farm["hands"] = [[1, 2], [2, 1]]

        action = agent(
            {
                "player": 0,
                "day": 0,
                "hour": 0,
                "farms": [farm],
                "private": {"shed": {}, "seeds": {}, "inventories": [{}, {}, {}]},
                "market": {"prices": {"WHEAT": 25}},
            }
        )

        self.assertEqual(len(action["hands"]), 2)
        self.assertTrue(action["farmer"])
        self.assertLessEqual(len(action["market"]), 10)
        self.assertFalse(
            any(order[0] in {"BUY_ANIMAL", "BUY_PRODUCT"} for order in action["market"])
        )

    def test_final_day_does_not_create_unsellable_work(self) -> None:
        farm = make_farm()
        farm["tiles"][0][0] = {
            "kind": "PLANT",
            "crop": "WHEAT",
            "planted_day": 25,
            "watered_today": True,
            "yield_units": 3,
        }

        plan = _field_tasks(farm, {"seeds": {"WHEAT": 0}}, day=29)

        self.assertEqual(plan.tasks, [])


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
