"""
Unit tests for the scoring rules and safety guards. Stdlib only — no RPC, no key, no
Solana dependency tree, so CI can run these on every push.

    python -m unittest discover -p 'test_*.py'
"""

import json
import os
import tempfile
import unittest

import points_core as core

HOUR = 3600
DAY = 24 * HOUR


def rows(*pairs):
    return [{"owner": o, "mint": "M", "supplied_usd": v} for o, v in pairs]


class TestAggregate(unittest.TestCase):
    def test_sums_across_reserves_and_obligations(self):
        # Same owner, two mints, two obligations: one balance.
        r = [
            {"owner": "A", "mint": "USDC", "supplied_usd": 100.0},
            {"owner": "A", "mint": "USDY", "supplied_usd": 50.0},
            {"owner": "A", "mint": "USDC", "supplied_usd": 25.0},
            {"owner": "B", "mint": "USDC", "supplied_usd": 10.0},
        ]
        self.assertEqual(core.aggregate_by_owner(r), {"A": 175.0, "B": 10.0})


class TestScoring(unittest.TestCase):
    def test_first_sighting_earns_nothing_but_is_backdated(self):
        state = core.update_points({}, {"A": 100.0}, now_ts=DAY, prev_run_ts=0,
                                   basis="min", backdate_fraction=0.5)
        self.assertEqual(state["A"]["cumulative_points"], 0.0)
        # prev_run_ts=0 means no known interval, so no backdate to apply.
        self.assertEqual(state["A"]["last_snapshot_ts"], DAY)

    def test_new_address_backdated_by_half_the_interval(self):
        state = {"A": {"cumulative_points": 0.0, "last_supplied_usd": 100.0, "last_snapshot_ts": DAY}}
        core.update_points(state, {"A": 100.0, "B": 500.0}, now_ts=2 * DAY, prev_run_ts=DAY,
                           basis="min", backdate_fraction=0.5)
        self.assertEqual(state["B"]["last_snapshot_ts"], 2 * DAY - 12 * HOUR)
        self.assertEqual(state["B"]["cumulative_points"], 0.0)

    def test_new_address_paid_for_backdated_period_next_cycle(self):
        state = {}
        core.update_points(state, {"B": 100.0}, now_ts=DAY, prev_run_ts=DAY - DAY,
                           basis="min", backdate_fraction=0.5)
        state = {"B": {"cumulative_points": 0.0, "last_supplied_usd": 100.0,
                       "last_snapshot_ts": 2 * DAY - 12 * HOUR}}
        core.update_points(state, {"B": 100.0}, now_ts=2 * DAY, prev_run_ts=DAY,
                           basis="min", backdate_fraction=0.5)
        # 12h of credit at 100 USD.
        self.assertAlmostEqual(state["B"]["cumulative_points"], 100.0 * 12)

    def test_holder_earns_balance_times_hours(self):
        state = {"A": {"cumulative_points": 0.0, "last_supplied_usd": 100.0, "last_snapshot_ts": 0}}
        core.update_points(state, {"A": 100.0}, now_ts=DAY, prev_run_ts=0, basis="min")
        self.assertAlmostEqual(state["A"]["cumulative_points"], 100.0 * 24)

    def test_min_basis_kills_the_snapshot_snipe(self):
        """Deposit before the snapshot, withdraw after: must be worth zero."""
        state = {}
        core.update_points(state, {"SNIPER": 1_000_000.0}, now_ts=DAY, prev_run_ts=0, basis="min")
        core.update_points(state, {}, now_ts=2 * DAY, prev_run_ts=DAY, basis="min")
        self.assertEqual(state["SNIPER"]["cumulative_points"], 0.0)

    def test_start_basis_still_pays_the_sniper(self):
        """The original behaviour, kept switchable — and this is why it is not the default."""
        state = {}
        core.update_points(state, {"SNIPER": 1_000_000.0}, now_ts=DAY, prev_run_ts=0, basis="start")
        core.update_points(state, {}, now_ts=2 * DAY, prev_run_ts=DAY, basis="start")
        self.assertAlmostEqual(state["SNIPER"]["cumulative_points"], 1_000_000.0 * 24)

    def test_min_basis_credits_the_smaller_of_the_two_endpoints(self):
        state = {"A": {"cumulative_points": 0.0, "last_supplied_usd": 1000.0, "last_snapshot_ts": 0}}
        core.update_points(state, {"A": 400.0}, now_ts=DAY, prev_run_ts=0, basis="min")
        self.assertAlmostEqual(state["A"]["cumulative_points"], 400.0 * 24)

    def test_topping_up_does_not_backdate_the_new_capital(self):
        state = {"A": {"cumulative_points": 0.0, "last_supplied_usd": 100.0, "last_snapshot_ts": 0}}
        core.update_points(state, {"A": 10_000.0}, now_ts=DAY, prev_run_ts=0, basis="min")
        self.assertAlmostEqual(state["A"]["cumulative_points"], 100.0 * 24)

    def test_points_never_go_negative_or_backwards(self):
        state = {"A": {"cumulative_points": 5.0, "last_supplied_usd": -3.0, "last_snapshot_ts": 0}}
        core.update_points(state, {"A": -9.0}, now_ts=DAY, prev_run_ts=0, basis="min")
        self.assertEqual(state["A"]["cumulative_points"], 5.0)

    def test_clock_skew_does_not_subtract_points(self):
        state = {"A": {"cumulative_points": 7.0, "last_supplied_usd": 100.0, "last_snapshot_ts": 2 * DAY}}
        core.update_points(state, {"A": 100.0}, now_ts=DAY, prev_run_ts=0, basis="min")
        self.assertEqual(state["A"]["cumulative_points"], 7.0)

    def test_missed_run_credits_the_whole_gap(self):
        """A skipped day must not cost an honest holder their points."""
        state = {"A": {"cumulative_points": 0.0, "last_supplied_usd": 100.0, "last_snapshot_ts": 0}}
        core.update_points(state, {"A": 100.0}, now_ts=3 * DAY, prev_run_ts=0, basis="min")
        self.assertAlmostEqual(state["A"]["cumulative_points"], 100.0 * 72)


class TestGuards(unittest.TestCase):
    def test_empty_read_is_refused(self):
        state = {"A": {"cumulative_points": 1.0, "last_supplied_usd": 100.0, "last_snapshot_ts": 0}}
        with self.assertRaises(RuntimeError):
            core.guard_supply_collapse(state, {}, force=False)

    def test_collapse_is_refused(self):
        state = {"A": {"cumulative_points": 1.0, "last_supplied_usd": 1000.0, "last_snapshot_ts": 0}}
        with self.assertRaises(RuntimeError):
            core.guard_supply_collapse(state, {"A": 1.0}, force=False, min_ratio=0.25)

    def test_collapse_can_be_forced(self):
        state = {"A": {"cumulative_points": 1.0, "last_supplied_usd": 1000.0, "last_snapshot_ts": 0}}
        core.guard_supply_collapse(state, {"A": 1.0}, force=True, min_ratio=0.25)

    def test_normal_movement_passes(self):
        state = {"A": {"cumulative_points": 1.0, "last_supplied_usd": 1000.0, "last_snapshot_ts": 0}}
        core.guard_supply_collapse(state, {"A": 900.0}, force=False, min_ratio=0.25)

    def test_first_ever_run_passes(self):
        core.guard_supply_collapse({}, {"A": 5.0}, force=False)

    def test_scope_change_is_refused(self):
        meta = {"market": "OLD", "tracked_mints": []}
        with self.assertRaises(RuntimeError):
            core.check_scope(meta, reset=False, market="NEW", mints=[])

    def test_scope_change_can_be_accepted(self):
        meta = {"market": "OLD", "tracked_mints": []}
        core.check_scope(meta, reset=True, market="NEW", mints=[])

    def test_tracked_mint_change_is_refused(self):
        meta = {"market": "M", "tracked_mints": ["USDC"]}
        with self.assertRaises(RuntimeError):
            core.check_scope(meta, reset=False, market="M", mints=[])

    def test_first_ever_run_has_no_scope_to_compare(self):
        core.check_scope({}, reset=False, market="M", mints=[])


class TestPrune(unittest.TestCase):
    def test_drops_departed_zero_earners_only(self):
        state = {
            "GONE_EMPTY": {"cumulative_points": 0.0, "last_supplied_usd": 0.0, "last_snapshot_ts": 0},
            "GONE_EARNED": {"cumulative_points": 42.0, "last_supplied_usd": 0.0, "last_snapshot_ts": 0},
            "HERE": {"cumulative_points": 0.0, "last_supplied_usd": 10.0, "last_snapshot_ts": 0},
        }
        self.assertEqual(core.prune_state(state, floor=1e-6), 1)
        self.assertEqual(set(state), {"GONE_EARNED", "HERE"})


class TestStateIO(unittest.TestCase):
    def test_roundtrip_and_leaderboard_order(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "points_state.csv")
            state = {
                "LOW": {"cumulative_points": 1.0, "last_supplied_usd": 5.0, "last_snapshot_ts": 10},
                "HIGH": {"cumulative_points": 99.0, "last_supplied_usd": 5.0, "last_snapshot_ts": 10},
            }
            core.save_state(p, state)
            with open(p) as f:
                body = f.read()
            self.assertLess(body.index("HIGH"), body.index("LOW"))
            self.assertEqual(core.load_state(p), state)

    def test_tiny_values_survive_the_roundtrip(self):
        """The live CSV already carries values like 7.6e-05; the frontend parses this too."""
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.csv")
            state = {"A": {"cumulative_points": 7.625333333333332e-05,
                           "last_supplied_usd": 0.000516, "last_snapshot_ts": 1789571943}}
            core.save_state(p, state)
            self.assertEqual(core.load_state(p), state)

    def test_missing_state_file_is_an_empty_leaderboard(self):
        self.assertEqual(core.load_state("/nonexistent/points_state.csv"), {})

    def test_meta_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "points_meta.json")
            core.save_meta(p, {"market": "M", "last_run_ts": 1})
            self.assertEqual(core.load_meta(p)["market"], "M")


class TestDescribeException(unittest.TestCase):
    """The real failure this guards: `FAILED: ` with nothing after the colon."""

    def test_exception_with_no_message_still_describes_itself(self):
        class SilentRpcError(Exception):
            def __str__(self):
                return ""

        out = core.describe_exception(SilentRpcError())
        self.assertIn("SilentRpcError", out)
        self.assertTrue(out.strip())

    def test_ordinary_message_is_kept(self):
        self.assertIn("boom", core.describe_exception(ValueError("boom")))

    def test_provider_error_attribute_is_surfaced(self):
        class RpcError(Exception):
            def __str__(self):
                return ""
            error_msg = "410 Gone: getProgramAccounts is disabled on this plan"

        out = core.describe_exception(RpcError())
        self.assertIn("getProgramAccounts is disabled", out)

    def test_cause_is_surfaced(self):
        try:
            try:
                raise ConnectionResetError("connection reset by peer")
            except ConnectionResetError as inner:
                raise RuntimeError("fetch failed") from inner
        except RuntimeError as e:
            out = core.describe_exception(e)
        self.assertIn("fetch failed", out)
        self.assertIn("connection reset by peer", out)

    def test_never_returns_empty(self):
        for e in (Exception(), ValueError(""), RuntimeError(None)):
            self.assertTrue(core.describe_exception(e).strip())


class TestIdlLayout(unittest.TestCase):
    """The memcmp offset is what keeps getProgramAccounts to one market. Pin it."""

    def setUp(self):
        if not os.path.exists(core.IDL_PATH):
            self.skipTest("klend_idl.json not present")
        with open(core.IDL_PATH) as f:
            self.idl = json.load(f)

    def test_lending_market_offset(self):
        # 8 discriminator + 8 (version/tag u64) + 16 (LastUpdate) = 32, for both accounts.
        self.assertEqual(core.idl_field_offset(self.idl, "Reserve", "lendingMarket"), 32)
        self.assertEqual(core.idl_field_offset(self.idl, "Obligation", "lendingMarket"), 32)

    def test_unknown_field_raises(self):
        with self.assertRaises(KeyError):
            core.idl_field_offset(self.idl, "Reserve", "nope")


if __name__ == "__main__":
    unittest.main()
