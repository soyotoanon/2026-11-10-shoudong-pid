"""Bounded-maximum evidence and target-search performance; no hardware access."""
from dataclasses import replace
from pathlib import Path
import math
import sys
from threading import Event
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import scan
from scan import ScanConfig, ScanStopped, _Trial, run_scan
from test_aom_refinement import ResponseRig


class MaximumTests(unittest.TestCase):
    def config(self, **changes):
        return replace(ScanConfig(130,130,1,50,2,0,350,settle_s=0,initial_mvpp=230,
                                  amplitude_step_mvpp=2,adaptive_search=True,max_step_mvpp=15,
                                  power_stop_mw=60), **changes)

    def run_case(self, response, **changes):
        rig=ResponseRig(response)
        samples=[]
        points=run_scan(self.config(**changes),rig,rig,Event(),lambda _:None,on_sample=samples.append)
        self.assertFalse(rig.enabled)
        values=[e[2] for e in rig.events if e[0]=='amplitude']
        self.assertLessEqual(max(values),350)
        if len(points)==1:
            self.assertLessEqual(max(abs(b-a) for a,b in zip(values,values[1:])),15.000001)
        return points,rig,samples

    def test_refined_peak_has_paired_amplitude_power_and_both_flanks(self):
        response=lambda a,f,n:max(.001,44-.004*(a-308)**2)
        points,rig,samples=self.run_case(response)
        p=points[0]
        self.assertEqual((p.status,p.maximum_kind),('maximum_below_target','local_peak'))
        self.assertAlmostEqual(p.amplitude_mvpp,308,delta=1)
        self.assertAlmostEqual(p.power_mw,response(p.amplitude_mvpp,0,0),places=7)
        self.assertLessEqual(p.peak_bracket_high_mvpp-p.peak_bracket_low_mvpp,2)
        self.assertLess(p.tested_max_mvpp,350)
        rollover=next(s['amplitude_mvpp'] for s in samples if s['control_phase']=='rollover_check')
        self.assertLessEqual(p.tested_max_mvpp,rollover)
        checked={s['amplitude_mvpp'] for s in samples if s['control_phase']=='peak_check'}
        self.assertGreaterEqual(len(checked),3)
        self.assertIn('本次已测响应分支',p.reason)

    def test_true_target_is_preferred_over_fallback_maximum(self):
        points,_,_=self.run_case(lambda a,f,n:max(.001,56-.004*(a-308)**2))
        self.assertEqual(points[0].status,'ok')
        self.assertEqual(points[0].maximum_kind,'')
        self.assertTrue(48<=points[0].power_mw<=52)

    def test_peak_refinement_accepts_quantized_hardware_amplitude_readback(self):
        class RoundedRig(ResponseRig):
            def set_amplitude_mvpp(self, amplitude):
                return super().set_amplitude_mvpp(round(amplitude,4))
        response=lambda a,f,n:max(.001,44-.004*(a-308)**2)
        rig=RoundedRig(response)
        p,=run_scan(self.config(),rig,rig,Event(),lambda _:None)
        self.assertFalse(rig.enabled)
        self.assertEqual(p.maximum_kind,'local_peak')
        self.assertEqual(p.amplitude_mvpp,round(p.amplitude_mvpp,4))
        self.assertAlmostEqual(p.power_mw,response(p.amplitude_mvpp,0,0),places=8)

    def test_refinement_discovers_previously_missed_reachable_target(self):
        points,_,samples=self.run_case(lambda a,f,n:max(.001,44-.004*(a-260)**2+7*math.exp(-((a-260)/1.5)**2)))
        p=points[0]
        self.assertEqual(p.status,'ok')
        self.assertTrue(48<=p.power_mw<=52)
        # Also verifies that target seeking, not a false below-target label, wins.
        self.assertEqual(p.maximum_kind,'')
        self.assertTrue(any(s['control_phase']=='peak_refine' for s in samples))

    def test_starting_on_descending_side_searches_down_to_peak(self):
        points,_,_=self.run_case(lambda a,f,n:max(.001,44-.004*(a-308)**2),initial_mvpp=320)
        p=points[0]
        self.assertEqual(p.maximum_kind,'local_peak')
        self.assertAlmostEqual(p.amplitude_mvpp,308,delta=1)

    def test_voltage_ceiling_is_labelled_as_constrained_not_optical_peak(self):
        points,_,_=self.run_case(lambda a,f,n:a*.1)
        p=points[0]
        self.assertEqual((p.maximum_kind,p.amplitude_mvpp,p.power_mw),('rf_limit',350,35))
        self.assertEqual(p.tested_min_mvpp,230)
        self.assertIn('不代表超过上限',p.reason)

    def test_one_boundary_measurement_is_not_enough_to_claim_maximum(self):
        points,_,_=self.run_case(lambda a,f,n:35,initial_mvpp=350)
        self.assertEqual(points[0].status,'maximum_unconfirmed')
        self.assertEqual(points[0].maximum_kind,'')

    def test_dark_sensor_never_becomes_a_valid_zero_peak(self):
        points,_,_=self.run_case(lambda a,f,n:.00001)
        self.assertEqual(points[0].status,'no_bracket')
        self.assertEqual(points[0].maximum_kind,'')

    def test_iteration_budget_is_not_unreachability_evidence(self):
        with patch('scan._LOCAL_TRIALS',1):
            points,_,_=self.run_case(lambda a,f,n:a*.1)
        self.assertEqual(points[0].maximum_kind,'')
        self.assertIn('次数上限',points[0].reason)

    def test_drift_during_peak_recheck_rejects_maximum(self):
        rig=ResponseRig(lambda a,f,n:0)
        def acquire(config,frequency,amplitude,generator,meter,stop,phase,on_sample):
            power=44-.004*(amplitude-308)**2
            if phase=='peak_check':power-=3
            return _Trial(amplitude,power,.01)
        with patch('scan._acquire',side_effect=acquire):
            p,=run_scan(self.config(),rig,rig,Event(),lambda _:None)
        self.assertFalse(rig.enabled)
        self.assertEqual(p.status,'maximum_unconfirmed')
        self.assertEqual(p.maximum_kind,'')

    def test_uncertainty_overlaps_target_range_is_not_a_proven_maximum(self):
        points,_,_=self.run_case(lambda a,f,n:max(.001,47.95-.01*(a-280)**2))
        self.assertEqual(points[0].status,'maximum_unconfirmed')
        self.assertEqual(points[0].maximum_kind,'')

    def test_peak_recheck_cancel_and_usb_failure_propagate_with_rf_off(self):
        for error in [ScanStopped('stop'),RuntimeError('USB failure')]:
            rig=ResponseRig(lambda a,f,n:0)
            def acquire(config,frequency,amplitude,generator,meter,stop,phase,on_sample):
                if phase=='peak_check':raise error
                return _Trial(amplitude,44-.004*(amplitude-308)**2,.01)
            with patch('scan._acquire',side_effect=acquire):
                with self.assertRaises(type(error)):
                    run_scan(self.config(),rig,rig,Event(),lambda _:None)
            self.assertFalse(rig.enabled)

    def test_neighbor_reuses_successful_amplitude_without_forced_downward_ramp(self):
        points,rig,_=self.run_case(lambda a,f,n:49+.5*(a-230),stop_mhz=132)
        self.assertEqual([p.status for p in points],['ok']*3)
        self.assertEqual([p.trials for p in points],[2]*3)
        self.assertEqual({e[2] for e in rig.events if e[0]=='amplitude'},{230})

    def test_confirmed_peak_is_not_reused_as_next_frequency_target_seed(self):
        response=lambda a,f,n:max(.001,44-.004*(a-308)**2) if f==130e6 else 49+.1*(a-230)
        points,rig,_=self.run_case(response,stop_mhz=131)
        self.assertEqual([p.status for p in points],['maximum_below_target','ok'])
        first_next=next(e[2] for e in rig.events if e[0]=='amplitude' and e[1]==131e6)
        self.assertEqual(first_next,230)

    def test_slow_usb_time_counts_toward_three_second_settling(self):
        now=[0.0]
        guards=[]
        class Stop:
            def is_set(self):return False
            def wait(self, seconds):now[0]+=seconds;return False
        class Meter:
            def read_mw(self):now[0]+=.4;return 49.0
        def sample(s):
            if s['stage']=='guard':guards.append(now[0])
        rig=ResponseRig(lambda a,f,n:49)
        with patch('scan.time.monotonic',side_effect=lambda:now[0]):
            trial=scan._acquire(self.config(settle_s=3),130,230,rig,Meter(),Stop(),'initial',sample)
        self.assertTrue(trial.stable)
        self.assertEqual(len(guards),8)
        self.assertGreaterEqual(guards[-1],3)
        self.assertLess(guards[-1],3.5)


if __name__=='__main__':unittest.main()
