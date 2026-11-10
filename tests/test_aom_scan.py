"""Offline regression tests for the AOM scan and RF state transitions."""

import sys
import unittest
from dataclasses import replace
from itertools import cycle
from pathlib import Path
from threading import Event


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scan import RFOffError, ScanConfig, ScanStopped, run_scan


CONFIG = ScanConfig(100, 100, 0.25, 5, 0.005, 40, 160, settle_s=0)


class Generator:
    def __init__(self, config):
        self.config = config
        self.events = []
        self.enabled = False
        self.amplitude = None
        self.frequency = None
        self.fail_off = False

    def prepare(self, frequency_hz):
        assert not self.enabled
        self.events.append(("prepare", frequency_hz))

    def set_frequency_hz(self, frequency_hz):
        assert not self.enabled
        self.events.append(("frequency", frequency_hz))
        self.frequency = frequency_hz

    def set_amplitude_mvpp(self, mvpp):
        assert self.config.min_mvpp <= mvpp <= self.config.max_mvpp
        self.events.append(("amplitude", mvpp))
        self.amplitude = mvpp

    def output_on(self):
        assert self.config.min_mvpp <= self.amplitude <= self.config.max_mvpp
        self.events.append(("on",))
        self.enabled = True

    def output_off(self):
        self.events.append(("off",))
        if self.fail_off:
            raise OSError("fake RF off timeout")
        self.enabled = False


class Meter:
    def __init__(self, generator, response):
        self.generator = generator
        self.response = response

    def read_mw(self):
        assert self.generator.enabled
        return self.response(self.generator.amplitude, self.generator.frequency)


class ScanTests(unittest.TestCase):
    def scan(self, config, response):
        generator = Generator(config)
        published, trials = [], []

        def record(point):
            self.assertFalse(generator.enabled)
            published.append(point)

        points = run_scan(
            config, generator, Meter(generator, response), Event(), record,
            lambda *trial: trials.append(trial),
        )
        self.assertEqual(points, published)
        interrupted_trials = sum(point.status == "overrange" for point in points)
        self.assertEqual(sum(point.trials for point in points), len(trials) + interrupted_trials)
        self.assertEqual(generator.events[0], ("off",))
        self.assertEqual(generator.events[-1], ("off",))
        return points, generator

    def test_frequency_grid_includes_endpoint_in_both_directions(self):
        self.assertEqual(replace(CONFIG, stop_mhz=100.6).frequencies_mhz(), [100, 100.25, 100.5, 100.6])
        self.assertEqual(replace(CONFIG, start_mhz=100.6).frequencies_mhz(), [100.6, 100.35, 100.1, 100])
        self.assertEqual(replace(CONFIG, start_mhz=0.1, stop_mhz=0.4, step_mhz=0.1).frequencies_mhz(), [0.1, 0.2, 0.30000000000000004, 0.4])

    def test_invalid_configuration_rejected(self):
        for overrides in (
            {"start_mhz": float("nan")}, {"step_mhz": 0}, {"max_mvpp": 20},
            {"tolerance_mw": 5},
            {"start_mhz": 100, "stop_mhz": 110, "step_mhz": 0.00001},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                replace(CONFIG, **overrides).frequencies_mhz()

    def test_points_follow_frequency_schedule_and_output_cycles(self):
        config = replace(CONFIG, stop_mhz=100.5)
        points, generator = self.scan(config, lambda amp, hz: amp / 20 + (hz - 100e6) / 1e6)
        self.assertEqual([point.frequency_mhz for point in points], [100, 100.25, 100.5])
        for point, expected in zip(points, (100, 95, 90)):
            self.assertAlmostEqual(point.amplitude_mvpp, expected, delta=0.026)
        self.assertEqual([point.status for point in points], ["ok"] * 3)
        self.assertEqual(generator.events.count(("on",)), 3)
        self.assertEqual(generator.events.count(("off",)), 5)

    def test_first_rising_crossing_selected_even_if_upper_limit_falls_below_target(self):
        points, _ = self.scan(CONFIG, lambda amp, hz: 8 - ((amp - 100) / 30) ** 2)
        point = points[0]
        self.assertEqual(point.status, "ok")
        self.assertLess(point.amplitude_mvpp, 100)
        self.assertLessEqual(abs(point.power_mw - 5), CONFIG.tolerance_mw)

    def test_unreachable_and_below_range_keep_attempt_separate(self):
        for response, status, best in (
            (lambda amp, hz: amp / 50, "unreachable", 160),
            (lambda amp, hz: amp / 1000 + 5.1, "below_range", 40),
        ):
            with self.subTest(status=status):
                points, generator = self.scan(CONFIG, response)
                self.assertEqual(points[0].status, status)
                self.assertEqual(points[0].best_attempt_mvpp, best)
                self.assertIsNone(points[0].amplitude_mvpp)
                if status == "below_range":
                    self.assertEqual(points[0].trials, 1)
                    self.assertEqual(
                        [event for event in generator.events if event[0] == "amplitude"],
                        [("amplitude", 40), ("amplitude", 40)],
                    )

    def test_matching_measurement_requires_an_independent_confirmation(self):
        points, _ = self.scan(CONFIG, lambda amp, hz: 5)
        self.assertEqual(points[0].status, "ok")
        self.assertEqual(points[0].amplitude_mvpp, CONFIG.min_mvpp)
        self.assertEqual(points[0].trials, 2)

    def test_rollover_stops_writes_at_that_frequency_and_restarts_next_at_minimum(self):
        config = replace(CONFIG, target_mw=50, stop_mhz=101, step_mhz=1)

        def response(amplitude, frequency_hz):
            peak = 70 if frequency_hz == 100e6 else 90
            return max(0.1, 10 - abs(amplitude - peak) * 0.2)

        points, generator = self.scan(config, response)
        self.assertEqual([point.status for point in points], ["rollover", "rollover"])
        for point, peak in zip(points, (70, 90)):
            self.assertIsNone(point.amplitude_mvpp)
            self.assertEqual(point.best_attempt_mvpp, peak)
            self.assertEqual(point.power_mw, 10)
            self.assertEqual(point.rollover_amplitude_mvpp, peak + 5)
            self.assertEqual(point.rollover_power_mw, 9)

        self.assertEqual(
            generator.events,
            [
                ("off",), ("prepare", 100e6),
                ("frequency", 100e6), ("amplitude", 40), ("on",),
                *[("amplitude", amplitude) for amplitude in range(40, 76, 5)],
                ("off",),
                ("frequency", 101e6), ("amplitude", 40), ("on",),
                *[("amplitude", amplitude) for amplitude in range(40, 96, 5)],
                ("off",), ("off",),
            ],
        )

    def test_unstable_initial_state_stops_before_later_descending_target(self):
        initial_reads = cycle((4.8, 5.2))

        def response(amplitude, frequency_hz):
            return next(initial_reads) if amplitude == 40 else 5

        points, generator = self.scan(CONFIG, response)
        point, = points
        self.assertEqual(point.status, "unstable")
        self.assertIsNone(point.amplitude_mvpp)
        self.assertTrue(all(event[1] == 40 for event in generator.events if event[0] == "amplitude"))
        self.assertEqual(generator.events[-2:], [("off",), ("off",)])

    def test_rollover_discovered_during_bisection_stops_further_probes(self):
        powers = {40: 1, 45: 1.5, 50: 2, 55: 2.5, 60: 3, 65: 3.5, 70: 4.9, 72.5: 4, 75: 5.4}
        points, generator = self.scan(CONFIG, lambda amp, hz: powers.get(amp, 5))
        point, = points
        self.assertEqual(point.status, "rollover")
        self.assertIsNone(point.amplitude_mvpp)
        self.assertEqual((point.best_attempt_mvpp, point.power_mw), (70, 4.9))
        self.assertEqual((point.rollover_amplitude_mvpp, point.rollover_power_mw), (72.5, 4))
        self.assertEqual(
            generator.events[-4:],
            [("amplitude", 75), ("amplitude", 72.5), ("off",), ("off",)],
        )

    def test_small_dip_within_acquisition_scatter_does_not_trip_rollover(self):
        powers = {40: 1, 45: 1.5, 50: 2, 55: 2.5, 60: 3.0, 65: 2.9999, 70: 3.5, 75: 3.75, 80: 4, 85: 4.5, 90: 5}
        noise = cycle((-0.0001, 0.0001))

        def response(amplitude, frequency_hz):
            return powers.get(amplitude, 5) + (next(noise) if amplitude in (60, 65) else 0)

        points, generator = self.scan(CONFIG, response)
        point, = points
        self.assertEqual((point.status, point.amplitude_mvpp), ("ok", 90))
        self.assertEqual(generator.events[-3:], [("amplitude", 90), ("off",), ("off",)])

    def test_noisy_crossing_never_claims_valid_amplitude(self):
        noise = cycle((-1, 1))
        points, _ = self.scan(CONFIG, lambda amp, hz: amp / 20 + next(noise))
        self.assertEqual(points[0].status, "unstable")
        self.assertIsNone(points[0].amplitude_mvpp)

    def test_cancellation_after_meter_read_turns_rf_off(self):
        generator = Generator(CONFIG)
        stop = Event()

        def request_stop(amp, hz):
            stop.set()
            return 5

        with self.assertRaises(ScanStopped):
            run_scan(CONFIG, generator, Meter(generator, request_stop), stop, self.fail)
        self.assertFalse(generator.enabled)
        self.assertEqual(generator.events[-1], ("off",))

    def test_invalid_meter_read_turns_rf_off(self):
        generator = Generator(CONFIG)
        with self.assertRaisesRegex(ValueError, "非有限读数"):
            run_scan(CONFIG, generator, Meter(generator, lambda amp, hz: float("inf")), Event(), self.fail)
        self.assertFalse(generator.enabled)
        self.assertEqual(generator.events[-1], ("off",))

    def test_amplitude_readback_above_limit_aborts_before_another_measurement(self):
        config = replace(CONFIG, target_mw=50)
        generator = Generator(config)
        set_amplitude = generator.set_amplitude_mvpp

        def overshoot_at_limit(amplitude):
            set_amplitude(amplitude)
            return amplitude + 0.05 if amplitude == config.max_mvpp else amplitude

        generator.set_amplitude_mvpp = overshoot_at_limit
        measured = []

        def read_power(amplitude, frequency_hz):
            measured.append(amplitude)
            return amplitude / 20

        with self.assertRaisesRegex(RuntimeError, "上限"):
            run_scan(config, generator, Meter(generator, read_power), Event(), self.fail)
        self.assertFalse(generator.enabled)
        self.assertNotIn(config.max_mvpp, measured)
        self.assertEqual(generator.events[-2:], [("amplitude", config.max_mvpp), ("off",)])

    def test_stop_and_output_off_failure_are_reported_together(self):
        generator = Generator(CONFIG)
        stop = Event()

        def request_stop(amp, hz):
            generator.fail_off = True
            stop.set()
            return 5

        with self.assertRaises(RFOffError) as result:
            run_scan(CONFIG, generator, Meter(generator, request_stop), stop, self.fail)
        self.assertIn("fake RF off timeout", str(result.exception))
        self.assertIn("扫频已停止", str(result.exception))


if __name__ == "__main__":
    unittest.main()
