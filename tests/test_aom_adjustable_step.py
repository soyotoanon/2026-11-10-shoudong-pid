from dataclasses import replace
from pathlib import Path
import sys
from threading import Event
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scan import ScanConfig, run_scan
from test_aom_refinement import ResponseRig

class AdjustableStepTests(unittest.TestCase):
    def config(self, **values):
        return replace(ScanConfig(130,130,.5,45,4,0,350,settle_s=0,initial_mvpp=220,
                                  amplitude_step_mvpp=10),**values)

    def test_selected_step_changes_actual_adjustment_and_refines_at_target(self):
        histories=[]
        for step in (2,10):
            rig=ResponseRig(lambda a,f,n:a*.15)
            points=run_scan(self.config(amplitude_step_mvpp=step),rig,rig,Event(),lambda _:None)
            writes=[e[2] for e in rig.events if e[0]=='amplitude']
            writes=[v for i,v in enumerate(writes) if i==0 or v!=writes[i-1]]
            self.assertEqual(points[0].status,'ok')
            self.assertAlmostEqual(writes[1]-writes[0],step)
            self.assertTrue(all(abs(b-a)<=step+1e-8 for a,b in zip(writes,writes[1:])))
            self.assertTrue(all(0<a<=350 for a in writes))
            histories.append(writes)
        self.assertLess(len(histories[1]),len(histories[0]))

    def test_live_changes_take_effect_after_current_measurement(self):
        current=[10]
        trials=[]
        rig=ResponseRig(lambda a,f,n:a*.15)
        def on_trial(frequency,amplitude,power):
            trials.append(amplitude)
            if len(trials)==1:current[0]=2
            elif len(trials)==2:current[0]=20
        points=run_scan(self.config(amplitude_step_source=lambda:current[0]),rig,rig,Event(),lambda _:None,on_trial)
        self.assertEqual(trials[:3],[220,222,242])
        self.assertEqual(points[0].status,'ok')

    def test_step_reduction_also_limits_refinement_jumps(self):
        current=[10]
        rig=ResponseRig(lambda a,f,n:a*.1)
        def on_trial(frequency,amplitude,power):
            if amplitude==260:current[0]=.5
        config=self.config(target_mw=25.7,tolerance_mw=2,initial_mvpp=250,
                           amplitude_step_source=lambda:current[0])
        points=run_scan(config,rig,rig,Event(),lambda _:None,on_trial)
        writes=[e[2] for e in rig.events if e[0]=='amplitude']
        after=writes[writes.index(260):]
        self.assertTrue(all(abs(b-a)<=.5000001 for a,b in zip(after,after[1:])))
        self.assertEqual(points[0].status,'ok')

    def test_bad_values_are_rejected_and_live_bad_value_never_enables_rf(self):
        for step in (0,-1,float('nan'),float('inf'),351):
            with self.assertRaises(ValueError):self.config(amplitude_step_mvpp=step)
        rig=ResponseRig(lambda a,f,n:0)
        with self.assertRaises(ValueError):
            run_scan(self.config(amplitude_step_source=lambda:float('nan')),rig,rig,Event(),lambda _:None)
        self.assertNotIn(('on',),rig.events)

    def test_large_step_cannot_exceed_rf_ceiling(self):
        rig=ResponseRig(lambda a,f,n:a*.001)
        points=run_scan(self.config(amplitude_step_mvpp=100,max_mvpp=230),rig,rig,Event(),lambda _:None)
        self.assertTrue(all(e[2]<=230 for e in rig.events if e[0]=='amplitude'))
        self.assertEqual(points[0].status,'unreachable')
        self.assertFalse(rig.enabled)

if __name__=='__main__':unittest.main()
