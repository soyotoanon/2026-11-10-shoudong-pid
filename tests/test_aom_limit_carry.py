"""Neighbor ceiling seeds must require measured rise, and preserve protection."""
from dataclasses import asdict, replace
from pathlib import Path
import sys
from threading import Event
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scan import ScanConfig, ScanPoint, _neighbor_start, run_scan
from sessions import point_from_row
from test_aom_refinement import ResponseRig

class LimitCarryTests(unittest.TestCase):
    def config(self, **changes):
        return replace(ScanConfig(130,132,1,50,1,0,350,settle_s=0,initial_mvpp=230,
            amplitude_step_mvpp=2,adaptive_search=True,max_step_mvpp=15,power_stop_mw=60),**changes)

    def test_three_ascending_unreachable_frequencies_start_230_300_300(self):
        rig=ResponseRig(lambda a,f,n:.1*a)
        points=run_scan(self.config(),rig,rig,Event(),lambda _:None)
        self.assertTrue(all(p.rising_to_limit and p.maximum_kind=='rf_limit' for p in points))
        starts=[next(e[2] for e in rig.events if e[0]=='amplitude' and e[1]==f*1e6) for f in (130,131,132)]
        self.assertEqual(starts,[230,300,300])
        for f in (130,131,132):
            writes=[e[2] for e in rig.events if e[0]=='amplitude' and e[1]==f*1e6]
            self.assertLessEqual(max(writes),350)
            self.assertLessEqual(max(abs(b-a) for a,b in zip(writes,writes[1:])),15.00001)
        self.assertLess(points[1].trials,points[0].trials)
        self.assertFalse(rig.enabled)

    def test_flat_ceiling_does_not_qualify_for_high_start(self):
        rig=ResponseRig(lambda a,f,n:35)
        points=run_scan(self.config(stop_mhz=131),rig,rig,Event(),lambda _:None)
        self.assertFalse(any(p.rising_to_limit for p in points))
        first=next(e[2] for e in rig.events if e[0]=='amplitude' and e[1]==131e6)
        self.assertEqual(first,230)

    def test_resume_retains_certified_ceiling_seed(self):
        rig=ResponseRig(lambda a,f,n:.1*a)
        prior,=run_scan(self.config(stop_mhz=130),rig,rig,Event(),lambda _:None)
        row={k:str(v) if v is not None else '' for k,v in asdict(prior).items()}
        row['output_power_mw']=row.pop('power_mw');row['power_std_mw']=row.pop('std_mw')
        loaded=point_from_row(row)
        self.assertEqual(loaded,prior)
        rig=ResponseRig(lambda a,f,n:.1*a)
        run_scan(self.config(stop_mhz=131),rig,rig,Event(),lambda _:None,completed_points=[loaded])
        self.assertEqual(next(e[2] for e in rig.events if e[0]=='amplitude'),300)
        row.pop('rising_to_limit')
        self.assertFalse(point_from_row(row).rising_to_limit)

    def test_legacy_or_unsafe_or_other_limit_does_not_reuse_seed(self):
        prior=ScanPoint(130,350,35,0,'maximum_below_target',12,maximum_kind='rf_limit',rising_to_limit=True)
        for point in [replace(prior,rising_to_limit=False),replace(prior,status='maximum_unconfirmed'),
                      replace(prior,maximum_kind='local_peak'),replace(prior,power_mw=49.5),replace(prior,amplitude_mvpp=340)]:
            self.assertIsNone(_neighbor_start(point,self.config())[0])
        self.assertIsNone(_neighbor_start(prior,self.config(max_mvpp=340))[0])
        self.assertIsNone(_neighbor_start(prior,self.config(adaptive_search=False))[0])

    def test_next_frequency_can_correct_downward_from_300(self):
        rig=ResponseRig(lambda a,f,n:.1*a if f==130e6 else 49.5+.2*(a-280))
        points=run_scan(self.config(stop_mhz=131),rig,rig,Event(),lambda _:None)
        self.assertEqual(points[1].status,'ok')
        self.assertLess(points[1].amplitude_mvpp,300)
        self.assertTrue(49<=points[1].power_mw<=50)
        self.assertFalse(rig.enabled)

    def test_optical_guard_still_stops_high_neighbor(self):
        rig=ResponseRig(lambda a,f,n:.1*a if f==130e6 else 65)
        points=run_scan(self.config(),rig,rig,Event(),lambda _:None)
        self.assertEqual(len(points),2)
        self.assertEqual(points[-1].status,'overrange')
        self.assertFalse(rig.enabled)

if __name__=='__main__':unittest.main()
