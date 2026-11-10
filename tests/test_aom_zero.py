"""Exercise zero as an RF-off baseline without connecting instruments."""

import sys
import unittest
from dataclasses import replace
from pathlib import Path
from threading import Event


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scan import ScanConfig, ScanStopped, run_scan


CONFIG = ScanConfig(100, 100, 1, 5, 0.05, 0, 100, settle_s=0)


class TestRig:
    def __init__(self, config, power):
        self.config = config
        self.power = power
        self.events = []
        self.enabled = False
        self.amplitude = None

    def prepare(self, frequency_hz):
        assert not self.enabled

    def set_frequency_hz(self, frequency_hz):
        assert not self.enabled

    def set_amplitude_mvpp(self, amplitude):
        assert 1 <= amplitude <= self.config.max_mvpp
        self.events.append(("amplitude", amplitude))
        self.amplitude = amplitude
        return amplitude

    def output_on(self):
        assert self.amplitude == 1
        self.events.append(("on",))
        self.enabled = True

    def output_off(self):
        self.events.append(("off",))
        self.enabled = False

    def read_mw(self):
        active_amplitude = self.amplitude if self.enabled else 0
        self.events.append(("read", active_amplitude))
        return self.power(active_amplitude)


class ZeroBaselineTests(unittest.TestCase):
    def run_case(self, config, power, stop=None, on_trial=None):
        rig = TestRig(config, power)
        points = run_scan(config, rig, rig, stop or Event(), lambda point: None, on_trial)
        self.assertFalse(rig.enabled)
        return points, rig.events

    def test_zero_matches_target_without_any_rf_enable_or_amplitude_write(self):
        points, events = self.run_case(CONFIG, lambda amplitude: 5)
        self.assertEqual(points[0].status, "ok")
        self.assertEqual(points[0].amplitude_mvpp, 0)
        self.assertIn(("read", 0), events)
        self.assertTrue(all(action[0] in {"off", "read"} for action in events))

    def test_each_frequency_reads_with_rf_off_before_first_positive_setting(self):
        config = replace(CONFIG, stop_mhz=101, target_mw=100, max_mvpp=1001)
        trials = []
        points, events = self.run_case(config, lambda amplitude: 0.01, on_trial=lambda *trial: trials.append(trial))
        self.assertEqual([point.status for point in points], ["unreachable"] * 2)
        for frequency in (100, 101):
            amplitudes = [amplitude for f, amplitude, power in trials if f == frequency]
            self.assertEqual(amplitudes[:2], [0, 1])
            self.assertEqual(amplitudes[-1], config.max_mvpp)
            self.assertTrue(all(0 < after - before <= 25.000000001 for before, after in zip(amplitudes, amplitudes[1:])))
        for index, event in enumerate(events):
            if event == ("on",):
                self.assertEqual(events[index - 1], ("amplitude", 1))
                self.assertEqual(events[index - 2], ("read", 0))

    def test_target_between_zero_and_first_positive_is_unreachable(self):
        points, events = self.run_case(replace(CONFIG, max_mvpp=1), lambda amplitude: 5.4 * amplitude)
        self.assertEqual(points[0].status, "unreachable")
        self.assertIsNone(points[0].amplitude_mvpp)
        self.assertEqual([event for event in events if event[0] == "amplitude"], [("amplitude", 1)])

    def test_max_below_generator_minimum_has_clear_validation(self):
        with self.assertRaisesRegex(ValueError, "至少为 1 mVpp"):
            replace(CONFIG, max_mvpp=0.9)

    def test_stop_during_baseline_never_enables_rf(self):
        stop = Event()

        def request_stop(amplitude):
            stop.set()
            return 0

        rig = TestRig(CONFIG, request_stop)
        with self.assertRaises(ScanStopped):
            run_scan(CONFIG, rig, rig, stop, self.fail)
        self.assertNotIn(("on",), rig.events)
        self.assertEqual(rig.events[-1], ("off",))


if __name__ == "__main__":
    unittest.main()
