"""Ordinary sub-mW meter scatter must not abort a scan allowing +/-4 mW."""
from itertools import cycle
from pathlib import Path
import sys
from threading import Event
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scan import ScanConfig, _Trial, _on_target, run_scan
from test_aom_refinement import ResponseRig

class MeasurementNoiseTests(unittest.TestCase):
    def test_sub_mw_batch_noise_converges_and_passes_separate_confirmation(self):
        # Two adjacent readings share each batch offset, as observed on hardware.
        noise=cycle((-.3,-.3,0,0,.3,.3))
        rig=ResponseRig(lambda amp,freq,count:45+(amp-290)*.25+next(noise))
        config=ScanConfig(130,130,.5,45,4,0,350,settle_s=0,initial_mvpp=281)
        points=run_scan(config,rig,rig,Event(),lambda _:None)
        self.assertEqual(points[0].status,'ok')
        self.assertLessEqual(abs(points[0].power_mw-45),.225)
        self.assertGreater(points[0].std_mw,.15)
        self.assertLessEqual(abs(points[0].power_mw-45)+points[0].std_mw,4)
        self.assertFalse(rig.enabled)

    def test_noise_allowance_cannot_accept_mean_far_from_target(self):
        config=ScanConfig(130,130,.5,45,4,0,350,settle_s=0,initial_mvpp=281)
        self.assertFalse(_on_target(config,_Trial(281,38,.3)))
        self.assertFalse(_on_target(config,_Trial(281,45,5)))

    def test_excessive_batch_noise_still_stops_without_recording_amplitude(self):
        noise=cycle((-3,-3,0,0,3,3))
        rig=ResponseRig(lambda amp,freq,count:45+next(noise))
        config=ScanConfig(130,130,.5,45,4,0,350,settle_s=0,initial_mvpp=220)
        points=run_scan(config,rig,rig,Event(),lambda _:None)
        self.assertEqual(points[0].status,'unstable')
        self.assertIsNone(points[0].amplitude_mvpp)

if __name__=='__main__':unittest.main()
