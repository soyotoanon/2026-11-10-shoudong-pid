"""Verify a settling wait precedes each optical acquisition."""

from dataclasses import replace
from pathlib import Path
import sys
from threading import Event
from time import monotonic
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scan import ScanConfig, ScanStopped, run_scan


CONFIG = ScanConfig(100, 100, 1, 4, 0.01, 40, 160)


class TracedEvent:
    def __init__(self, events, cancel_on_wait=False):
        self.events = events
        self.cancel_on_wait = cancel_on_wait
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, duration):
        self.events.append(("wait", duration))
        self.stopped = self.cancel_on_wait
        return self.stopped


class Rig:
    def __init__(self, baseline_power=4):
        self.events = []
        self.enabled = False
        self.amplitude = 0
        self.baseline_power = baseline_power
        self.last_change = monotonic()
        self.read_delays = []
        self.control_read_delays = []
        self.guard_reads = 0

    def prepare(self, frequency_hz):
        self.events.append(("prepare", frequency_hz))

    def set_frequency_hz(self, frequency_hz):
        self.events.append(("frequency", frequency_hz))
        self.last_change = monotonic()

    def set_amplitude_mvpp(self, amplitude):
        self.events.append(("amplitude", amplitude))
        self.amplitude = amplitude
        self.last_change = monotonic()
        return amplitude

    def output_on(self):
        self.events.append(("on",))
        self.enabled = True
        self.last_change = monotonic()

    def output_off(self):
        self.events.append(("off",))
        self.enabled = False
        self.last_change = monotonic()

    def read_mw(self):
        self.events.append(("read", self.amplitude if self.enabled else 0))
        self.read_delays.append(monotonic() - self.last_change)
        return self.amplitude / 10 if self.enabled else self.baseline_power

    def sample(self, record):
        self.events.append(("telemetry", record["stage"], record["batch"]))
        if record["stage"] == "guard":
            self.guard_reads += 1
        elif record["batch"] == 1:
            self.control_read_delays.append(self.read_delays[-2])


class TimingTests(unittest.TestCase):
    def test_default_is_three_seconds_and_invalid_waits_are_rejected(self):
        self.assertEqual(CONFIG.settle_s, 3.0)
        for duration in (-1, float("nan"), float("inf")):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                replace(CONFIG, settle_s=duration)
        self.assertEqual(replace(CONFIG, settle_s=0).settle_s, 0)

    def test_positive_output_waits_after_amplitude_and_rf_on_before_reads(self):
        rig = Rig()
        points = run_scan(CONFIG, rig, rig, TracedEvent(rig.events), lambda _: None, on_sample=rig.sample)
        self.assertEqual(points[0].status, "ok")
        waits = [index for index, event in enumerate(rig.events) if event[0] == "wait"]
        self.assertTrue(all(0 < rig.events[index][1] <= 0.25 for index in waits))
        self.assertGreaterEqual(sum(rig.events[index][1] for index in waits), 6)
        wait_index = waits[0]
        self.assertIn(("amplitude", 40), rig.events[:wait_index])
        self.assertIn(("on",), rig.events[:wait_index])
        first_control = rig.events.index(("telemetry", "sample", 1))
        self.assertEqual(sum(event[0:2] == ("telemetry", "guard") for event in rig.events[:first_control]), 30)
        self.assertGreater(rig.guard_reads, 0)
        self.assertFalse(rig.enabled)

    def test_zero_baseline_waits_while_rf_off_before_reads(self):
        config = replace(CONFIG, min_mvpp=0)
        rig = Rig()
        points = run_scan(config, rig, rig, TracedEvent(rig.events), lambda _: None, on_sample=rig.sample)
        self.assertEqual(points[0].amplitude_mvpp, 0)
        self.assertNotIn(("on",), rig.events)
        self.assertFalse(any(event[0] == "amplitude" for event in rig.events))
        first_control = rig.events.index(("telemetry", "sample", 1))
        self.assertEqual(sum(event[0:2] == ("telemetry", "guard") for event in rig.events[:first_control]), 30)
        self.assertGreater(rig.guard_reads, 0)
        self.assertFalse(rig.enabled)

    def test_each_frequency_and_bisection_trial_gets_its_own_wait(self):
        config = replace(CONFIG, stop_mhz=101, target_mw=5.25)
        rig = Rig()
        points = run_scan(config, rig, rig, TracedEvent(rig.events), lambda _: None, on_sample=rig.sample)
        self.assertEqual([point.status for point in points], ["ok", "ok"])
        settling = [event for event in rig.events if event == ("telemetry", "settling", 0)]
        self.assertEqual(len(settling), sum(point.trials for point in points))
        self.assertIn(("amplitude", 52.5), rig.events)
        pauses = [event for event in rig.events if event == ("wait", 0.25)]
        self.assertEqual(len(pauses), 2 * sum(point.trials for point in points))

    def test_cancellation_during_wait_allows_safety_check_but_prevents_control_reads(self):
        for minimum in (0, 40):
            with self.subTest(minimum=minimum):
                rig = Rig()
                stop = TracedEvent(rig.events, cancel_on_wait=True)
                with self.assertRaises(ScanStopped):
                    run_scan(replace(CONFIG, min_mvpp=minimum), rig, rig, stop, self.fail, on_sample=rig.sample)
                self.assertEqual(rig.guard_reads, 1)
                self.assertEqual(rig.control_read_delays, [])
                self.assertFalse(rig.enabled)
                self.assertEqual(rig.events[-1], ("off",))

    def test_real_clock_one_second_elapses_before_positive_and_zero_control_reads(self):
        for minimum in (0, 40):
            with self.subTest(minimum=minimum):
                rig = Rig()
                points = run_scan(replace(CONFIG, min_mvpp=minimum, settle_s=1), rig, rig, Event(), lambda _: None, on_sample=rig.sample)
                self.assertEqual(points[0].status, "ok")
                self.assertGreaterEqual(rig.control_read_delays[0], 1.0)
                self.assertLess(rig.read_delays[0], 0.2)
                self.assertFalse(rig.enabled)

    def test_real_clock_default_three_seconds_precedes_control_reads(self):
        rig = Rig()
        points = run_scan(replace(CONFIG, target_mw=3.8), rig, rig, Event(), lambda _: None, on_sample=rig.sample)
        self.assertEqual(points[0].status, "below_range")
        self.assertGreaterEqual(rig.control_read_delays[0], 3.0)
        self.assertLess(rig.read_delays[0], 0.2)
        self.assertFalse(rig.enabled)


if __name__ == "__main__":
    unittest.main()
