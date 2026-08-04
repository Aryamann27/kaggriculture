"""Unit and integration smoke tests for the submission agent."""

from __future__ import annotations

import unittest

from kaggle_environments import make

from main import (
    ANIMALS,
    FieldPlan,
    _field_tasks,
    _logistics_actions,
    _market_orders,
    _move_toward,
    _town_demand,
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
        crop_active={"WHEAT": 0, "CARROT": 0, "TOMATO": 0, "STRAWBERRY": 0, "MELON": 0},
        crop_maturing={"WHEAT": 0, "CARROT": 0, "TOMATO": 0, "STRAWBERRY": 0, "MELON": 0},
        crop_targets={"WHEAT": 5, "CARROT": 4, "TOMATO": 5, "STRAWBERRY": 0, "MELON": 3},
        animal_targets={"COW": 4, "SHEEP": 2, "GOOSE": 1},
        demand=_town_demand({}, day=0),
        occupied_by_animal={"COW": 0, "SHEEP": 0, "GOOSE": 0},
        structures_built={"PASTURE": 0, "COOP": 0},
        empty_structures={"PASTURE": [], "COOP": []},
        unfed_animals=[],
        fertilizable_wheat=[],
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
    def test_empty_field_plants_multiple_crops_and_builds_structures(self) -> None:
        farm = make_farm()
        private = {
            "seeds": {
                "WHEAT": 25,
                "CARROT": 25,
                "TOMATO": 25,
                "STRAWBERRY": 25,
                "MELON": 25,
            },
        }
        plan = _field_tasks(farm, private, day=0)

        planted_crops = {t.action[1] for t in plan.tasks if t.action[0] == "PLANT"}
        built_kinds = {t.action[0] for t in plan.tasks if t.action[0].startswith("BUILD_")}

        self.assertGreater(len(planted_crops), 1)
        self.assertEqual(built_kinds, {"BUILD_PASTURE", "BUILD_COOP"})
        # Crops get the majority land share.
        self.assertGreater(sum(plan.crop_targets.values()), plan.field_capacity * 0.5)

    def test_cow_and_sheep_share_the_pasture_target(self) -> None:
        farm = make_farm()
        plan = _field_tasks(farm, {"seeds": {}}, day=0)
        pasture_target = plan.animal_targets["COW"] + plan.animal_targets["SHEEP"]
        self.assertGreater(pasture_target, 0)
        self.assertEqual(ANIMALS["COW"].structure, "PASTURE")
        self.assertEqual(ANIMALS["SHEEP"].structure, "PASTURE")
        self.assertEqual(ANIMALS["GOOSE"].structure, "COOP")


class TownDemandTests(unittest.TestCase):
    def test_scores_only_already_unlocked_shops(self) -> None:
        demand = _town_demand(
            {"unlocked_shops": ["PET_CAFE", "UNKNOWN_FUTURE_SHOP"]},
            day=0,
        )

        # One town-center unit plus PET_CAFE's double order three times per
        # center interval. Unknown shops must not be treated as predictions.
        self.assertEqual(demand.score["CARROT"], 7)
        self.assertEqual(demand.score["STRAWBERRY"], 1)
        self.assertEqual(demand.shop_units["STRAWBERRY"], 0)

    def test_pet_cafe_shifts_crop_targets_to_carrots_without_strawberry_bet(self) -> None:
        farm = make_farm()
        private = {"seeds": {}}
        baseline = _field_tasks(farm, private, day=5, town={})
        pet_cafe = _field_tasks(
            farm,
            private,
            day=5,
            town={"unlocked_shops": ["PET_CAFE"]},
        )

        self.assertGreater(pet_cafe.crop_targets["CARROT"], baseline.crop_targets["CARROT"])
        self.assertEqual(pet_cafe.crop_targets["STRAWBERRY"], 0)
        self.assertGreaterEqual(pet_cafe.crop_targets["WHEAT"], 4)

    def test_strawberry_shop_enables_a_capped_strawberry_target(self) -> None:
        farm = make_farm()
        brunch = _field_tasks(
            farm,
            {"seeds": {}},
            day=5,
            town={"unlocked_shops": ["BRUNCH_SPOT"]},
        )

        self.assertGreater(brunch.crop_targets["STRAWBERRY"], 0)
        self.assertLessEqual(brunch.crop_targets["STRAWBERRY"], 2)

    def test_yarn_store_shifts_livestock_targets_to_sheep(self) -> None:
        farm = make_farm()
        baseline = _field_tasks(farm, {"seeds": {}}, day=5, town={})
        yarn_store = _field_tasks(
            farm,
            {"seeds": {}},
            day=5,
            town={"unlocked_shops": ["YARN_STORE"]},
        )

        self.assertGreater(yarn_store.animal_targets["SHEEP"], baseline.animal_targets["SHEEP"])
        self.assertGreater(yarn_store.animal_targets["SHEEP"], yarn_store.animal_targets["COW"])


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

    def test_sales_wait_for_the_turn_after_town_consumption(self) -> None:
        farm = make_farm()
        private = {"shed": {"WHEAT": 6}, "seeds": {}}
        market = {"prices": {"WHEAT": 25}}
        plan = empty_plan(demand=_town_demand({"unlocked_shops": ["BAKERY"]}, day=5))

        before_drain = _market_orders(farm, private, market, day=5, hour=0, plan=plan)
        after_drain = _market_orders(farm, private, market, day=5, hour=1, plan=plan)

        self.assertFalse(any(order[:2] == ["SELL", "WHEAT"] for order in before_drain))
        self.assertIn(["SELL", "WHEAT", 6], after_drain)

    def test_premium_sales_are_limited_to_observed_town_demand(self) -> None:
        farm = make_farm()
        private = {"shed": {"WOOL": 8}, "seeds": {}}
        market = {"prices": {"WOOL": 200}}
        plan = empty_plan(demand=_town_demand({"unlocked_shops": ["YARN_STORE"]}, day=5))

        orders = _market_orders(farm, private, market, day=5, hour=1, plan=plan)

        # YARN_STORE removes two wool and the center removes one before this
        # hour, so only three units replenish the observed drain.
        self.assertIn(["SELL", "WOOL", 3], orders)

    def test_wheat_sale_reserves_the_animal_feed_buffer(self) -> None:
        farm = make_farm()
        private = {"shed": {"WHEAT": 10}, "seeds": {}}
        market = {"prices": {"WHEAT": 30}}  # well above the sell threshold
        plan = empty_plan(occupied_by_animal={"COW": 2, "SHEEP": 0, "GOOSE": 0})

        orders = _market_orders(farm, private, market, day=5, hour=1, plan=plan)

        sell_orders = [o for o in orders if o[0] == "SELL" and o[1] == "WHEAT"]
        # 2 animals * FEED_DAYS_BUFFER(3) = 6 reserved; only the surplus (4) sells.
        self.assertEqual(sell_orders, [["SELL", "WHEAT", 4]])

    def test_animal_purchases_respect_the_cash_reserve(self) -> None:
        farm = make_farm()
        farm["money"] = 1200.0  # exactly the reserve floor, no surplus
        private = {"shed": {}, "seeds": {}}
        market = {"prices": {"WHEAT": 25}}
        plan = empty_plan()

        orders = _market_orders(farm, private, market, day=0, hour=1, plan=plan)

        self.assertFalse(any(o[0] == "BUY_ANIMAL" for o in orders))


class LogisticsTests(unittest.TestCase):
    def test_unit_carrying_an_animal_walks_to_and_places_on_its_structure(self) -> None:
        plan = empty_plan(empty_structures={"PASTURE": [(4, 4)], "COOP": []})
        private = {"inventories": [{"COW": 1}], "shed": {}}

        actions = _logistics_actions(
            unit_positions=[(2, 2)],
            private=private,
            board_size=5,
            plan=plan,
            animal_fill_priority=["COW", "SHEEP", "GOOSE"],
        )
        self.assertEqual(actions[0], _move_toward((2, 2), (4, 4)))

        actions_at_target = _logistics_actions(
            unit_positions=[(4, 4)],
            private=private,
            board_size=5,
            plan=plan,
            animal_fill_priority=["COW", "SHEEP", "GOOSE"],
        )
        self.assertEqual(actions_at_target[0], ["PLACE", "COW"])

    def test_unit_carrying_wheat_feeds_nearest_unfed_animal(self) -> None:
        plan = empty_plan(unfed_animals=[(4, 4)])
        private = {"inventories": [{"WHEAT": 3}], "shed": {}}

        actions = _logistics_actions(
            unit_positions=[(4, 4)],
            private=private,
            board_size=5,
            plan=plan,
            animal_fill_priority=["COW", "SHEEP", "GOOSE"],
        )
        self.assertEqual(actions[0], ["FEED"])

    def test_only_one_unit_picks_up_wheat_for_a_shared_feeding_need(self) -> None:
        # Regression test: previously every idle unit independently queued a
        # PICKUP for the same unfed animals, since the shared "still needs
        # feeding" list was never reduced after a pickup was issued.
        plan = empty_plan(unfed_animals=[(4, 4), (4, 3)])
        private = {"inventories": [{}, {}], "shed": {"WHEAT": 10}}
        shed_adjacent = (2, 2)  # _shed_access_tiles(5) for a 5x5 board

        actions = _logistics_actions(
            unit_positions=[shed_adjacent, shed_adjacent],
            private=private,
            board_size=5,
            plan=plan,
            animal_fill_priority=["COW", "SHEEP", "GOOSE"],
        )

        pickups = [a for a in actions if a is not None and a[0] == "PICKUP"]
        self.assertEqual(len(pickups), 1)
        self.assertEqual(pickups[0], ["PICKUP", "WHEAT", 2])

    def test_no_feed_source_falls_through_to_normal_task_pool(self) -> None:
        plan = empty_plan(unfed_animals=[(4, 4)])
        private = {"inventories": [{}], "shed": {}}  # no wheat anywhere

        actions = _logistics_actions(
            unit_positions=[(2, 2)],
            private=private,
            board_size=5,
            plan=plan,
            animal_fill_priority=["COW", "SHEEP", "GOOSE"],
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
