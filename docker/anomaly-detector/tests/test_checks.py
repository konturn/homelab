"""Check types against synthetic series: seasonality, spikes, stale, drift."""
import math
import random
import unittest

from detector import checks
from detector.config import CHECK_DEFAULTS, duration

WEEK = 7 * 86400


def params(kind, **kw):
    p = dict(CHECK_DEFAULTS[kind])
    p.update(kw)
    for k in ("window", "every", "lookback", "interval"):
        if k in p:
            p[k] = duration(p[k])
    return p


def weekly_pattern(hour_of_week, rng):
    """Daily cycle (higher in the evening), busier weekends, plus noise."""
    hour = hour_of_week % 24
    day = hour_of_week // 24
    base = 40 + 15 * math.sin((hour - 6) / 24 * 2 * math.pi)
    if day >= 5:
        base += 10
    return base + rng.gauss(0, 1.5)


class SeasonalTest(unittest.TestCase):
    def setUp(self):
        self.rng = random.Random(42)
        self.p = params("seasonal")

    def history(self, how, weeks=4):
        return [weekly_pattern(how, self.rng) for _ in range(weeks)]

    def test_normal_hours_not_flagged_across_a_week(self):
        flagged = 0
        for how in range(168):
            r = checks.seasonal(weekly_pattern(how, self.rng), self.history(how), self.p)
            flagged += r.anomalous
        self.assertEqual(flagged, 0)

    def test_value_normal_overall_but_wrong_for_the_hour(self):
        # 3am weekday is ~27; 55 is an ordinary evening value but wrong at 3am.
        how = 3
        r = checks.seasonal(55.0, self.history(how), self.p)
        self.assertEqual(r.severity, "severe")
        self.assertGreater(r.score, 10)
        self.assertAlmostEqual(r.baseline, weekly_pattern(how, random.Random(0)), delta=5)

    def test_spike_grades_moderate_then_severe(self):
        hist = [50.0, 51.0, 49.0, 50.5]
        scale = max(1.4826 * 0.5, 0.05 * 50.25)
        moderate = checks.seasonal(50.25 + 7 * scale, hist, self.p)
        severe = checks.seasonal(50.25 + 12 * scale, hist, self.p)
        self.assertEqual(moderate.severity, "moderate")
        self.assertEqual(severe.severity, "severe")

    def test_min_history_guard(self):
        r = checks.seasonal(500.0, [50.0, None, None, 51.0], self.p)
        self.assertEqual(r.status, "insufficient_history")
        self.assertFalse(r.anomalous)

    def test_direction_up_ignores_drops(self):
        p = dict(self.p, direction="up")
        self.assertFalse(checks.seasonal(0.0, [50.0, 51.0, 49.0, 50.0], p).anomalous)
        self.assertTrue(checks.seasonal(500.0, [50.0, 51.0, 49.0, 50.0], p).anomalous)

    def test_flat_history_uses_scale_floor(self):
        # MAD is 0; rel_scale 5% of 100 = 5, so +10 is z=2 (fine), +60 is z=12.
        self.assertFalse(checks.seasonal(110.0, [100.0] * 4, self.p).anomalous)
        self.assertEqual(checks.seasonal(160.0, [100.0] * 4, self.p).severity, "severe")

    def test_flat_zero_history_no_floor_is_capped(self):
        p = dict(self.p, rel_scale=0.0)
        r = checks.seasonal(1.0, [0.0] * 4, p)
        self.assertEqual(r.score, checks.Z_CAP)

    def test_min_abs_delta(self):
        p = dict(self.p, min_abs_delta=5)
        self.assertFalse(checks.seasonal(1.0, [0.1, 0.1, 0.1, 0.1], p).anomalous)

    def test_no_data(self):
        self.assertEqual(checks.seasonal(None, [1.0] * 4, self.p).status, "no_data")


class RateTest(unittest.TestCase):
    def test_spike_in_rate(self):
        rng = random.Random(1)
        vals = [20 + 0.1 * i + rng.gauss(0, 0.2) for i in range(24)]
        p = params("rate", min_points=12)
        self.assertFalse(checks.rate(vals, p).anomalous)
        vals.append(vals[-1] - 8)
        r = checks.rate(vals, p)
        self.assertTrue(r.anomalous)
        self.assertLess(r.value, -7)

    def test_max_change_absolute(self):
        vals = [68.0] * 20 + [63.5]
        p = params("rate", min_points=12, direction="down", z_moderate=1e9, z_severe=1e9,
                   max_change={"moderate": 4, "severe": 7})
        self.assertEqual(checks.rate(vals, p).severity, "moderate")
        self.assertEqual(checks.rate(vals[:-1] + [60.0], p).severity, "severe")
        self.assertFalse(checks.rate(vals[:-1] + [72.0], p).anomalous)

    def test_too_short(self):
        self.assertEqual(checks.rate([1.0], params("rate")).status, "no_data")
        self.assertEqual(checks.rate([1.0, 2.0, 3.0], params("rate")).status, "insufficient_history")


class StaleTest(unittest.TestCase):
    p = {"lookback": 7 * 86400, "max_age": {"moderate": 600, "severe": 1800}}

    def test_fresh(self):
        self.assertEqual(checks.stale(30, self.p).severity, "ok")

    def test_moderate_then_severe(self):
        self.assertEqual(checks.stale(900, self.p).severity, "moderate")
        self.assertEqual(checks.stale(3600, self.p).severity, "severe")

    def test_nothing_in_lookback(self):
        r = checks.stale(None, self.p)
        self.assertEqual(r.severity, "severe")
        self.assertIn("no data", r.detail)


class DriftTest(unittest.TestCase):
    def daily(self, slope, n=14, noise=0.0, base=50.0, seed=3):
        rng = random.Random(seed)
        return [(i * 86400, base + slope * i + rng.gauss(0, noise)) for i in range(n)]

    def test_creeping_counter(self):
        p = {"min_points": 7, "slope": {"moderate": 0.05, "severe": 5}, "min_total_change": 1}
        self.assertEqual(checks.drift(self.daily(0.0, base=8), p).severity, "ok")
        r = checks.drift(self.daily(0.3, base=8), p)
        self.assertEqual(r.severity, "moderate")
        self.assertAlmostEqual(r.value, 0.3, places=6)
        self.assertEqual(checks.drift(self.daily(10, base=8), p).severity, "severe")

    def test_noisy_flat_usage_is_ok(self):
        p = {"min_points": 7, "slope": {"moderate": 0.75, "severe": 3}}
        self.assertFalse(checks.drift(self.daily(0.0, noise=0.5), p).anomalous)
        self.assertTrue(checks.drift(self.daily(1.0, noise=0.5), p).anomalous)

    def test_down_is_ignored_by_default(self):
        p = {"min_points": 7, "slope": {"moderate": 0.5}}
        self.assertFalse(checks.drift(self.daily(-2.0), p).anomalous)

    def test_min_points(self):
        p = {"min_points": 7, "slope": {"moderate": 0.5}}
        self.assertEqual(checks.drift(self.daily(5, n=3), p).status, "insufficient_history")


class ThresholdTest(unittest.TestCase):
    def test_above_below(self):
        p = {"above": {"moderate": 82, "severe": 88}, "below": {"moderate": 60, "severe": 55}}
        self.assertEqual(checks.threshold(70, p).severity, "ok")
        self.assertEqual(checks.threshold(85, p).severity, "moderate")
        self.assertEqual(checks.threshold(90, p).severity, "severe")
        self.assertEqual(checks.threshold(58, p).severity, "moderate")
        self.assertEqual(checks.threshold(50, p).severity, "severe")
        self.assertEqual(checks.threshold(None, p).status, "no_data")

    def test_status_code_band(self):
        p = {"above": {"severe": 2.5}, "below": {"severe": 1.5}}
        self.assertFalse(checks.threshold(2, p).anomalous)
        self.assertEqual(checks.threshold(3, p).severity, "severe")


class HelpersTest(unittest.TestCase):
    def test_cap_and_worst(self):
        self.assertEqual(checks.cap("severe", "moderate"), "moderate")
        self.assertEqual(checks.cap("ok", "moderate"), "ok")
        self.assertEqual(checks.worst(["ok", "moderate", "ok"]), "moderate")
        self.assertEqual(checks.worst([]), "ok")


if __name__ == "__main__":
    unittest.main()
