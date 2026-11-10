"""CH1 load readback, failure handling and UI checks; no real USB access."""
import math
from pathlib import Path
import sys
import tempfile
import threading
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from instruments import VerifiedRigol, LoadReadback, parse_output_load
from app import EqualizerApp


class FakeVisa:
    def __init__(self):
        self.load = '9.9E+37'
        self.commands = []
        self.error = None
        self.ignore_load_write = False

    def query(self, command):
        self.commands.append(('query', command))
        if command == ':OUTP1:LOAD?':
            if self.error:
                raise self.error
            return self.load
        return {':SOUR1:APPL?': 'SIN,130000000,0.001,0,0', ':SOUR1:VOLT:UNIT?': 'VPP',
                ':SOUR1:FREQ?': '130000000'}.get(command, '0')

    def write(self, command):
        self.commands.append(('write', command))
        if command == ':OUTP1:LOAD 50' and not self.ignore_load_write:
            self.load = '50'


def rigol():
    device = VerifiedRigol.__new__(VerifiedRigol)
    device.lock = threading.RLock()
    device.instrument = FakeVisa()
    device.log = lambda _: None
    device.load_readback = None
    device.load_error = ''
    device.output_enabled = False
    device.identity = 'RIGOL TECHNOLOGIES,DG5352 Pro,FAKE,0'
    device._profile_ready = device._frequency_ready = device._amplitude_ready = False
    return device


def pump(root, done):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        root.update()
        if done():
            return
        time.sleep(.005)
    raise AssertionError('UI operation did not finish')


class LoadTests(unittest.TestCase):
    def test_supported_readbacks(self):
        for value in ['9.9E+37', '+9.900000000000000E+37', 'INF', 'INFINITY', 'HighZ']:
            self.assertTrue(math.isinf(parse_output_load(value)))
        for text, value in [('5.00000E+01', 50), ('75', 75), ('1', 1), ('10000', 10000)]:
            self.assertEqual(parse_output_load(text), value)

    def test_invalid_readbacks_do_not_masquerade_as_highz(self):
        for text in ['', 'ERROR', 'nan', '-inf', '0', '-50', '10001', '1E37']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_output_load(text)

    def test_readback_only_queries_load(self):
        generator = rigol()
        self.assertTrue(math.isinf(generator.read_output_load().ohms))
        self.assertEqual(generator.instrument.commands, [('query', ':OUTP1:LOAD?')])
        self.assertFalse(generator.output_enabled)

    def test_failed_refresh_invalidates_previous_readback(self):
        generator = rigol()
        generator.read_output_load()
        generator.instrument.error = OSError('USB unavailable')
        with self.assertRaises(OSError):
            generator.read_output_load()
        self.assertIsNone(generator.load_readback)
        self.assertIn('USB unavailable', generator.load_error)

    def test_prepare_publishes_actual_50_ohm_readback_with_rf_off(self):
        generator = rigol()
        generator.read_output_load()
        generator.prepare(130e6)
        self.assertEqual(generator.load_readback.ohms, 50)
        self.assertFalse(generator.output_enabled)
        self.assertNotIn(('write', ':OUTP1 ON'), generator.instrument.commands)

    def test_prepare_cannot_enable_rf_when_load_setting_did_not_take(self):
        generator = rigol()
        generator.instrument.ignore_load_write = True
        with self.assertRaisesRegex(RuntimeError, '扫描要求 50'):
            generator.prepare(130e6)
        with self.assertRaises(RuntimeError):
            generator.output_on()
        self.assertTrue(math.isinf(generator.load_readback.ohms))
        self.assertNotIn(('write', ':OUTP1 ON'), generator.instrument.commands)


class LoadUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'tests', prefix='aom_load_')
        self.root = tk.Tk()
        self.root.withdraw()
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.app = EqualizerApp(self.root, data_dir=self.tmp.name, auto_discover=False)

    def tearDown(self):
        self.app.worker.shutdown(wait=True)
        for timer in self.root.tk.call('after', 'info'):
            self.root.after_cancel(timer)
        self.root.destroy()
        self.tmp.cleanup()
        self.assertEqual(self.errors, [])

    def test_offline_load_is_unknown_and_refresh_is_disabled(self):
        self.assertIn('未知', self.app.load_state.get())
        self.assertEqual(str(self.app.load_refresh_button['state']), 'disabled')
        self.app.refresh_output_load()
        self.assertFalse(self.app.busy)

    def test_refresh_reads_actual_mode_without_changing_scan_data(self):
        self.app.restore_measurements([ROOT / 'tests/fixtures/synthetic_scan.csv'])
        before = (list(self.app.points), self.app._settings_snapshot())
        self.app.generator = generator = rigol()
        self.app._update_controls()
        self.app.load_refresh_button.invoke()
        pump(self.root, lambda: not self.app.busy)
        self.assertIn('高阻', self.app.load_state.get())
        generator.instrument.load = '50'
        self.app.load_refresh_button.invoke()
        pump(self.root, lambda: not self.app.busy)
        self.assertIn('50 Ω', self.app.load_state.get())
        self.assertEqual((self.app.points, self.app._settings_snapshot()), before)
        self.assertEqual(generator.instrument.commands, [('query', ':OUTP1:LOAD?')] * 2)

    def test_busy_scan_never_queues_load_queries(self):
        self.app.generator = generator = rigol()
        self.app.busy, self.app.operation = True, 'scan'
        self.app._update_controls()
        self.app.refresh_output_load()
        self.assertEqual(str(self.app.load_refresh_button['state']), 'disabled')
        self.assertEqual(generator.instrument.commands, [])
        self.app.busy = False

    def test_failed_refresh_clears_previous_value_in_ui(self):
        self.app.generator = generator = rigol()
        generator.read_output_load()
        self.app._update_load_display()
        generator.instrument.error = OSError('USB unavailable')
        self.app.refresh_output_load()
        pump(self.root, lambda: not self.app.busy)
        self.assertIn('读取失败', self.app.load_state.get())
        self.assertNotIn('高阻', self.app.load_state.get())
        self.assertIn('USB unavailable', self.app.notice.get())

    def test_cached_load_update_never_queries_instrument_on_tk_thread(self):
        self.app.generator = generator = rigol()
        generator.load_readback = LoadReadback(75, '20:00:00')
        self.app._update_load_display()
        self.assertIn('75 Ω', self.app.load_state.get())
        self.assertEqual(generator.instrument.commands, [])
        self.app.generator = None
        self.app._update_load_display()
        self.assertIn('未连接', self.app.load_state.get())

    def test_connect_populates_load_and_load_failure_is_explicit(self):
        generator = rigol()
        meter = SimpleNamespace(identity={'model': 'PM100D3', 'serial': 'TEST'}, read_mw=lambda: 0)
        with patch('app.connect', return_value=(generator, meter)):
            self.app.connect_devices()
            pump(self.root, lambda: not self.app.busy)
        self.assertIn('高阻', self.app.load_state.get())
        self.assertIsNotNone(self.app.device_settings)
        self.app.generator = self.app.meter = None
        generator.instrument.error = OSError('load read error')
        with patch('app.connect', return_value=(generator, meter)):
            self.app.connect_devices()
            pump(self.root, lambda: not self.app.busy)
        self.assertIn('读取失败', self.app.load_state.get())
        self.assertIn('load read error', self.app.notice.get())
        self.assertIsNotNone(self.app.device_settings)


if __name__ == '__main__':
    unittest.main()
