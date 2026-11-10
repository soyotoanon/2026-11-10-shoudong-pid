"""Range switching must complete before RF, and cleanup must preserve errors."""
import ctypes as C
from pathlib import Path
import sys
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from instruments import VerifiedPowerMeter
from scan import ScanConfig, run_scan
from test_aom_refinement import ResponseRig

NAMES = ('getPowerAutorange', 'getPowerRange', 'setPowerAutoRange', 'setPowerRange')

class Driver:
    def __init__(self):
        self.auto = 1
        self.range = .0000045
        self.limit = .0897461
        self.fail_restore = False
        self.keep_auto = False
    def call(self, name, session, *args):
        if name == 'getPowerAutorange':
            C.cast(args[0], C.POINTER(C.c_uint16))[0] = self.auto
        elif name == 'getPowerRange':
            C.cast(args[1], C.POINTER(C.c_double))[0] = self.range
        elif name == 'setPowerRange':
            self.range = min(args[0], self.limit)
            if not self.keep_auto:self.auto = 0
        elif name == 'setPowerAutoRange':
            if self.fail_restore:raise RuntimeError('restore failed')
            self.auto = args[0]
        return 0

def meter_for(driver, read):
    meter = VerifiedPowerMeter.__new__(VerifiedPowerMeter)
    meter.session = C.c_uint32(1)
    meter.dll = SimpleNamespace(**{'TLPMX_'+name: Mock() for name in NAMES})
    meter.call = driver.call
    meter.read_mw = read
    return meter

class RangeTests(unittest.TestCase):
    def test_range_setup_precedes_rf_and_restores_original_after_rf_off(self):
        driver = Driver()
        rig = ResponseRig(lambda a,f,n: 45)
        def read():
            if rig.enabled and driver.auto:raise RuntimeError('unexpected autoranging with light')
            return 45
        meter = meter_for(driver, read)
        config = ScanConfig(130,130,.5,45,4,0,350,settle_s=0,initial_mvpp=220)
        points = run_scan(config,rig,meter,Event(),lambda _:None)
        self.assertEqual(points[0].status,'ok')
        self.assertFalse(rig.enabled)
        self.assertEqual(driver.auto,1)
        self.assertAlmostEqual(driver.range,.0000045)
        self.assertEqual(meter.scan_range_mw,53)

    def test_insufficient_range_stops_before_enabling_rf_and_restores_settings(self):
        driver=Driver(); driver.limit=.01
        rig=ResponseRig(lambda a,f,n:45)
        meter=meter_for(driver,lambda:45)
        config=ScanConfig(130,130,.5,45,4,0,350,settle_s=0,initial_mvpp=220)
        with self.assertRaisesRegex(RuntimeError,'固定量程'):
            run_scan(config,rig,meter,Event(),lambda _:None)
        self.assertNotIn(('on',),rig.events)
        self.assertEqual(driver.auto,1)

    def test_autorange_readback_must_really_be_disabled(self):
        driver=Driver(); driver.keep_auto=True
        meter=meter_for(driver,lambda:45)
        with self.assertRaisesRegex(RuntimeError,'固定量程'):
            meter.prepare_scan(53)
        meter.restore_scan()

    def test_read_error_is_preserved_when_range_restore_also_fails(self):
        driver=Driver(); driver.fail_restore=True
        rig=ResponseRig(lambda a,f,n:45)
        error=RuntimeError('first USB error')
        meter=meter_for(driver,Mock(side_effect=error))
        config=ScanConfig(130,130,.5,45,4,0,350,settle_s=0,initial_mvpp=220)
        with self.assertRaisesRegex(RuntimeError,'first USB error'):
            run_scan(config,rig,meter,Event(),lambda _:None)
        self.assertFalse(rig.enabled)
        self.assertIn('restore failed',' '.join(error.__notes__))

    def test_range_restore_failure_is_not_reported_as_a_successful_scan(self):
        driver=Driver(); driver.fail_restore=True
        rig=ResponseRig(lambda a,f,n:45)
        meter=meter_for(driver,lambda:45)
        config=ScanConfig(130,130,.5,45,4,0,350,settle_s=0,initial_mvpp=220)
        with self.assertRaisesRegex(RuntimeError,'量程恢复失败'):
            run_scan(config,rig,meter,Event(),lambda _:None)
        self.assertFalse(rig.enabled)

if __name__=='__main__':unittest.main()
