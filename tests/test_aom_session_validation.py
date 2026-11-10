"""Reject incompatible or incomplete recovery records."""
import csv
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import CSV_FIELDS, POWER
from scan import ScanConfig
from sessions import load_measurements

class SessionValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.path=Path(self.temp.name)/'scan.csv'
        self.config=ScanConfig(130,140,1,50,1,0,350)
        self.settings=dict(wavelength='400',mode=POWER,incident='')
        self.row=dict(frequency_mhz=130,amplitude_mvpp=230,output_power_mw=49.5,power_std_mw=.1,
                      target_power_mw=50,tolerance_mw=1,wavelength_nm=400,status='ok',trials=2,
                      simulation=False,target_mode=POWER,meter_serial='TEST',generator='TESTGEN')
    def tearDown(self):self.temp.cleanup()
    def write(self):
        with self.path.open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=CSV_FIELDS);w.writeheader();w.writerow(self.row)
    def test_different_device_does_not_count_as_completed(self):
        self.write()
        for checks in [dict(meter_serial='OTHER'),dict(generator='OTHER')]:
            with self.assertRaises(ValueError):
                load_measurements([self.path],self.config,self.settings,False,**checks)
    def test_unknown_state_or_nonfinite_value_does_not_silently_skip_a_point(self):
        for field,value in [('status','unknown'),('amplitude_mvpp','nan'),('trials','inf')]:
            original=self.row[field];self.row[field]=value;self.write()
            with self.assertRaises(ValueError):load_measurements([self.path],self.config,self.settings,False)
            self.row[field]=original
    def test_truncated_row_and_simulated_record_are_rejected(self):
        self.row['simulation']=True;self.write()
        with self.assertRaises(ValueError):load_measurements([self.path],self.config,self.settings,False)
        self.write()
        with self.path.open('a',encoding='utf-8',newline='') as f:f.write('131,230')
        with self.assertRaises(ValueError):load_measurements([self.path],self.config,self.settings,True)

if __name__=='__main__':unittest.main()
