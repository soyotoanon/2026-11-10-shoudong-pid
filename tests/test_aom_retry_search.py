"""Noise recovery, continued searching and 15 mVpp adjustment limits."""
from dataclasses import replace
from pathlib import Path
import sys
from threading import Event
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scan import ScanConfig, ScanStopped, _Trial, run_scan
from test_aom_refinement import ResponseRig


class RetrySearchTests(unittest.TestCase):
    def config(self,**changes):
        return replace(ScanConfig(130,130,1,50,2,0,350,settle_s=0,initial_mvpp=230,
                                  amplitude_step_mvpp=2,adaptive_search=True,max_step_mvpp=15,
                                  power_stop_mw=60),**changes)

    def run_scripted(self,acquire,**changes):
        rig = ResponseRig(lambda a,f,n:0)
        with patch('scan._acquire',side_effect=acquire):
            points = run_scan(self.config(**changes),rig,rig,Event(),lambda _:None)
        self.assertFalse(rig.enabled)
        return points[0],rig

    def test_noisy_window_repeats_at_same_amplitude_then_succeeds(self):
        windows = [_Trial(230,49,1,False),_Trial(230,49,.1),_Trial(230,49.3,.1)]
        point,rig = self.run_scripted(windows)
        self.assertEqual(point.status,'ok')
        self.assertEqual(point.trials,3)
        self.assertEqual({e[2] for e in rig.events if e[0]=='amplitude'},{230})

    def test_persistent_noise_stops_after_three_windows_without_raising_amplitude(self):
        def acquire(config,frequency,amplitude,*args):return _Trial(amplitude,45,3,False)
        point,rig = self.run_scripted(acquire)
        self.assertEqual(point.status,'unstable')
        self.assertEqual(point.trials,3)
        self.assertEqual({e[2] for e in rig.events if e[0]=='amplitude'},{230})

    def test_single_outside_repeat_is_remeasured_before_changing_amplitude(self):
        windows = [_Trial(230,p,.1) for p in [50,47.8,49.9,49.8]]
        point,rig = self.run_scripted(windows)
        self.assertEqual(point.status,'ok')
        self.assertEqual(point.trials,4)
        self.assertEqual({e[2] for e in rig.events if e[0]=='amplitude'},{230})

    def test_confirmed_offset_resumes_search_in_both_directions(self):
        for offset in [-4,4]:
            calls=[]
            def acquire(config,frequency,amplitude,generator,meter,stop,phase,on_sample):
                calls.append((amplitude,phase))
                power=50 if len(calls)==1 else 50+offset+(amplitude-230)
                return _Trial(amplitude,power,.1)
            with self.subTest(offset=offset):
                point,rig=self.run_scripted(acquire)
                self.assertEqual(point.status,'ok')
                self.assertTrue(48<=point.power_mw<=52)
                self.assertEqual([a for a,p in calls[:3]],[230,230,230])
                self.assertTrue(point.amplitude_mvpp>230 if offset<0 else point.amplitude_mvpp<230)

    def test_plateau_with_light_continues_search_until_later_rise(self):
        rig=ResponseRig(lambda a,f,n:43 if a<=245 else 43+.5*(a-245))
        point,=run_scan(self.config(),rig,rig,Event(),lambda _:None)
        self.assertEqual(point.status,'ok')
        self.assertGreater(point.amplitude_mvpp,245)
        self.assertTrue(48<=point.power_mw<=52)

    def test_flat_nonzero_signal_searches_to_rf_limit_instead_of_three_step_failure(self):
        rig=ResponseRig(lambda a,f,n:43)
        point,=run_scan(self.config(initial_mvpp=310),rig,rig,Event(),lambda _:None)
        self.assertEqual(point.status,'maximum_below_target')
        self.assertEqual(point.maximum_kind,'rf_limit')
        self.assertIn('RF 振幅上限 350',point.reason)
        self.assertEqual(max(e[2] for e in rig.events if e[0]=='amplitude'),350)

    def test_transient_power_dip_recovers_then_continues(self):
        calls=[]
        def acquire(config,frequency,amplitude,generator,meter,stop,phase,on_sample):
            calls.append((amplitude,phase))
            power=30 if len(calls)==2 else 40+.5*(amplitude-230)
            return _Trial(amplitude,power,.05)
        point,rig=self.run_scripted(acquire)
        self.assertEqual(point.status,'ok')
        self.assertTrue(any(p=='rollover_check' for a,p in calls))
        self.assertEqual(calls[1][0],calls[2][0])

    def test_fifteen_mvpp_limit_is_used_and_never_exceeded(self):
        rig=ResponseRig(lambda a,f,n:max(.001,50+.25*(a-340)))
        config=self.config(initial_mvpp=170)
        point,=run_scan(config,rig,rig,Event(),lambda _:None)
        values=[e[2] for e in rig.events if e[0]=='amplitude']
        deltas=[abs(b-a) for a,b in zip(values,values[1:])]
        self.assertEqual(point.status,'ok')
        self.assertGreater(max(deltas),10)
        self.assertLessEqual(max(deltas),15.000001)
        self.assertLessEqual(max(values),350)

    def test_noisy_rising_curve_converges_without_tiny_step_stalling(self):
        rig=ResponseRig(lambda a,f,n:50+.3*(a-330)+(.25 if n%2 else -.25))
        point,=run_scan(self.config(),rig,rig,Event(),lambda _:None)
        self.assertEqual(point.status,'ok')
        self.assertLessEqual(point.trials,22)
        self.assertTrue(48<=point.power_mw<=52)

    def test_noise_does_not_hide_cancel_or_usb_exception(self):
        for error in [ScanStopped('stop'),RuntimeError('USB failed')]:
            rig=ResponseRig(lambda a,f,n:0)
            with patch('scan._acquire',side_effect=[_Trial(230,45,3,False),error]):
                with self.assertRaises(type(error)):
                    run_scan(self.config(),rig,rig,Event(),lambda _:None)
            self.assertFalse(rig.enabled)

if __name__=='__main__':unittest.main()
