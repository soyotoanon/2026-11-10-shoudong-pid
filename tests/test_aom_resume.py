"""Completed-point recovery and downward corrections; no instrument access."""
from dataclasses import replace
from pathlib import Path
import sys
from threading import Event
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scan import ScanConfig, ScanPoint, _Trial, run_scan
from app import plot_limits
from test_aom_refinement import ResponseRig


class ResumeTests(unittest.TestCase):
    def config(self,**changes):
        return replace(ScanConfig(100,104,1,50,1,0,350,settle_s=0,initial_mvpp=230,
                                  amplitude_step_mvpp=2,adaptive_search=True,max_step_mvpp=15,power_stop_mw=60),**changes)

    def test_resume_skips_completed_points_including_holes_and_carries_previous_amplitude(self):
        completed=[ScanPoint(100,231,49.5,0,'ok',2),ScanPoint(102,232,49.5,0,'ok',2)]
        rig=ResponseRig(lambda a,f,n:49.5)
        callbacks=[]
        result=run_scan(self.config(),rig,rig,Event(),callbacks.append,completed_points=completed)
        self.assertEqual([p.frequency_mhz for p in result],[100,101,102,103,104])
        self.assertEqual([p.frequency_mhz for p in callbacks],[101,103,104])
        self.assertEqual([e[1] for e in rig.events if e[0]=='frequency'],[101e6,103e6,104e6])
        writes=[e for e in rig.events if e[0]=='amplitude']
        self.assertEqual(next(e[2] for e in writes if e[1]==101e6),231)
        self.assertEqual(next(e[2] for e in writes if e[1]==103e6),232)
        self.assertFalse(rig.enabled)

    def test_completed_scan_touches_neither_rf_nor_meter(self):
        rig=ResponseRig(lambda a,f,n:self.fail('meter must not be touched'))
        complete=[ScanPoint(f,230,49.5,0,'ok',2) for f in self.config().frequencies_mhz()]
        result=run_scan(self.config(),rig,rig,Event(),self.fail,completed_points=complete)
        self.assertEqual(result,complete)
        self.assertEqual(rig.events,[])

    def test_unsafe_and_malformed_recovery_stops_before_any_hardware_action(self):
        for points in [[ScanPoint(99,230,49,0,'ok',2)],
                       [ScanPoint(100,400,49,0,'ok',2)],
                       [ScanPoint(100,230,60,0,'overrange',2)],
                       [ScanPoint(100,230,49,0,'ok',2)]*2]:
            rig=ResponseRig(lambda a,f,n:49)
            with self.assertRaises(ValueError):
                run_scan(self.config(),rig,rig,Event(),self.fail,completed_points=points)
            self.assertEqual(rig.events,[])

    def test_stable_power_above_target_is_corrected_down_even_inside_tolerance(self):
        rig=ResponseRig(lambda a,f,n:50.7+.5*(a-230))
        samples=[]
        p,=run_scan(self.config(stop_mhz=100),rig,rig,Event(),lambda _:None,on_sample=samples.append)
        self.assertEqual(p.status,'ok')
        self.assertTrue(49<=p.power_mw<=50)
        self.assertLess(p.amplitude_mvpp,230)
        self.assertTrue(any(s['control_phase']=='downward_predict' for s in samples))

    def test_overshoot_returns_to_measured_lower_target_amplitude_not_midpoint(self):
        calls=[]
        initial=[49,45,45,47,53]
        def acquire(config,frequency,amplitude,generator,meter,stop,phase,on_sample):
            calls.append((amplitude,phase))
            return _Trial(amplitude, initial[len(calls)-1] if len(calls)<=len(initial) else 49.5,.01)
        rig=ResponseRig(lambda a,f,n:0)
        with patch('scan._acquire',side_effect=acquire):
            p,=run_scan(self.config(stop_mhz=100),rig,rig,Event(),lambda _:None)
        self.assertEqual(p.status,'ok')
        self.assertIn((230,'return_to_target'),calls)
        self.assertEqual(p.amplitude_mvpp,230)

    def test_zoom_uses_fifty_mvpp_margin_and_overview_keeps_hard_limit(self):
        points=[ScanPoint(100,220,49,0,'ok',2),ScanPoint(101,250,49,0,'ok',2)]
        self.assertEqual(plot_limits(points,self.config()),(0,350))
        self.assertEqual(plot_limits(points,self.config(),True),(170,300))
        points.append(ScanPoint(102,350,44,0,'maximum_below_target',2))
        self.assertEqual(plot_limits(points,self.config(),True),(170,400))


if __name__=='__main__':unittest.main()
