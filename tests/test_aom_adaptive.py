"""Offline plant-response tests for bounded adaptive search (no USB/RF access)."""
from dataclasses import replace
from pathlib import Path
import sys
from threading import Event
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scan import ScanConfig, run_scan
from test_aom_refinement import ResponseRig
from test_aom_timing import Rig, TracedEvent


class AdaptiveSearchTests(unittest.TestCase):
    def config(self, **changes):
        return replace(ScanConfig(130,130,1,50,2,0,350,settle_s=0,initial_mvpp=230,
                                  amplitude_step_mvpp=2,adaptive_search=True,max_step_mvpp=10,
                                  power_stop_mw=55), **changes)

    def run_case(self, response, **changes):
        rig = ResponseRig(response)
        samples = []
        points = run_scan(self.config(**changes),rig,rig,Event(),lambda p:None,on_sample=samples.append)
        self.assertFalse(rig.enabled)
        return points,rig,samples

    def writes(self, rig):
        values = [e[2] for e in rig.events if e[0]=='amplitude']
        return [v for i,v in enumerate(values) if not i or v!=values[i-1]]

    def test_far_target_converges_faster_than_fixed_two_mv_steps(self):
        response = lambda a,f,n:max(.0001,50+.3*(a-330))
        points,rig,_ = self.run_case(response)
        fixed,_,_ = self.run_case(response,adaptive_search=False)
        self.assertEqual(points[0].status,'ok')
        self.assertLessEqual(points[0].trials,.6*fixed[0].trials)
        self.assertTrue(48<=points[0].power_mw<=50)
        values = self.writes(rig)
        self.assertEqual(values[:3],[230,232,234])
        self.assertTrue(any(b-a>2.1 for a,b in zip(values,values[1:])))
        self.assertTrue(all(abs(b-a)<=10.000001 for a,b in zip(values,values[1:])))
        self.assertTrue(all(e[3]<=50 for e in rig.events if e[0]=='read'))

    def test_in_range_finishes_without_chasing_internal_quarter_mw_goal(self):
        points,rig,_ = self.run_case(lambda a,f,n:49)
        self.assertEqual(points[0].status,'ok')
        self.assertEqual(points[0].trials,2)
        self.assertEqual(self.writes(rig),[230])

    def test_measured_bracket_is_refined_without_using_voltage_ceiling(self):
        points,rig,samples = self.run_case(lambda a,f,n:50+a-227.3,tolerance_mw=.1)
        self.assertEqual(points[0].status,'ok')
        self.assertTrue(any(s['phase']=='refine' for s in samples))
        self.assertLessEqual(max(self.writes(rig)),230)
        self.assertAlmostEqual(points[0].power_mw,50,delta=.1)

    def test_power_rollover_does_not_continue_toward_ceiling(self):
        points,rig,_ = self.run_case(lambda a,f,n:max(.0001,44-.004*(a-308)**2))
        self.assertEqual(points[0].status,'maximum_below_target')
        self.assertEqual(points[0].maximum_kind,'local_peak')
        self.assertLess(max(self.writes(rig)),350)
        self.assertTrue(all(e[3]<50 for e in rig.events if e[0]=='read'))

    def test_dark_or_flat_response_stops_without_accelerating(self):
        points,rig,_ = self.run_case(lambda a,f,n:.0001)
        self.assertEqual(points[0].status,'no_bracket')
        self.assertEqual(self.writes(rig),[230,232,234,236])

    def test_steep_measured_response_reduces_steps_before_target(self):
        points,rig,_ = self.run_case(lambda a,f,n:35+2*(a-230))
        self.assertEqual(points[0].status,'ok')
        self.assertTrue(all(e[3]<=50 for e in rig.events if e[0]=='read'))
        self.assertTrue(any(0<b-a<1 for a,b in zip(self.writes(rig),self.writes(rig)[1:])))

    def test_unknown_abrupt_jump_trips_on_first_read_not_a_zero_overshoot_promise(self):
        points,rig,_ = self.run_case(lambda a,f,n:80 if a>232 else 20,stop_mhz=131)
        self.assertEqual([p.status for p in points],['overrange'])
        self.assertEqual(rig.read_counts[(130e6,234)],1)
        self.assertEqual(max(self.writes(rig)),234)
        self.assertNotIn(('frequency',131e6),rig.events)

    def test_hard_amplitude_limit_is_reported_explicitly(self):
        points,rig,_ = self.run_case(lambda a,f,n:a*.1)
        self.assertEqual(points[0].status,'maximum_below_target')
        self.assertEqual(points[0].maximum_kind,'rf_limit')
        self.assertEqual(max(self.writes(rig)),350)
        self.assertIn('RF 振幅上限 350',points[0].reason)

    def test_repeated_confirmation_drift_is_retried_but_bounded(self):
        points,rig,_ = self.run_case(lambda a,f,n:50 if n<=7 else 54)
        self.assertEqual(points[0].status,'unstable')
        self.assertGreater(len(self.writes(rig)),1)
        self.assertLessEqual(points[0].trials,16)
        self.assertIn('4 轮',points[0].reason)

    def test_explicit_power_stop_is_independent_of_tolerance(self):
        config = self.config(power_stop_mw=53,tolerance_mw=5)
        self.assertEqual(config.power_limit_mw,53)
        points,_,_ = self.run_case(lambda a,f,n:53,power_stop_mw=53,tolerance_mw=5)
        self.assertEqual(points[0].status,'overrange')
        for cutoff in [50,49,float('nan'),float('inf')]:
            with self.assertRaises(ValueError):self.config(power_stop_mw=cutoff)
        for step in [0,-1,float('nan'),float('inf')]:
            with self.assertRaises(ValueError):self.config(max_step_mvpp=step)

    def test_changes_wait_three_seconds_but_repeat_does_not_repeat_that_delay(self):
        rig = Rig()
        config = self.config(target_mw=4,tolerance_mw=.1,min_mvpp=40,max_mvpp=160,
                             initial_mvpp=40,power_stop_mw=5,settle_s=3)
        points = run_scan(config,rig,rig,TracedEvent(rig.events),lambda _:None,on_sample=rig.sample)
        self.assertEqual(points[0].status,'ok')
        first = rig.events.index(('telemetry','sample',1))
        self.assertEqual(sum(e[:2]==('telemetry','guard') for e in rig.events[:first]),30)
        total_wait = sum(e[1] for e in rig.events if e[0]=='wait')
        self.assertGreater(total_wait,3.9)
        self.assertLess(total_wait,4.1)
        self.assertEqual(rig.guard_reads,31)

if __name__=='__main__':unittest.main()
