"""Offline regression tests for ENGIE's per-meter historical backfill.

All dates, accounts and values in these tests are synthetic. No HA runtime,
network or personal ENGIE account is needed to run them.
"""

from __future__ import annotations

from datetime import date, timedelta
import importlib.util
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "engie"


def _module():
    for name, directory in (
        ("_history_case", ROOT),
        ("_history_case.vendor", ROOT / "vendor"),
        ("_history_case.vendor.engie_particuliers", ROOT / "vendor" / "engie_particuliers"),
    ):
        if name not in sys.modules:
            package = types.ModuleType(name)
            package.__path__ = [str(directory)]
            sys.modules[name] = package
    name = "_history_case.meter_history_core"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, ROOT / "meter_history_core.py"
        )
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        assert spec.loader is not None
        spec.loader.exec_module(m)
    return sys.modules[name]


class MeterHistoryPlanningTests(unittest.TestCase):
    def test_default_period_uses_completed_local_days(self):
        m = _module()
        today = date(2026, 11, 4)
        self.assertEqual(
            m.plan_period("90_days", today=today),
            (date(2026, 8, 6), today)
        )
        self.assertEqual(
            m.plan_period("365_days", today=today),
            (today - timedelta(days=365), today)
        )
        a, b = m.plan_period("all_available", today=today)
        self.assertEqual(b, today)
        self.assertEqual((b - a).days, m.HISTORY_MAX_DAYS)

    def test_custom_dates_reject_future_and_unbounded_history(self):
        m = _module()
        today = date(2026, 11, 4)
        self.assertEqual(
            m.plan_period(
                "custom", start_date="2026-10-01",
                end_date="2026-10-31", today=today
            ),
            (date(2026, 10, 1), date(2026, 11, 1)),
        )
        for start, end in (
            ("2026-11-03", "2026-11-04"),
            ("2026-10-09", "2026-10-08"),
            ("2001-01-01", "2026-11-03"),
            ("2026/10/01", "2026-10-31"),
        ):
            with self.subTest(start=start):
                with self.assertRaises(ValueError):
                    m.plan_period(
                        "custom", start_date=start, end_date=end, today=today
                    )

    def test_windows_have_no_gaps_or_overlaps(self):
        m = _module()
        first, last = date(2025, 1, 1), date(2026, 11, 4)
        ranges = m.windows_reverse(first, last)
        self.assertEqual(ranges[0][1], last)
        self.assertEqual(ranges[-1][0], first)
        for i, (start, end) in enumerate(ranges):
            self.assertGreater((end-start).days, 0)
            self.assertLessEqual((end-start).days, 90)
            if i:
                self.assertEqual(end, ranges[i-1][0])
        self.assertEqual(sum((b-a).days for a,b in ranges), (last-first).days)

    def test_paris_calendar_handles_both_dst_changes(self):
        m = _module()
        hours = lambda a,b: (m.local_midnight_ms(b)-m.local_midnight_ms(a))/3600000
        self.assertEqual(
            hours(date(2026,10,25), date(2026,10,26)), 25
        )
        self.assertEqual(
            hours(date(2026,3,29), date(2026,3,30)), 23
        )


class MeterHistoryImportTests(unittest.TestCase):
    def _point(self, day: str, kwh: float, cost: float | None, *, partial: bool = False):
        from _history_case.vendor.engie_particuliers.models import ConsumptionPoint
        return ConsumptionPoint(
            energy="GAZ", granularity="day",
            start=day + "T00:00:00+02:00", kwh=kwh,
            cost_ttc=cost, partial=partial
        )

    def test_skips_incomplete_days_outside_range_and_invalid_values(self):
        m = _module()
        points = [
            self._point("2026-10-01", 11.0, 1.25),
            self._point("2026-10-02", 15.0, 1.85, partial=True),
            self._point("2026-09-30", 30.0, 3.33),
            self._point("2026-10-03", float("nan"), 2),
            self._point("2026-10-04", -2, 2),
        ]
        rows = m.normalize_daily_points(
            points, first=date(2026,10,1), exclusive_end=date(2026,11,1)
        )
        self.assertEqual(rows, {
            "2026-10-01": {"kwh":11.0, "cost_ttc":1.25}
        })

    def test_import_twice_is_idempotent_and_correction_shifts_later_sums(self):
        m = _module()
        initial = {
            "2026-10-01":{"kwh":10.0,"cost_ttc":1.2},
            "2026-10-02":{"kwh":20.0,"cost_ttc":2.4},
            "2026-10-03":{"kwh":30.0,"cost_ttc":3.6},
        }
        same, changes = m.reconcile_daily(initial, initial)
        self.assertEqual(changes["new_days"], 0)
        self.assertEqual(changes["corrected_days"], 0)
        self.assertEqual(changes["unchanged_days"], 3)
        self.assertEqual(same, initial)

        corrected, changes = m.reconcile_daily(
            initial,
            {"2026-10-01": {"kwh":15.0, "cost_ttc":None}}
        )
        self.assertEqual(changes["corrected_days"], 1)
        self.assertEqual(corrected["2026-10-01"]["cost_ttc"],1.2)
        energy=m.cumulative_statistics(corrected,stream="energy")
        self.assertEqual([x["sum"] for x in energy],[15.0,35.0,65.0])
        self.assertEqual([x["state"] for x in energy],[15.0,20.0,30.0])
        costs=m.cumulative_statistics(corrected,stream="cost")
        self.assertEqual([x["sum"] for x in costs],[1.2,3.6,7.2])

    def test_missing_cost_does_not_create_fictitious_zero_cost(self):
        m=_module()
        merged, changes=m.reconcile_daily({},{
            "2026-10-01":{"kwh":10.0,"cost_ttc":None},
            "2026-10-02":{"kwh":4.0,"cost_ttc":0.5}
        })
        self.assertEqual(changes["total_days"],2)
        self.assertEqual(len(m.cumulative_statistics(merged,stream="energy")),2)
        cost=m.cumulative_statistics(merged,stream="cost")
        self.assertEqual(len(cost),1)
        self.assertEqual(cost[0]["sum"],0.5)

    def test_source_statistic_ids_cannot_reference_other_integrations(self):
        implementation=(ROOT / "meter_history.py").read_text(encoding="utf-8")
        self.assertIn('"_SAFE_ID = re.compile(', implementation.replace("_SAFE_ID = re.compile", '"_SAFE_ID = re.compile(') if False else implementation)
        self.assertIn('r"^meter_[0-9a-f]{20}$"',implementation)
        self.assertIn('statistics_scope": "engie:meter_* only"',implementation)
        self.assertNotIn("sensor.lixee", implementation)
        self.assertNotIn("sensor.compteur_de_gaz_volume",implementation)

    def test_native_controls_are_independent_of_physical_utility_meters(self):
        button=(ROOT/"button.py").read_text(encoding="utf-8")
        select=(ROOT/"select.py").read_text(encoding="utf-8")
        self.assertIn("class EngieHistoryButton",button)
        self.assertIn("class EngieHistoryPeriodSelect",select)
        self.assertIn('"90_days"',select)
        self.assertIn('"365_days"',select)
        self.assertIn('"all_available"',select)


if __name__ == "__main__":
    unittest.main()
