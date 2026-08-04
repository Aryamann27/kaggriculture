"""Unit and integration smoke tests for the submission agent."""

from __future__ import annotations

import unittest

from kaggle_environments import make

from main import (
    ANIMAL_CASH_RESERVE,
    FieldPlan,
    _field_tasks,
    _logistics_actions,
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
    """A FieldPlan with harmless defaults, overridable per test."""
    base = dict(
        tasks=[],
        field_capacity=25,
        crop_active={"WHEAT": 0, "TOMATO": 0, "STRAWBERRY": 0},
        crop_maturing={"WHEAT": 0, "TOMATO": 0, "STRAWBERRY": 0},
        crop_targets={"WHEAT": 7, "TOMATO": 15, "STRAWBERRY": 0},
        animal_targets={"GOOSE": 2},
        occupied_by_animal={"GOOSE": 0},
        structures_built={"COOP": 0},
        empty_structures={"COOP": []},
        unfed_animals=[],
        fertilizable_ongoing=[],
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
        # first_yield_day=2, but max_yield_day=4: harvesting here would
        # throw away two more days of bonus-window watering.
        plan = _field_tasks(farm, {"seeds": {}}, day=2)
        actions_here = [t.action[0] for t in plan.tasks if t.position == (0, 0)]
        self.assertNotIn("HARVEST", actions_here)

    def test_wheat_harvests_once_bonus_window_closes(self) -> None:
        farm = make_farm()
        farm["tiles"][0][0] = {
            "kind": "PLANT",
            "crop": "WHEAT",
            "planted_day": 0,
            "watered_today": True,
            "yield_units": 4,
        }
        plan = _field_tasks(farm, {"seeds": {}}, day=5)  # age 5 > max_yield_day 4
        actions_here = [t.action[0] for t in plan.tasks if t.position == (0, 0)]
        self.assertIn("HARVEST", actions_here)

    def test_wheat_harvests_early_once_hard_cap_reached(self) -> None:
        farm = make_farm()
        farm["tiles"][0][0] = {
            "kind": "PLANT",
            "crop": "WHEAT",
            "planted_day": 0,
            "watered_today": True,
            "yield_units": 6,  # wheat's max_yield
        }
        plan = _field_tasks(farm, {"seeds": {}}, day=3)  # still within the bonus window
        actions_here = [t.action[0] for t in plan.tasks if t.position == (0, 0)]
        self.assertIn("HARVEST", actions_here)


class AllocationTests(unittest.TestCase):
    def test_empty_field_builds_a_tomato_pipeline_and_coops(self) -> None:
        farm = make_farm()
        private = {
            "seeds": {"WHEAT": 25, "TOMATO": 25, "STRAWBERRY": 25},
        }
        plan = _field_tasks(farm, private, day=0)

        planted_crops = {t.action[1] for t in plan.tasks if t.action[0] == "PLANT"}
        built_kinds = {t.action[0] for t in plan.tasks if t.action[0].startswith("BUILD_")}

        self.assertEqual(planted_crops, {"WHEAT", "TOMATO"})
        self.assertEqual(built_kinds, {"BUILD_COOP"})
        self.assertGreater(plan.crop_targets["TOMATO"], plan.crop_targets["WHEAT"])
        self.assertGreater(sum(plan.crop_targets.values()), plan.field_capacity * 0.5)

    def test_goose_target_is_capped_when_more_land_unlocks(self) -> None:
        farm = make_farm(size=10)
        plan = _field_tasks(farm, {"seeds": {}}, day=0)

        self.assertEqual(plan.animal_targets, {"GOOSE": 2})

    def test_strawberry_needs_town_demand_and_cash(self) -> None:
        farm = make_farm()
        private = {"seeds": {}}

        without_demand = _field_tasks(farm, private, day=3, town={"unlocked_shops": []})
        with_demand = _field_tasks(
            farm,
            private,
            day=3,
            town={"unlocked_shops": ["FARMERS_MARKET"]},
        )

        self.assertEqual(without_demand.crop_targets["STRAWBERRY"], 0)
        self.assertGreater(with_demand.crop_targets["STRAWBERRY"], 0)


class FertilizerRoutingTests(unittest.TestCase):
    def test_tomatoes_precede_strawberries_on_production_days(self) -> None:
        farm = make_farm()
        farm["tiles"][0][0] = {
            "kind": "PLANT",
            "crop": "TOMATO",
            "planted_day": 0,
            "watered_today": False,
            "yield_units": 0,
            "fertilized_until_day": -1,
        }
        farm["tiles"][1][0] = {
            "kind": "PLANT",
            "crop": "STRAWBERRY",
            "planted_day": 0,
            "watered_today": False,
            "yield_units": 0,
            "fertilized_until_day": -1,
        }

        plan = _field_tasks(farm, {"seeds": {}}, day=9)

        self.assertEqual(plan.fertilizable_ongoing, [(0, 0), (0, 1)])
        water_targets = {task.position for task in plan.tasks if task.action == ["WATER"]}
        self.assertTrue({(0, 0), (0, 1)} <= water_targets)

    def test_fertilizer_carriers_dispatch_to_distinct_targets(self) -> None:
        plan = empty_plan(fertilizable_ongoing=[(4, 2), (2, 4)])
        private = {
            "inventories": [{"FERTILIZER": 1}, {"FERTILIZER": 1}],
            "shed": {},
        }

        actions = _logistics_actions(
            unit_positions=[(2, 2), (2, 2)],
            private=private,
            board_size=5,
            plan=plan,
            animal_fill_priority=["GOOSE"],
        )

        self.assertEqual(actions, [["EAST"], ["SOUTH"]])


class MarketOrderTests(unittest.TestCase):
    def test_sell_threshold_differs_by_product(self) -> None:
        farm = make_farm()
        private = {"shed": {"WHEAT": 5, "WOOL": 5}, "seeds": {}}
        # Above wheat's 0.65 threshold ($16.25) but below wool's 0.85 ($170).
        market = {"prices": {"WHEAT": 20, "WOOL": 100}}
        plan = empty_plan()

        orders = _market_orders(farm, private, market, day=5, hour=1, plan=plan)

        self.assertIn(["SELL", "WHEAT", 5], orders)
        self.assertFalse(any(o[0] == "SELL" and o[1] == "WOOL" for o in orders))

    def test_wheat_sale_reserves_the_animal_feed_buffer(self) -> None:
        farm = make_farm()
        private = {"shed": {"WHEAT": 10}, "seeds": {}}
        market = {"prices": {"WHEAT": 30}}  # well above the sell threshold
        plan = empty_plan(occupied_by_animal={"GOOSE": 2})

        orders = _market_orders(farm, private, market, day=5, hour=1, plan=plan)

        sell_orders = [o for o in orders if o[0] == "SELL" and o[1] == "WHEAT"]
        # 2 geese * FEED_DAYS_BUFFER(4) = 8 reserved; only 2 sells.
        self.assertEqual(sell_orders, [["SELL", "WHEAT", 2]])

    def test_animal_purchases_respect_the_cash_reserve(self) -> None:
        farm = make_farm()
        farm["money"] = float(ANIMAL_CASH_RESERVE)  # exactly reserve, no surplus
        private = {"shed": {}, "seeds": {}}
        market = {"prices": {"WHEAT": 25}}
        plan = empty_plan()

        orders = _market_orders(farm, private, market, day=0, hour=1, plan=plan)

        self.assertFalse(any(o[0] == "BUY_ANIMAL" for o in orders))

    def test_carried_goose_counts_toward_the_flock_cap(self) -> None:
        farm = make_farm()
        farm["money"] = 5000.0
        private = {"shed": {}, "seeds": {}, "inventories": [{"GOOSE": 1}]}
        market = {"prices": {"WHEAT": 25}}
        plan = empty_plan(occupied_by_animal={"GOOSE": 1})

        orders = _market_orders(farm, private, market, day=5, hour=1, plan=plan)

        self.assertFalse(any(o[0] == "BUY_ANIMAL" for o in orders))


class LogisticsTests(unittest.TestCase):
    def test_unit_carrying_an_animal_walks_to_and_places_on_its_structure(self) -> None:
        plan = empty_plan(empty_structures={"COOP": [(4, 4)]})
        private = {"inventories": [{"GOOSE": 1}], "shed": {}}

        actions = _logistics_actions(
            unit_positions=[(2, 2)],
            private=private,
            board_size=5,
            plan=plan,
            animal_fill_priority=["GOOSE"],
        )
        self.assertEqual(actions[0], _move_toward((2, 2), (4, 4)))

        actions_at_target = _logistics_actions(
            unit_positions=[(4, 4)],
            private=private,
            board_size=5,
            plan=plan,
            animal_fill_priority=["GOOSE"],
        )
        self.assertEqual(actions_at_target[0], ["PLACE", "GOOSE"])

    def test_unit_carrying_wheat_feeds_nearest_unfed_animal(self) -> None:
        plan = empty_plan(unfed_animals=[(4, 4)])
        private = {"inventories": [{"WHEAT": 3}], "shed": {}}

        actions = _logistics_actions(
            unit_positions=[(4, 4)],
            private=private,
            board_size=5,
            plan=plan,
            animal_fill_priority=["GOOSE"],
        )
        self.assertEqual(actions[0], ["FEED"])

    def test_each_goose_gets_a_distinct_parallel_wheat_pickup(self) -> None:
        # A single courier carrying both units delays the second feed by
        # several turns. Claim one target per hand so both geese receive
        # wheat on the same trip without duplicate pickups.
        plan = empty_plan(unfed_animals=[(4, 4), (4, 3)])
        private = {"inventories": [{}, {}], "shed": {"WHEAT": 10}}
        shed_adjacent = (2, 2)  # _shed_access_tiles(5) for a 5x5 board

        actions = _logistics_actions(
            unit_positions=[shed_adjacent, shed_adjacent],
            private=private,
            board_size=5,
            plan=plan,
            animal_fill_priority=["GOOSE"],
        )

        pickups = [a for a in actions if a is not None and a[0] == "PICKUP"]
        self.assertEqual(pickups, [["PICKUP", "WHEAT", 1], ["PICKUP", "WHEAT", 1]])

    def test_no_feed_source_falls_through_to_normal_task_pool(self) -> None:
        plan = empty_plan(unfed_animals=[(4, 4)])
        private = {"inventories": [{}], "shed": {}}  # no wheat anywhere

        actions = _logistics_actions(
            unit_positions=[(2, 2)],
            private=private,
            board_size=5,
            plan=plan,
            animal_fill_priority=["GOOSE"],
        )
        self.assertIsNone(actions[0])


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
        self.assertEqual(plan.unfed_animals, [])


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
        self.assertGreater(final[0].reward, 3000.0)  # beats starting money


if __name__ == "__main__":
    unittest.main()
