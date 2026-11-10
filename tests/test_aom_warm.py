"""Verify warm-start continuity, local step limits, and optical shutdown."""

from collections import defaultdict
from pathlib import Path
import sys
from threading import Event
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_aom_refinement import ResponseRig
from scan import ScanConfig, run_scan


def writes_by_frequency(rig):
    result = defaultdict(list)
    for event in rig.events:
        if event[0] == "amplitude":
            result[event[1]].append(event[2])
    return result


class RoundedRig(ResponseRig):
    def set_amplitude_mvpp(self, amplitude):
        self.events.append(("amplitude", self.frequency, amplitude))
        actual = round(amplitude, 1)
        if self.amplitude != actual:
            self.setting_reads = 0
        self.amplitude = actual
        return actual


class WarmStartTests(unittest.TestCase):
    def test_first_frequency_initializes_then_warm_steps_track_both_directions(self):
        config = ScanConfig(100, 102, 1, 50, 2, 0, 300, settle_s=0)
        roots = {100e6: 200.4, 101e6: 201.2, 102e6: 194.5}
        rig = RoundedRig(lambda amplitude, frequency, read: max(0.00004, 50 + amplitude - roots[frequency]))
        points = run_scan(config, rig, rig, Event(), lambda point: None)
        self.assertEqual([point.status for point in points], ["ok"] * 3)
        writes = writes_by_frequency(rig)
        self.assertIn((100e6, 0), rig.read_counts)
        self.assertEqual(writes[100e6][0], 1)

        directions = []
        for prior, current in zip(points, points[1:]):
            frequency = current.frequency_mhz * 1e6
            values = writes[frequency]
            self.assertAlmostEqual(values[0], prior.amplitude_mvpp * 0.98)
            self.assertNotIn((frequency, 0), rig.read_counts)
            for before, after in zip(values, values[1:]):
                self.assertLessEqual(abs(after - before), 5.0000001)
            directions.append(current.amplitude_mvpp - values[0])
            self.assertAlmostEqual(current.amplitude_mvpp, roots[frequency], delta=0.250001)
        self.assertGreater(directions[0], 0)
        self.assertLess(directions[1], 0)
        self.assertFalse(rig.enabled)

    def test_unstable_frequency_clears_carry_before_next_frequency(self):
        config = ScanConfig(100, 102, 1, 50, 2, 0, 300, settle_s=0)

        def response(amplitude, frequency, read):
            if frequency == 101e6:
                return 47 + (0.5 if read % 2 else -0.5)
            return max(0.00004, 50 + amplitude - 200)

        rig = ResponseRig(response)
        points = run_scan(config, rig, rig, Event(), lambda point: None)
        self.assertEqual([point.status for point in points], ["ok", "unstable", "ok"])
        writes = writes_by_frequency(rig)
        self.assertAlmostEqual(writes[101e6][0], points[0].amplitude_mvpp * 0.98)
        self.assertNotIn((101e6, 0), rig.read_counts)
        self.assertIn((102e6, 0), rig.read_counts)
        self.assertEqual(writes[102e6][0], 1)
        self.assertFalse(rig.enabled)

    def test_warm_local_search_obeys_user_rf_amplitude_ceiling(self):
        config = ScanConfig(100, 101, 1, 50, 2, 0, 210, settle_s=0)
        roots = {100e6: 200, 101e6: 250}
        rig = ResponseRig(lambda amplitude, frequency, read: max(0.00004, 50 + amplitude - roots[frequency]))
        points = run_scan(config, rig, rig, Event(), lambda point: None)
        self.assertEqual([point.status for point in points], ["ok", "unreachable"])
        writes = writes_by_frequency(rig)[101e6]
        self.assertLessEqual(max(writes), 210)
        self.assertEqual(max(writes), 210)
        self.assertTrue(all(abs(after - before) <= 5.0000001 for before, after in zip(writes, writes[1:])))
        self.assertFalse(rig.enabled)

    def test_first_read_at_optical_ceiling_shuts_off_before_telemetry_and_next_frequency(self):
        config = ScanConfig(100, 102, 1, 50, 2, 0, 300, settle_s=0)

        def response(amplitude, frequency, read):
            return 55 if frequency == 101e6 else max(0.00004, 50 + amplitude - 200)

        rig = ResponseRig(response)
        overrange_telemetry = []

        def sample(record):
            if record.get("stage") == "guard" and record.get("mean_mw", 0) >= 55:
                self.assertFalse(rig.enabled)
                overrange_telemetry.append(record)

        points = run_scan(config, rig, rig, Event(), lambda point: None, on_sample=sample)
        self.assertEqual([point.status for point in points], ["ok", "overrange"])
        self.assertIsNone(points[-1].amplitude_mvpp)
        self.assertEqual(sum(count for (frequency, _), count in rig.read_counts.items() if frequency == 101e6), 1)
        self.assertTrue(overrange_telemetry)
        self.assertNotIn(("frequency", 102e6), rig.events)
        self.assertFalse(rig.enabled)


if __name__ == "__main__":
    unittest.main()
