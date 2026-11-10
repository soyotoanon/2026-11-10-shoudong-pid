"""Regressions for coarse-bin plateaus and settling transients."""

from collections import Counter
from pathlib import Path
import sys
from threading import Event
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scan import ScanConfig, run_scan


class ResponseRig:
    def __init__(self, response):
        self.response = response
        self.frequency = None
        self.amplitude = 0
        self.enabled = False
        self.setting_reads = 0
        self.events = []
        self.read_counts = Counter()

    def prepare(self, frequency_hz):
        assert not self.enabled
        self.events.append(("prepare", frequency_hz))

    def set_frequency_hz(self, frequency_hz):
        assert not self.enabled
        if self.frequency != frequency_hz:
            self.setting_reads = 0
        self.frequency = frequency_hz
        self.events.append(("frequency", frequency_hz))

    def set_amplitude_mvpp(self, amplitude):
        if self.amplitude != amplitude:
            self.setting_reads = 0
        self.amplitude = amplitude
        self.events.append(("amplitude", self.frequency, amplitude))
        return amplitude

    def output_on(self):
        if not self.enabled:
            self.setting_reads = 0
        self.enabled = True
        self.events.append(("on",))

    def output_off(self):
        if self.enabled:
            self.setting_reads = 0
        self.enabled = False
        self.events.append(("off",))

    def read_mw(self):
        amplitude = self.amplitude if self.enabled else 0
        self.setting_reads += 1
        self.read_counts[(self.frequency, amplitude)] += 1
        value = self.response(amplitude, self.frequency, self.setting_reads)
        self.events.append(("read", self.frequency, amplitude, value))
        return value


class RefinementTests(unittest.TestCase):
    def run_case(self, config, response):
        rig = ResponseRig(response)
        published = []
        points = run_scan(config, rig, rig, Event(), published.append)
        self.assertEqual(points, published)
        self.assertFalse(rig.enabled)
        self.assertEqual(rig.events[-1], ("off",))
        return points, rig

    def test_decaying_rf_off_residual_is_never_an_rf_on_rollover_peak(self):
        config = ScanConfig(100, 101, 1, 5, 0.05, 0, 120, settle_s=0)

        def response(amplitude, frequency, read):
            if amplitude == 0:
                return 0.0015 if read <= 2 else 0.00004
            return max(0.000045, amplitude / 20 if amplitude > 1 else 0.000045)

        points, rig = self.run_case(config, response)
        self.assertEqual([point.status for point in points], ["ok", "ok"])
        for point in points:
            self.assertAlmostEqual(point.amplitude_mvpp, 100, delta=0.3)
        self.assertTrue(any(event[0] == "amplitude" and event[2] > 1 for event in rig.events))

    def test_loose_user_tolerance_does_not_quantize_frequency_curve_to_one_coarse_bin(self):
        config = ScanConfig(100, 102, 1, 50, 2, 50, 290, settle_s=0)
        roots = {100e6: 208.5, 101e6: 210, 102e6: 211.5}
        points, _ = self.run_case(
            config, lambda amplitude, frequency, read: max(0.00004, 50 + amplitude - roots[frequency])
        )
        self.assertEqual([point.status for point in points], ["ok"] * 3)
        amplitudes = [point.amplitude_mvpp for point in points]
        self.assertTrue(all(first < second for first, second in zip(amplitudes, amplitudes[1:])), amplitudes)
        for point in points:
            self.assertLessEqual(abs(point.amplitude_mvpp - roots[point.frequency_mhz * 1e6]), 0.250001)
            self.assertLessEqual(abs(point.power_mw - 50) + point.std_mw, 0.250001)

    def test_quiet_initial_stale_target_must_pass_an_independent_confirmation(self):
        config = ScanConfig(100, 100, 1, 5, 0.05, 40, 160, settle_s=0)
        points, rig = self.run_case(config, lambda amplitude, frequency, read: 5 if read <= 7 else 5.3)
        self.assertEqual(points[0].status, "unstable")
        self.assertIsNone(points[0].amplitude_mvpp)
        self.assertGreater(rig.read_counts[(100e6, 40)], 6)
        self.assertEqual(max(event[2] for event in rig.events if event[0] == "amplitude"), 40)

    def test_quiet_initial_stale_drop_is_confirmed_before_rollover_classification(self):
        config = ScanConfig(100, 100, 1, 50, 0.05, 40, 160, settle_s=0)

        def response(amplitude, frequency, read):
            if amplitude == 40:
                return 5
            return 3 if read <= 7 else 6

        points, rig = self.run_case(config, response)
        self.assertEqual(points[0].status, "unstable")
        self.assertIsNone(points[0].amplitude_mvpp)
        writes = [event[2] for event in rig.events if event[0] == "amplitude"]
        self.assertGreater(rig.read_counts[(100e6, max(writes))], 6)
        self.assertEqual(len(set(writes)), 2)

    def test_never_settling_readings_stop_without_trying_a_higher_amplitude(self):
        config = ScanConfig(100, 100, 1, 5, 0.05, 40, 160, settle_s=0)
        points, rig = self.run_case(config, lambda amplitude, frequency, read: 1 + read * 0.01)
        self.assertEqual(points[0].status, "unstable")
        self.assertIsNone(points[0].amplitude_mvpp)
        self.assertGreaterEqual(rig.read_counts[(100e6, 40)], 6)
        self.assertLessEqual(rig.read_counts[(100e6, 40)], 70)
        self.assertEqual(set(event[2] for event in rig.events if event[0] == "amplitude"), {40})

    def test_confirmed_optical_rollover_never_applies_a_larger_amplitude(self):
        config = ScanConfig(100, 100, 1, 50, 0.05, 40, 160, settle_s=0)
        points, rig = self.run_case(
            config, lambda amplitude, frequency, read: max(0.01, 10 - abs(amplitude - 70) * 0.2)
        )
        point, = points
        self.assertEqual(point.status, "rollover")
        self.assertEqual(point.best_attempt_mvpp, 70)
        self.assertEqual(point.rollover_amplitude_mvpp, 75)
        self.assertEqual(max(event[2] for event in rig.events if event[0] == "amplitude"), 75)
        self.assertGreater(rig.read_counts[(100e6, 75)], 6)

    def test_sample_telemetry_exposes_settling_windows_and_independent_confirmation(self):
        config = ScanConfig(100, 100, 1, 5, 0.05, 40, 160, settle_s=0)
        rig = ResponseRig(lambda amplitude, frequency, read: 5)
        samples = []
        points = run_scan(config, rig, rig, Event(), lambda point: None, on_sample=samples.append)
        self.assertEqual(points[0].status, "ok")
        settling = [sample for sample in samples if sample["stage"] == "settling"]
        self.assertEqual(len(settling), points[0].trials)
        self.assertEqual(samples[0]["batch"], 0)
        self.assertIsNone(samples[0]["mean_mw"])
        self.assertTrue(any(sample["phase"] == "confirm" for sample in samples))
        final = samples[-1]
        self.assertEqual(final["stage"], "confirm")
        self.assertTrue(final["stable"])
        self.assertEqual(final["batch"], 3)
        self.assertEqual(final["mean_mw"], 5)
        self.assertEqual(final["std_mw"], 0)
        self.assertEqual(final["frequency_mhz"], 100)
        self.assertEqual(final["amplitude_mvpp"], 40)
        self.assertGreaterEqual(final["elapsed_s"], 0)


if __name__ == "__main__":
    unittest.main()
