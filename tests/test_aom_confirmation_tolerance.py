"""Replay the actual 142/144 MHz samples from the 17:52:02 measurement."""
from pathlib import Path
from statistics import fmean, stdev
import sys
from threading import Event
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scan import ScanConfig, _Trial, _confirmation_failure, run_scan
from test_aom_refinement import ResponseRig

SAMPLES = [
    (142,226.9607,
     [50.5315,50.5294,49.5584,49.5607,50.4979,50.4927],
     [50.1143,50.1058,50.302,50.3088,50.5729,50.5642]),
    (144,226.225,
     [50.1262,50.1328,49.5623,49.5785,50.064,50.0712],
     [49.7339,49.6999,49.803,49.8267,49.4827,49.4804]),
]

class ConfirmationToleranceTests(unittest.TestCase):
    def config(self,frequency=142,amplitude=226.9607):
        return ScanConfig(frequency,frequency,1,50,5,0,350,settle_s=0,initial_mvpp=amplitude)

    def test_actual_repeated_measurements_within_five_mw_are_accepted(self):
        for frequency,amplitude,candidate,repeat in SAMPLES:
            with self.subTest(frequency=frequency):
                trials=[_Trial(amplitude,fmean(values),stdev(values),True,len(values)) for values in (candidate,repeat)]
                self.assertGreater(abs(trials[1].power-50),.25)
                rig=ResponseRig(lambda a,f,n:50)
                with patch('scan._acquire',side_effect=trials) as acquire:
                    points=run_scan(self.config(frequency,amplitude),rig,rig,Event(),lambda _:None)
                self.assertEqual(points[0].status,'ok')
                self.assertAlmostEqual(points[0].amplitude_mvpp,amplitude)
                self.assertAlmostEqual(points[0].power_mw,fmean(repeat))
                self.assertEqual(acquire.call_count,2)
                self.assertFalse(rig.enabled)

    def test_outside_user_tolerance_is_rejected_with_numeric_reason(self):
        config=self.config()
        reason=_confirmation_failure(config,_Trial(226,50,.1),_Trial(226,55.2,.1))
        self.assertIn('55.2',reason)
        self.assertIn('5 mW',reason)

    def test_scatter_and_between_window_drift_remain_checked(self):
        config=self.config()
        self.assertIsNotNone(_confirmation_failure(config,_Trial(226,50,.1),_Trial(226,54.9,.2)))
        self.assertIsNotNone(_confirmation_failure(config,_Trial(226,46,.1),_Trial(226,54,.1)))
        self.assertIsNotNone(_confirmation_failure(config,_Trial(226,50,.1),_Trial(226,50,.1,False)))

if __name__=='__main__':unittest.main()
