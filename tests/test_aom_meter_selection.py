"""Meter-selection regressions with mocked USB discovery and withdrawn Tk."""

import csv
import json
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))
import app as app_module
import instruments
from scan import RFOffError, ScanPoint, ScanConfig
from scan import run_scan as run_real_scan
from test_aom_refinement import ResponseRig


METER_A = {"model": "PM100USB", "serial": "TEST-USB-001", "resource": "USB::METER_A", "available": True}
METER_B = {"model": "PM100D3", "serial": "TEST-D3-001", "resource": "USB::METER_B", "available": True}


def pump(root, condition, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError("Mocked Tk operation did not finish")


class MeterSelectionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="aom_meter_test_", dir=WORKSPACE / "tests")
        self.root = tk.Tk()
        self.root.withdraw()
        self.callback_errors = []
        self.root.report_callback_exception = (
            lambda kind, value, trace: self.callback_errors.append(f"{kind.__name__}: {value}")
        )
        self.discovery = Mock(return_value=[dict(METER_A), dict(METER_B)])
        self.connector = Mock(side_effect=AssertionError("Unexpected instrument connection"))
        self.discover_patch = patch.object(app_module, "discover_power_meters", self.discovery)
        self.connect_patch = patch.object(app_module, "connect", self.connector)
        self.discover_patch.start()
        self.connect_patch.start()
        self.app = app_module.EqualizerApp(self.root, simulate=False, data_dir=self.folder.name)
        self.app.vars['power_stop'].set('60')
        pump(self.root, lambda: not self.app.busy)

    def tearDown(self):
        self.app.worker.shutdown(wait=True)
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.root.destroy()
        self.discover_patch.stop()
        self.connect_patch.stop()
        self.folder.cleanup()
        self.assertEqual(self.callback_errors, [])

    def refresh(self, meters):
        self.discovery.return_value = [dict(meter) for meter in meters]
        self.app.refresh_power_meters()
        pump(self.root, lambda: not self.app.busy)

    def choose(self, serial):
        values = self.app.meter_combo["values"]
        label = next(value for value in values if serial in value)
        self.app.meter_combo.set(label)
        self.app.meter_combo.event_generate("<<ComboboxSelected>>")
        self.root.update()
        self.assertEqual(self.app.vars["serial"].get(), serial)

    def test_both_models_are_listed_and_selected_label_maps_to_correct_serial(self):
        self.refresh([METER_A, METER_B])
        values = self.app.meter_combo["values"]
        self.assertEqual(len(values), 2)
        self.assertTrue(any(METER_A["model"] in value and METER_A["serial"] in value for value in values))
        self.assertTrue(any(METER_B["model"] in value and METER_B["serial"] in value for value in values))
        self.choose(METER_B["serial"])
        self.connector.assert_not_called()

    def test_one_meter_is_auto_selected_but_two_need_an_explicit_choice(self):
        self.refresh([METER_A])
        self.assertEqual(self.app.vars["serial"].get(), METER_A["serial"])
        self.app.vars["serial"].set("")
        self.refresh([METER_A, METER_B])
        self.assertEqual(self.app.vars["serial"].get(), "")
        self.app.connect_devices()
        self.root.update()
        self.connector.assert_not_called()

    def test_selected_serial_is_forwarded_to_connect(self):
        self.refresh([METER_A, METER_B])
        self.choose(METER_B["serial"])
        generator = SimpleNamespace(output_off=Mock(), close=Mock())
        meter = SimpleNamespace(
            identity={"model": METER_B["model"], "serial": METER_B["serial"], "wavelength_nm": 556.0},
            read_mw=Mock(return_value=0.25),
            close=Mock(),
        )
        self.connector.side_effect = None
        self.connector.return_value = (generator, meter)
        self.app.connect_devices()
        pump(self.root, lambda: not self.app.busy)
        self.assertEqual(self.connector.call_args.args[2], METER_B["serial"])
        self.assertIs(self.app.meter, meter)

    def test_refresh_preserves_selected_present_meter_and_measurement_fields(self):
        self.refresh([METER_A, METER_B])
        self.choose(METER_B["serial"])
        values = {"target": "50", "tolerance": "2", "start": "140", "stop": "150",
                  "step": "0.5", "amp_min": "0", "amp_max": "300", "wavelength": "400", "incident": "51"}
        for key, value in values.items():
            self.app.vars[key].set(value)
        self.refresh([METER_B, METER_A])
        self.assertEqual(self.app.vars["serial"].get(), METER_B["serial"])
        for key, value in values.items():
            self.assertEqual(self.app.vars[key].get(), value)

    def test_disconnected_selection_is_cleared_instead_of_connecting_another_meter(self):
        self.refresh([METER_A, METER_B])
        self.choose(METER_B["serial"])
        self.refresh([METER_A])
        self.assertEqual(self.app.vars["serial"].get(), "")

    def test_refresh_during_scan_is_disabled_and_does_not_call_discovery(self):
        self.refresh([METER_A, METER_B])
        self.discovery.reset_mock()
        self.app.operation = "scan"
        self.app.busy = True
        self.app._update_controls()
        self.assertEqual(str(self.app.refresh_button["state"]), "disabled")
        self.assertEqual(str(self.app.meter_combo["state"]), "disabled")
        self.app.refresh_power_meters()
        self.discovery.assert_not_called()
        self.app.busy = False
        self.app._update_controls()

    def test_amplitude_step_can_be_applied_during_scan_without_enabling_other_settings(self):
        self.app.config = ScanConfig(130,130,.5,50,5,0,350)
        self.app.operation = 'scan'
        self.app.busy = True
        self.app._update_controls()
        self.assertEqual(str(self.app.amplitude_step_entry['state']),'normal')
        self.assertEqual(str(self.app.amplitude_step_button['state']),'normal')
        self.assertEqual(str(self.app.refresh_button['state']),'disabled')
        self.app.vars['amp_step'].set('12.5')
        self.app.apply_amplitude_step()
        self.assertEqual(self.app._read_amplitude_step(),12.5)
        self.app.vars['amp_step'].set('NaN')
        self.app.apply_amplitude_step()
        self.assertEqual(self.app._read_amplitude_step(),12.5)
        self.app.busy=False
        self.app._update_controls()

    def test_sdk_unavailable_flag_allows_explicit_selection_without_auto_connect(self):
        unavailable = dict(METER_B, available=False)
        self.refresh([METER_A, unavailable])
        label = next(value for value in self.app.meter_combo["values"] if METER_B["serial"] in value)
        self.app.meter_combo.set(label)
        self.app.meter_combo.event_generate("<<ComboboxSelected>>")
        self.root.update()
        self.assertEqual(self.app.vars["serial"].get(), METER_B["serial"])
        self.connector.assert_not_called()

    def test_explicit_selected_serial_survives_refresh_to_sdk_unavailable(self):
        self.refresh([METER_A, METER_B])
        self.choose(METER_B["serial"])
        self.refresh([METER_A, dict(METER_B, available=False)])
        self.assertEqual(self.app.vars["serial"].get(), METER_B["serial"])
        self.assertIn(METER_B["serial"], self.app.meter_choice.get())
        self.connector.assert_not_called()

    def test_current_owned_meter_stays_selected_when_driver_marks_handle_busy(self):
        self.refresh([METER_A, METER_B])
        self.choose(METER_B["serial"])
        self.app.generator = SimpleNamespace(output_off=Mock(), close=Mock())
        self.app.meter = SimpleNamespace(
            identity={"model": METER_B["model"], "serial": METER_B["serial"], "wavelength_nm": 556.0},
            read_mw=Mock(return_value=0.25),
            close=Mock(),
        )
        self.app.device_settings = ("", METER_B["serial"], 556.0)
        self.refresh([METER_A, dict(METER_B, available=False)])
        self.assertEqual(self.app.vars["serial"].get(), METER_B["serial"])
        self.assertIn("当前连接", self.app.meter_choice.get())
        self.connector.assert_not_called()

    def test_open_session_with_blank_raw_identity_is_restored_only_after_live_read(self):
        resource = "USB0::0x1313::0x8072::::INSTR"
        self.refresh([METER_A])
        read_power = Mock(return_value=0.25)
        self.app.generator = SimpleNamespace(output_off=Mock(), close=Mock())
        self.app.meter = SimpleNamespace(
            identity={"model": METER_A["model"], "serial": METER_A["serial"],
                      "resource": resource, "wavelength_nm": 556.0},
            read_mw=read_power, close=Mock(),
        )
        self.app.device_settings = ("", METER_A["serial"], 556.0)
        raw = {"model": "PM100USB", "serial": "", "resource": resource, "available": False}
        self.refresh([raw])
        read_power.assert_called_once_with()
        self.assertEqual(self.app.vars["serial"].get(), METER_A["serial"])
        self.assertIn(METER_A["serial"], self.app.meter_choice.get())
        self.assertIn("当前连接", self.app.meter_choice.get())
        selected = self.app.meter_choices[self.app.meter_choice.get()]
        self.assertTrue(selected["available"])
        self.assertEqual(selected["resource"], resource)
        self.app._require_current_connection()
        self.assertEqual(str(self.app.start_button["state"]), "normal")
        self.connector.assert_not_called()

    def test_failed_live_read_does_not_restore_blank_raw_identity_from_cache(self):
        resource = "USB0::0x1313::0x8072::::INSTR"
        self.refresh([METER_A])
        read_power = Mock(side_effect=RuntimeError("Power meter session disconnected"))
        self.app.generator = SimpleNamespace(output_off=Mock(), close=Mock())
        self.app.meter = SimpleNamespace(
            identity={"model": METER_A["model"], "serial": METER_A["serial"],
                      "resource": resource, "wavelength_nm": 556.0},
            read_mw=read_power, close=Mock(),
        )
        self.app.device_settings = ("", METER_A["serial"], 556.0)
        raw = {"model": "PM100USB", "serial": "", "resource": resource, "available": False}
        self.refresh([raw])
        read_power.assert_called_once_with()
        self.assertFalse(any(record.get("available") and record.get("serial") == METER_A["serial"]
                             for record in self.app.meter_choices.values()))
        self.assertNotIn("当前连接", self.app.meter_choice.get())
        self.assertIsNone(self.app.device_settings)
        with self.assertRaises(ValueError):
            self.app._require_current_connection()
        self.assertIn("disconnected", self.app.notice.get().lower())
        self.connector.assert_not_called()

    def test_swapping_meters_releases_old_handles_before_opening_new_selection(self):
        self.refresh([METER_A, METER_B])
        events = []
        self.app.generator = SimpleNamespace(
            output_off=lambda: events.append("RF off"),
            close=lambda: events.append("old generator closed"),
        )
        self.app.meter = SimpleNamespace(
            identity={"model": METER_A["model"], "serial": METER_A["serial"], "wavelength_nm": 556.0},
            close=lambda: events.append("old meter closed"),
        )
        self.app.device_settings = ("", METER_A["serial"], 556.0)
        self.app.vars["incident"].set("12.34")
        self.choose(METER_B["serial"])
        new_generator = SimpleNamespace(output_off=Mock(), close=Mock())
        new_meter = SimpleNamespace(
            identity={"model": METER_B["model"], "serial": METER_B["serial"], "wavelength_nm": 556.0},
            read_mw=Mock(return_value=0.25),
            close=Mock(),
        )

        def open_selected(simulate, resource, serial, wavelength, log):
            self.assertEqual(events, ["RF off", "old generator closed", "old meter closed"])
            self.assertEqual(serial, METER_B["serial"])
            events.append("new selection connected")
            return new_generator, new_meter

        self.connector.side_effect = open_selected
        self.app.connect_devices()
        pump(self.root, lambda: not self.app.busy)
        self.assertIs(self.app.meter, new_meter)
        self.assertEqual(self.app.vars["incident"].get(), "")
        self.assertEqual(events[-1], "new selection connected")

    def test_failed_connection_preserves_user_reference_field(self):
        self.refresh([METER_A, METER_B])
        self.choose(METER_B["serial"])
        self.app.vars["incident"].set("12.34")
        self.connector.side_effect = RuntimeError("Selected meter was unplugged")
        self.app.connect_devices()
        pump(self.root, lambda: not self.app.busy)
        self.assertEqual(self.app.vars["incident"].get(), "12.34")
        self.assertIsNone(self.app.meter)

    def test_disconnect_releases_both_handles_even_when_rf_shutdown_fails(self):
        error = RuntimeError("stale generator USB handle")
        generator = SimpleNamespace(output_off=Mock(side_effect=error), close=Mock())
        meter = SimpleNamespace(close=Mock())
        self.app.generator, self.app.meter = generator, meter
        self.app.device_settings = ("", METER_B["serial"], 400)
        with self.assertRaisesRegex(RuntimeError, "stale generator"):
            self.app._disconnect()
        generator.close.assert_called_once_with()
        meter.close.assert_called_once_with()
        self.assertIsNone(self.app.generator)
        self.assertIsNone(self.app.meter)
        self.assertIsNone(self.app.device_settings)
        self.app._disconnect()
        generator.close.assert_called_once_with()

    def test_disconnect_preserves_all_cleanup_errors_and_drops_failed_handles(self):
        error = RuntimeError("RF state unknown")
        generator = SimpleNamespace(output_off=Mock(side_effect=error),
                                    close=Mock(side_effect=RuntimeError("generator close failed")))
        meter = SimpleNamespace(close=Mock(side_effect=RuntimeError("meter close failed")))
        self.app.generator, self.app.meter = generator, meter
        with self.assertRaisesRegex(RuntimeError, "RF state unknown"):
            self.app._disconnect()
        self.assertIn("generator close failed", " ".join(error.__notes__))
        self.assertIn("meter close failed", " ".join(error.__notes__))
        self.assertIsNone(self.app.generator)
        self.assertIsNone(self.app.meter)

    def test_unresolved_attempt_stays_in_table_details_without_a_false_result_marker(self):
        self.app.config = ScanConfig(140,150,1,50,5,0,350)
        self.app.points = [ScanPoint(142,None,56,.2058,'unstable',8,best_attempt_mvpp=226.9607)]
        with patch.object(self.app.chart,'winfo_width',return_value=800), patch.object(self.app.chart,'winfo_height',return_value=500):
            self.app._draw()
        markers = [item for item in self.app.chart.find_all() if self.app.chart.type(item)=='text'
                   and self.app.chart.itemcget(item,'text')=='×']
        self.assertEqual(markers,[])
        self.app._refresh_table()
        self.assertEqual(self.app.table.item('0','values')[1],'226.961')
        self.assertIn('待补测',self.app.table.item('0','values')[3])
        detail=self.app.annotation_text.get('1.0','end')
        self.assertIn('142 MHz',detail)
        self.assertIn('实测 56 mW',detail)
        self.assertIn('不作能否达到的结论',detail)

    def test_in_range_points_are_drawn_despite_failed_checks_and_scatter(self):
        self.app.config = ScanConfig(140,150,1,50,5,0,350)
        self.app.points = [ScanPoint(140+i,None,power,9,'unstable',8,best_attempt_mvpp=220+i)
                           for i,power in enumerate([45,55,50.328])]
        with patch.object(self.app.chart,'winfo_width',return_value=800), patch.object(self.app.chart,'winfo_height',return_value=500):
            self.app._draw()
        ovals = [item for item in self.app.chart.find_all() if self.app.chart.type(item)=='oval']
        crosses = [item for item in self.app.chart.find_all() if self.app.chart.type(item)=='text'
                   and self.app.chart.itemcget(item,'text')=='×']
        self.assertEqual(len(ovals),3)
        self.assertEqual(crosses,[])

    def test_confirmed_maximum_has_diamond_label_and_breaks_target_line(self):
        self.app.config=ScanConfig(140,150,1,50,2,0,350)
        peak=ScanPoint(145,308,44,.01,'maximum_below_target',20,
                       maximum_kind='local_peak',tested_min_mvpp=230,tested_max_mvpp=320,
                       peak_bracket_low_mvpp=307,peak_bracket_high_mvpp=309)
        self.app.points=[ScanPoint(140,230,49,0,'ok',2),peak,ScanPoint(150,231,50,0,'ok',2)]
        with patch.object(self.app.chart,'winfo_width',return_value=900),patch.object(self.app.chart,'winfo_height',return_value=500):
            self.app._draw()
        diamonds=[i for i in self.app.chart.find_all() if self.app.chart.type(i)=='polygon']
        self.assertEqual(len(diamonds),1)
        target_lines=[i for i in self.app.chart.find_all() if self.app.chart.type(i)=='line'
                      and self.app.chart.itemcget(i,'fill')==app_module.TEAL]
        self.assertEqual(target_lines,[])
        labels=[self.app.chart.itemcget(i,'text') for i in self.app.chart.find_all()
                if self.app.chart.type(i)=='text']
        self.assertFalse(any('mW' in label for label in labels))
        self.assertIn('已测分支峰值 44 mW @ 308 mVpp',self.app.annotation_text.get('1.0','end'))
        self.assertEqual(app_module.point_display(peak,self.app.config)[1],'maximum')
        uncertain=replace(peak,status='maximum_unconfirmed',maximum_kind='')
        self.assertEqual(app_module.point_display(uncertain,self.app.config)[1],'out_of_range')

    def test_maximum_csv_retains_scope_amplitude_power_and_summary(self):
        self.prepare_mock_scan(Mock(return_value=0))
        peak=ScanPoint(100,298,40,.01,'maximum_below_target',20,
                       maximum_kind='local_peak',tested_min_mvpp=220,tested_max_mvpp=300,
                       peak_bracket_low_mvpp=297,peak_bracket_high_mvpp=299)
        def run_mock(config,generator,meter,stop,on_point,*args,**kwargs):
            on_point(peak)
            return [peak]
        with patch.object(app_module,'run_scan',side_effect=run_mock):
            self.app.start_scan()
            pump(self.root,lambda:not self.app.busy)
        with self.app.csv_path.open(encoding='utf-8-sig',newline='') as f:
            row,=list(csv.DictReader(f))
        self.assertEqual(row['plot_status'],'maximum')
        self.assertEqual(row['maximum_kind'],'local_peak')
        self.assertEqual(float(row['amplitude_mvpp']),298)
        self.assertEqual(float(row['output_power_mw']),40)
        self.assertEqual(float(row['peak_bracket_high_mvpp']),299)
        self.assertEqual(float(row['tested_max_mvpp']),300)
        self.assertIn('1 个上限内达不到',self.app.trial_state.get())
        self.assertNotIn('无有效读数',self.app.trial_state.get())

    def test_invalid_readings_are_not_mislabelled_as_outside_range(self):
        config = ScanConfig(140,150,1,50,5,0,350)
        for power in [None,float('nan'),float('inf')]:
            with self.subTest(power=power):
                point = ScanPoint(140,None,power,0,'unstable',1,best_attempt_mvpp=220)
                amplitude,state,reason = app_module.point_display(point,config)
                self.assertIsNone(amplitude)
                self.assertEqual(state,'no_reading')
                self.assertNotIn('超出',reason)

    def test_display_classifier_uses_reading_even_for_ok_status(self):
        config = ScanConfig(140,150,1,50,5,0,350)
        for power,state in [(44.999,'out_of_range'),(45,'in_range'),(55,'in_range'),(55.001,'out_of_range')]:
            with self.subTest(power=power):
                point = ScanPoint(140,220,power,0,'ok',1)
                self.assertEqual(app_module.point_display(point,config)[1],state)

    def test_scan_display_summary_and_csv_use_user_range_preserving_check_results(self):
        self.prepare_mock_scan(Mock(return_value=0))
        for name,value in {'target':'50','tolerance':'5','stop':'102'}.items():
            self.app.vars[name].set(value)
        points = [ScanPoint(100,None,50.328,6,'unstable',8,best_attempt_mvpp=226.96,reason='复测检查未通过'),
                  ScanPoint(101,None,56,.1,'unreachable',8,best_attempt_mvpp=240),
                  ScanPoint(102,None,None,0,'unstable',1)]
        def run_mock(config,generator,meter,stop,on_point,*args,**kwargs):
            for point in points:
                on_point(point)
            return points
        with patch.object(app_module,'run_scan',side_effect=run_mock):
            self.app.start_scan()
            pump(self.root,lambda:not self.app.busy)
        with self.app.csv_path.open(encoding='utf-8-sig',newline='') as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([r['plot_status'] for r in rows],['in_range','out_of_range','no_reading'])
        self.assertEqual(rows[0]['status'],'unstable')
        self.assertEqual(rows[0]['reason'],'复测检查未通过')
        self.assertEqual(rows[0]['amplitude_mvpp'],'')
        self.assertEqual(float(rows[0]['plot_amplitude_mvpp']),226.96)
        self.assertEqual(self.app.trial_state.get(),'1 个达到目标  ·  0 个上限内达不到  ·  2 个待补测（不计入结果）')

    def test_scan_csv_records_selected_meter_model_and_actual_serial(self):
        self.refresh([METER_A, METER_B])
        self.choose(METER_B["serial"])
        generator = SimpleNamespace(identity="MOCK,DG5352 Pro,TEST", output_off=Mock(), close=Mock())
        meter = SimpleNamespace(
            identity={"model": METER_B["model"], "serial": METER_B["serial"], "wavelength_nm": 556.0},
            read_mw=Mock(side_effect=[0.25, 0.0]),
            close=Mock(),
        )
        self.connector.side_effect = None
        self.connector.return_value = generator, meter
        self.app.connect_devices()
        pump(self.root, lambda: not self.app.busy)
        fields = {"start": "100", "stop": "100", "step": "1", "target": "50",
                  "tolerance": "2", "amp_min": "220", "amp_max": "300"}
        for name, value in fields.items():
            self.app.vars[name].set(value)
        point = ScanPoint(100, 210, 50, 0, "ok", 2, verification_error_mw=0)

        def run_mock(config, actual_generator, actual_meter, stop, on_point, *callbacks, on_sample=None):
            self.assertIs(actual_meter, meter)
            self.assertIs(actual_generator, generator)
            self.assertEqual(config.min_mvpp, 0)
            self.assertEqual(config.initial_mvpp, 220)
            self.assertEqual(meter.read_mw.call_count, 2)
            on_point(point)
            return [point]

        with patch.object(app_module, "run_scan", side_effect=run_mock):
            self.app.start_scan()
            pump(self.root, lambda: not self.app.busy)
        self.assertEqual(self.app.points, [point], self.app.notice.get())
        with self.app.csv_path.open(encoding="utf-8-sig", newline="") as handle:
            row, = list(csv.DictReader(handle))
        self.assertEqual(row["meter_model"], METER_B["model"])
        self.assertEqual(row["meter_serial"], METER_B["serial"])
        self.assertEqual(float(row["wavelength_nm"]), 556)
        self.assertEqual(float(row["target_power_mw"]), 50)
        self.assertEqual(float(row["initial_mvpp"]), 220)
        for name, value in fields.items():
            self.assertEqual(self.app.vars[name].get(), value)

    def prepare_mock_scan(self, reading):
        generator = SimpleNamespace(
            identity="MOCK,DG5352 Pro,TEST", output_off=Mock(), output_on=Mock(), close=Mock(),
        )
        meter = SimpleNamespace(
            identity={"model": METER_A["model"], "serial": METER_A["serial"], "wavelength_nm": 556.0},
            read_mw=reading, close=Mock(),
        )
        self.app.generator, self.app.meter = generator, meter
        self.app.vars["serial"].set(METER_A["serial"])
        self.app.device_settings = ("", METER_A["serial"], 556.0)
        for name, value in {"start": "100", "stop": "101", "step": "1", "target": "45",
                            "tolerance": "2", "amp_min": "220", "amp_max": "300"}.items():
            self.app.vars[name].set(value)
        self.app._update_controls()
        return generator, meter

    def test_recovery_resume_keeps_original_csv_and_appends_only_missing_frequency(self):
        self.prepare_mock_scan(Mock(return_value=0))
        first=ScanPoint(100,220,44.5,.01,'ok',2)
        def interrupted(config,generator,meter,stop,on_point,*args,**kwargs):
            on_point(first)
            raise app_module.ScanStopped('test interruption')
        with patch.object(app_module,'run_scan',side_effect=interrupted):
            self.app.start_scan()
            pump(self.root,lambda:not self.app.busy)
        original=self.app.csv_path
        original_bytes=original.read_bytes()
        self.app.points=[]
        self.app.restore_measurements([original])
        self.assertEqual(self.app.points,[first])
        self.assertEqual(self.app.restored_frequencies,{100})
        def resume(config,generator,meter,stop,on_point,*args,**kwargs):
            self.assertEqual(kwargs['completed_points'],[first])
            self.assertEqual(self.app.points,[first])
            second=ScanPoint(101,221,44.7,.01,'ok',2)
            on_point(second)
            return [first,second]
        with patch.object(app_module,'run_scan',side_effect=resume):
            self.app.resume_scan()
            pump(self.root,lambda:not self.app.busy)
        self.assertEqual(original.read_bytes(),original_bytes)
        self.assertNotEqual(self.app.csv_path,original)
        self.assertEqual([p.frequency_mhz for p in self.app.points],[100,101])
        with self.app.csv_path.open(encoding='utf-8-sig',newline='') as stream:
            rows=list(csv.DictReader(stream))
        self.assertEqual([r['restored'] for r in rows],['True','False'])
        self.assertEqual(Path(rows[0]['source_csv']),original.resolve())
        self.assertTrue(app_module.session_path(self.app.csv_path).exists())
        self.assertEqual(len(self.app.table.get_children()),2)
        self.assertEqual(app_module.read_session(self.app.csv_path)['state'],'completed')
        with patch.object(app_module,'run_scan') as run:
            self.app.resume_scan()
        run.assert_not_called()

    def test_resume_remeasures_unresolved_attempt_without_erasing_original_or_valid_points(self):
        self.prepare_mock_scan(Mock(return_value=0))
        first=ScanPoint(100,220,44.5,.01,'ok',2)
        unresolved=ScanPoint(101,None,38,.01,'maximum_unconfirmed',9,best_attempt_mvpp=250)
        def initial(config,generator,meter,stop,on_point,*args,**kwargs):
            on_point(first);on_point(unresolved)
            return [first,unresolved]
        with patch.object(app_module,'run_scan',side_effect=initial):
            self.app.start_scan();pump(self.root,lambda:not self.app.busy)
        original=self.app.csv_path;original_bytes=original.read_bytes()
        self.assertEqual(app_module.read_session(original)['state'],'incomplete')
        self.assertEqual(str(self.app.resume_button['state']),'normal')
        replacement=ScanPoint(101,255,44.6,.02,'ok',3)
        def resumed(config,generator,meter,stop,on_point,*args,**kwargs):
            self.assertEqual(kwargs['completed_points'],[first])
            self.assertEqual(self.app.points,[first,unresolved])
            on_point(replacement)
            return [first,replacement]
        with patch.object(app_module,'run_scan',side_effect=resumed):
            self.app.resume_scan();pump(self.root,lambda:not self.app.busy)
        self.assertEqual(original.read_bytes(),original_bytes)
        self.assertEqual(self.app.points,[first,replacement])
        self.assertEqual(app_module.read_session(self.app.csv_path)['state'],'completed')
        self.app.restore_measurements([self.app.csv_path])
        self.assertEqual(self.app.points,[first,replacement])

    def test_many_limit_points_have_separate_complete_readable_details(self):
        self.app.config=ScanConfig(130,140,1,50,1,0,350)
        self.app.points=[ScanPoint(f,350,40+(f-130)*.2,.01,'maximum_below_target',10,
                            maximum_kind='rf_limit',tested_min_mvpp=230,tested_max_mvpp=350) for f in range(130,141)]
        self.app._refresh_table()
        with patch.object(self.app.chart,'winfo_width',return_value=650),patch.object(self.app.chart,'winfo_height',return_value=240):
            self.app._draw()
        detail=self.app.annotation_text.get('1.0','end')
        for f in range(130,141):self.assertIn(f'{f} MHz',detail)
        labels=[self.app.chart.itemcget(i,'text') for i in self.app.chart.find_all() if self.app.chart.type(i)=='text']
        self.assertFalse(any('mW' in t or '未达标' in t for t in labels))
        self.assertEqual(len([i for i in self.app.chart.find_all() if self.app.chart.type(i)=='polygon']),11)
        self.app._select_plot_point(3)
        self.assertEqual(self.app.table.selection(),('3',))
        self.assertIn('133 MHz',self.app.notice.get())

    def test_data_button_reads_finished_snapshot_without_any_instrument_command(self):
        generator,meter=self.prepare_mock_scan(Mock(return_value=0))
        def finished(config,generator,meter,stop,on_point,*args,**kwargs):
            p=ScanPoint(100,220.1234567,44.567891,.01,'ok',2)
            on_point(p);return [p]
        with patch.object(app_module,'run_scan',side_effect=finished):
            self.app.start_scan();pump(self.root,lambda:not self.app.busy)
        generator.output_off.reset_mock();generator.output_on.reset_mock();meter.read_mw.reset_mock()
        self.assertEqual(str(self.app.data_button['state']),'normal')
        self.app.show_data_table()
        self.assertEqual(len(self.app.data_window.table.get_children()),1)
        generator.output_off.assert_not_called();generator.output_on.assert_not_called();meter.read_mw.assert_not_called()
        self.assertFalse(self.app.stop_event.is_set())
        self.app.busy=True;self.app.operation='scan';self.app._update_controls()
        self.assertEqual(str(self.app.data_button['state']),'disabled')
        with patch.object(app_module,'DataTableWindow') as view:
            self.app.show_data_table();view.assert_not_called()
        self.assertFalse(self.app.stop_event.is_set())
        self.app.busy=False

    def test_offline_start_does_not_discover_or_connect_instruments(self):
        other_root=tk.Tk();other_root.withdraw()
        self.discovery.reset_mock();self.connector.reset_mock()
        offline=app_module.EqualizerApp(other_root,data_dir=self.folder.name,auto_discover=False)
        try:
            other_root.update()
            self.discovery.assert_not_called();self.connector.assert_not_called()
            self.assertFalse(offline.busy)
            self.assertIn('查看和保存',offline.device_state.get())
        finally:
            offline.worker.shutdown(wait=True)
            for timer in other_root.tk.call('after','info'):other_root.after_cancel(timer)
            other_root.destroy()

    def test_loaded_settings_and_newest_duplicate_win_but_other_targets_are_rejected(self):
        self.prepare_mock_scan(Mock(return_value=0))
        files=[]
        for amplitude in [220,222]:
            def one(config,generator,meter,stop,on_point,*args,**kwargs):
                point=ScanPoint(100,amplitude,44.5,.01,'ok',2)
                on_point(point)
                return [point]
            with patch.object(app_module,'run_scan',side_effect=one):
                self.app.start_scan()
                pump(self.root,lambda:not self.app.busy)
            files.append(self.app.csv_path)
        self.app.vars['target'].set('100')
        self.app.restore_measurements(files)
        self.assertEqual(self.app.vars['target'].get(),'45')
        self.assertEqual(len(self.app.points),1)
        self.assertEqual(self.app.points[0].amplitude_mvpp,222)
        self.app.vars['target'].set('46')
        with patch.object(app_module,'run_scan') as run:
            self.app.resume_scan()
        run.assert_not_called()
        self.assertIn('不同',self.app.notice.get())

    def test_loading_while_scan_busy_never_cancels_running_operation(self):
        self.app.busy=True
        self.app.operation='scan'
        with self.assertRaises(ValueError):
            self.app.restore_measurements(['does_not_exist.csv'])
        self.assertFalse(self.app.stop_event.is_set())
        self.app.busy=False

    def test_resume_preflight_error_preserves_loaded_data_and_recovery_source(self):
        _,meter=self.prepare_mock_scan(Mock(return_value=0))
        point=ScanPoint(100,220,44.5,.01,'ok',2)
        def first(config,generator,meter,stop,on_point,*args,**kwargs):
            on_point(point)
            return [point]
        with patch.object(app_module,'run_scan',side_effect=first):
            self.app.start_scan()
            pump(self.root,lambda:not self.app.busy)
        original=self.app.csv_path
        self.app.restore_measurements([original])
        original_bytes=original.read_bytes()
        meter.read_mw.side_effect=RuntimeError('USB failed before resuming')
        self.app.resume_scan()
        pump(self.root,lambda:not self.app.busy)
        self.assertEqual(self.app.points,[point])
        self.assertEqual(self.app.recovery_paths,(original,))
        self.assertEqual(original.read_bytes(),original_bytes)

    def assert_meter_requires_reconnect(self):
        self.assertIsNone(self.app.meter)
        self.assertIsNone(self.app.device_settings)
        self.assertEqual(str(self.app.start_button["state"]), "disabled")
        self.assertEqual(str(self.app.incident_button["state"]), "disabled")

    def test_usb_error_before_scan_never_starts_rf_or_creates_csv(self):
        error = RuntimeError("TLPMX measPower: -1073806848: USB control transfer stalled")
        generator, meter = self.prepare_mock_scan(Mock(side_effect=error))
        with patch.object(app_module, "run_scan") as run:
            self.app.start_scan()
            pump(self.root, lambda: not self.app.busy)
        run.assert_not_called()
        generator.output_on.assert_not_called()
        generator.output_off.assert_called()
        meter.close.assert_called_once_with()
        self.assert_meter_requires_reconnect()
        self.assertEqual(list(Path(self.folder.name).glob("*.csv")), [])
        self.assertIn("-1073806848", self.app.notice.get())
        records = [json.loads(line) for line in (Path(self.folder.name) / "connection_errors.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[-1]["operation"], "scan")
        self.assertIn("-1073806848", records[-1]["traceback"])

    def test_later_connection_failure_does_not_overwrite_first_error_record(self):
        for message in ("first USB error", "later timeout"):
            self.app._submit("connect", Mock(side_effect=RuntimeError(message)))
            pump(self.root, lambda: not self.app.busy)
        records = [json.loads(line) for line in (Path(self.folder.name) / "connection_errors.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["error"] for row in records], ["first USB error", "later timeout"])
        self.assertTrue(all(row["time"] and row["operation"] == "connect" for row in records))

    def test_nonfinite_preflight_read_never_starts_rf(self):
        generator, meter = self.prepare_mock_scan(Mock(return_value=float("nan")))
        with patch.object(app_module, "run_scan") as run:
            self.app.start_scan()
            pump(self.root, lambda: not self.app.busy)
        run.assert_not_called()
        generator.output_on.assert_not_called()
        meter.close.assert_called_once_with()
        self.assert_meter_requires_reconnect()

    def test_invalid_explicit_optical_cutoff_blocks_scan_before_rf_commands(self):
        generator,meter = self.prepare_mock_scan(Mock(return_value=0))
        self.app.vars['power_stop'].set('44')
        with patch.object(app_module,'run_scan') as run:
            self.app.start_scan()
        run.assert_not_called()
        generator.output_on.assert_not_called()
        self.assertIn('光功率关断值',self.app.notice.get())

    def test_blank_optical_cutoff_uses_existing_automatic_guard(self):
        self.prepare_mock_scan(Mock(return_value=0))
        self.app.vars['power_stop'].set('')
        def run_mock(config,*args,**kwargs):
            self.assertEqual(config.max_mvpp,300)
            self.assertIsNone(config.power_stop_mw)
            self.assertEqual(config.power_limit_mw,49.5)
            return []
        with patch.object(app_module,'run_scan',side_effect=run_mock) as run:
            self.app.start_scan()
            pump(self.root,lambda:not self.app.busy)
        run.assert_called_once()

    def test_running_usb_error_closes_rf_and_preserves_completed_csv_rows(self):
        _, meter = self.prepare_mock_scan(Mock(return_value=0.25))

        def response(amplitude, frequency, read):
            if frequency is None:
                return 0.25
            if frequency == 101e6:
                raise RuntimeError("TLPMX measPower: -1073806848: USB control transfer stalled")
            return 45

        generator = ResponseRig(response)
        generator.identity = "MOCK,DG5352 Pro,TEST"
        meter.read_mw = generator.read_mw
        self.app.generator = generator

        def fail_after_point(config, actual_generator, actual_meter, stop, on_point, *callbacks, on_sample=None):
            return run_real_scan(replace(config, settle_s=0), actual_generator, actual_meter,
                                 stop, on_point, *callbacks, on_sample=on_sample)

        with patch.object(app_module, "run_scan", side_effect=fail_after_point):
            self.app.start_scan()
            pump(self.root, lambda: not self.app.busy)
        self.assertEqual(generator.events[-1], ("off",))
        self.assertIn(("on",), generator.events)
        self.assertFalse(generator.enabled)
        meter.close.assert_called_once_with()
        self.assert_meter_requires_reconnect()
        with self.app.csv_path.open(encoding="utf-8-sig", newline="") as handle:
            row, = list(csv.DictReader(handle))
        self.assertEqual(float(row["frequency_mhz"]), 100)
        self.assertEqual(float(row["amplitude_mvpp"]), 220)
        self.assertIn("-1073806848", self.app.notice.get())

    def test_connect_read_failure_never_marks_meter_connected(self):
        self.refresh([METER_A, dict(METER_B, available=False)])
        self.choose(METER_B["serial"])
        generator = SimpleNamespace(output_off=Mock(), output_on=Mock(), close=Mock())
        meter = SimpleNamespace(
            identity={"model": METER_B["model"], "serial": METER_B["serial"], "wavelength_nm": 556.0},
            read_mw=Mock(side_effect=RuntimeError("USB control transfer stalled")), close=Mock(),
        )
        self.connector.side_effect = None
        self.connector.return_value = generator, meter
        self.app.connect_devices()
        pump(self.root, lambda: not self.app.busy)
        generator.output_on.assert_not_called()
        self.assert_meter_requires_reconnect()
        self.assertIn("重新连接", self.app.device_state.get())
        self.assertIn("stalled", self.app.notice.get())

    def test_cleanup_failure_preserves_original_rf_off_error_and_displays_note(self):
        _, meter = self.prepare_mock_scan(Mock(return_value=0.25))
        meter.close.side_effect = RuntimeError("meter close failed")
        original_error = RFOffError("RF shutdown verification failed")
        received_errors = []
        finish = self.app._finish

        def record_finish(operation, result, error):
            if error is not None:
                received_errors.append(error)
            return finish(operation, result, error)

        with (
            patch.object(app_module, "run_scan", side_effect=original_error),
            patch.object(self.app, "_finish", side_effect=record_finish),
        ):
            self.app.start_scan()
            pump(self.root, lambda: not self.app.busy)
        self.assertEqual(received_errors, [original_error])
        self.assertIsInstance(received_errors[0], RFOffError)
        self.assertIn("RF shutdown verification failed", self.app.notice.get())
        self.assertIn("meter close failed", self.app.notice.get())
        self.assert_meter_requires_reconnect()


class AdapterSelectionTests(unittest.TestCase):
    def setUp(self):
        self.generator = SimpleNamespace(output_off=Mock(), close=Mock())
        self.meter = SimpleNamespace(close=Mock())
        self.discovery = Mock(return_value=[dict(METER_A), dict(METER_B)])
        self.discover_patch = patch.object(instruments, "discover_power_meters", self.discovery)
        self.generator_patch = patch.object(instruments, "VerifiedRigol", return_value=self.generator)
        self.meter_patch = patch.object(instruments, "VerifiedPowerMeter", return_value=self.meter)
        self.discover_patch.start()
        self.open_generator = self.generator_patch.start()
        self.open_meter = self.meter_patch.start()

    def tearDown(self):
        self.discover_patch.stop()
        self.generator_patch.stop()
        self.meter_patch.stop()

    def connect(self, serial=""):
        return instruments.connect(False, "USB::RIGOL", serial, 556, lambda _: None)

    def test_blank_selection_resolves_one_available_meter(self):
        self.discovery.return_value = [dict(METER_A), dict(METER_B, available=False)]
        generator, meter = self.connect()
        self.assertIs(generator, self.generator)
        self.assertIs(meter, self.meter)
        self.open_meter.assert_called_once_with(METER_A["serial"], 556)

    def test_blank_selection_with_two_available_meters_requires_a_choice(self):
        with self.assertRaises(ValueError):
            self.connect()
        self.open_meter.assert_not_called()

    def test_explicit_second_meter_is_not_replaced_by_the_first(self):
        self.connect(METER_B["serial"])
        self.open_meter.assert_called_once_with(METER_B["serial"], 556)

    def test_missing_explicit_serial_is_rejected(self):
        self.discovery.return_value = [dict(METER_A), dict(METER_B, available=False)]
        with self.assertRaises(ValueError):
            self.connect("DISCONNECTED")
        self.open_meter.assert_not_called()

    def test_explicit_known_serial_is_opened_despite_sdk_unavailable_flag(self):
        self.discovery.return_value = [dict(METER_A), dict(METER_B, available=False)]
        generator, meter = self.connect(METER_B["serial"])
        self.assertIs(generator, self.generator)
        self.assertIs(meter, self.meter)
        self.open_meter.assert_called_once_with(METER_B["serial"], 556)

    def test_actual_open_error_is_preserved_and_generator_is_closed(self):
        self.discovery.return_value = [dict(METER_A), dict(METER_B, available=False)]
        original = RuntimeError("TLPMX init: -1073806848: USB control transfer stalled")
        self.open_meter.side_effect = original
        with self.assertRaises(RuntimeError) as caught:
            self.connect(METER_B["serial"])
        self.assertIs(caught.exception, original)
        self.open_meter.assert_called_once_with(METER_B["serial"], 556)
        self.generator.close.assert_called_once_with()

    def test_no_available_meter_is_rejected(self):
        self.discovery.return_value = [dict(METER_A, available=False), dict(METER_B, available=False)]
        with self.assertRaises(ValueError):
            self.connect()
        self.open_meter.assert_not_called()

    def test_device_disappears_between_selection_and_connect(self):
        self.discovery.return_value = [dict(METER_A)]
        with self.assertRaises(ValueError):
            self.connect(METER_B["serial"])
        self.open_meter.assert_not_called()

    def test_duplicate_serial_does_not_pick_an_arbitrary_device(self):
        duplicate = dict(METER_B, serial=METER_A["serial"])
        self.discovery.return_value = [dict(METER_A), duplicate]
        with self.assertRaises(ValueError):
            self.connect(METER_A["serial"])
        self.open_meter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
