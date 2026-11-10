"""Verify using a known RF amplitude as the first frequency's starting estimate."""

from dataclasses import replace
from pathlib import Path
import sys
from threading import Event
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_aom_refinement import ResponseRig
from test_aom_warm import writes_by_frequency
from scan import ScanConfig, run_scan


class InitialEstimateTests(unittest.TestCase):
    def run_case(self, config, response):
        rig = ResponseRig(response)
        points = run_scan(config, rig, rig, Event(), lambda point: None)
        self.assertFalse(rig.enabled)
        return points, rig

    def test_220_mvpp_estimate_is_used_first_without_a_zero_ramp(self):
        config = ScanConfig(100, 100, 1, 45, 2, 0, 300, settle_s=0, initial_mvpp=220)
        points, rig = self.run_case(config, lambda amplitude, frequency, read: amplitude * 45 / 220)
        point, = points
        self.assertEqual(point.status, "ok")
        self.assertEqual(point.amplitude_mvpp, 220)
        self.assertEqual(writes_by_frequency(rig)[100e6][0], 220)
        self.assertNotIn((100e6, 0), rig.read_counts)
        self.assertTrue(all(event[2] == 220 for event in rig.events if event[0] == "amplitude"))

    def test_initial_estimate_above_target_can_adjust_down(self):
        config = ScanConfig(100, 100, 1, 45, 2, 0, 300, settle_s=0, initial_mvpp=220)
        points, rig = self.run_case(config, lambda amplitude, frequency, read: 45 + amplitude - 218)
        point, = points
        self.assertEqual(point.status, "ok")
        self.assertLess(point.amplitude_mvpp, 220)
        self.assertAlmostEqual(point.amplitude_mvpp, 218, delta=0.225001)
        writes = writes_by_frequency(rig)[100e6]
        self.assertEqual(writes[0], 220)
        self.assertLess(min(writes), 220)
        self.assertTrue(all(abs(after - before) <= 5.0000001 for before, after in zip(writes, writes[1:])))

    def test_far_error_uses_larger_steps_and_fewer_trials_than_zero_start(self):
        config = ScanConfig(100, 100, 1, 45, 2, 0, 300, settle_s=0, initial_mvpp=220)
        response = lambda amplitude, frequency, read: max(0.00004, 45 + 0.2 * (amplitude - 250))
        estimated, rig = self.run_case(config, response)
        cold, _ = self.run_case(replace(config, initial_mvpp=None), response)
        self.assertEqual((estimated[0].status, cold[0].status), ("ok", "ok"))
        self.assertLessEqual(estimated[0].trials, 0.75 * cold[0].trials)
        writes = writes_by_frequency(rig)[100e6]
        steps = [abs(after - before) for before, after in zip(writes, writes[1:])]
        self.assertTrue(any(step > 1.000001 for step in steps), steps)
        self.assertLessEqual(max(steps), 5.0000001)
        self.assertLessEqual(abs(estimated[0].power_mw - 45) + estimated[0].std_mw, 0.225001)

    def test_estimate_and_subsequent_steps_respect_rf_ceiling(self):
        config = ScanConfig(100, 100, 1, 45, 2, 0, 225, settle_s=0, initial_mvpp=220)
        points, rig = self.run_case(config, lambda amplitude, frequency, read: max(0.00004, 45 + amplitude - 250))
        self.assertEqual(points[0].status, "unreachable")
        writes = writes_by_frequency(rig)[100e6]
        self.assertEqual(writes[0], 220)
        self.assertEqual(max(writes), 225)
        for initial in (-1, 226, float("nan"), float("inf")):
            with self.subTest(initial=initial), self.assertRaises(ValueError):
                replace(config, initial_mvpp=initial)

    def test_unsafe_power_at_initial_estimate_stops_before_any_adjustment(self):
        config = ScanConfig(100, 101, 1, 45, 2, 0, 300, settle_s=0, initial_mvpp=220)
        points, rig = self.run_case(config, lambda amplitude, frequency, read: 50)
        self.assertEqual([point.status for point in points], ["overrange"])
        self.assertIsNone(points[0].amplitude_mvpp)
        self.assertEqual(rig.read_counts[(100e6, 220)], 1)
        self.assertTrue(all(event[2] == 220 for event in rig.events if event[0] == "amplitude"))
        self.assertNotIn(("frequency", 101e6), rig.events)

    def test_zero_initial_estimate_preserves_default_cold_start(self):
        config = ScanConfig(100, 100, 1, 45, 2, 0, 300, settle_s=0)
        response = lambda amplitude, frequency, read: max(0.00004, 45 + amplitude - 220)
        default_points, default_rig = self.run_case(config, response)
        zero_points, zero_rig = self.run_case(replace(config, initial_mvpp=0), response)
        self.assertEqual(zero_points, default_points)
        self.assertEqual(writes_by_frequency(zero_rig), writes_by_frequency(default_rig))
        self.assertIn((100e6, 0), zero_rig.read_counts)

    def test_subminimum_positive_initial_estimate_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "至少"):
            ScanConfig(100, 100, 1, 45, 2, 0, 300, settle_s=0, initial_mvpp=0.5)

    def test_failure_restores_explicit_220_estimate_for_next_frequency(self):
        config = ScanConfig(100, 102, 1, 45, 2, 0, 300, settle_s=0, initial_mvpp=220)

        def response(amplitude, frequency, read):
            if frequency == 101e6:
                return 47 + (0.5 if read % 2 else -0.5)
            root = 220 if frequency == 100e6 else 218
            return max(0.00004, 45 + amplitude - root)

        points, rig = self.run_case(config, response)
        self.assertEqual([point.status for point in points], ["ok", "unstable", "ok"])
        writes = writes_by_frequency(rig)
        self.assertAlmostEqual(writes[101e6][0], points[0].amplitude_mvpp * 0.98)
        self.assertEqual(writes[102e6][0], 220)
        self.assertNotIn((102e6, 0), rig.read_counts)
        self.assertAlmostEqual(points[2].amplitude_mvpp, 218, delta=0.225001)


if __name__ == "__main__":
    unittest.main()
