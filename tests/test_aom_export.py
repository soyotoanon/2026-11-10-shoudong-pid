"""Data export correctness and original-record protection, without USB access."""
import csv
from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from data_table import read_snapshot, save_snapshot, DataTableWindow


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent,prefix='aom_export_')
        self.folder=Path(self.tmp.name)
        self.source=self.folder/'original.csv'
        self.fields=['frequency_mhz','amplitude_mvpp','best_attempt_mvpp','output_power_mw','target_power_mw',
                     'tolerance_mw','status','maximum_kind','reason','source_csv']
        self.rows=[['130','350','','43.57201666666666','50','1','maximum_below_target','rf_limit','上限复测',''],
                   ['131','','329.2806','44.8868','50','1','maximum_unconfirmed','','需要补测',''],
                   ['140','235.98440000000002','','49.56041666666667','50','1','ok','','精度保留','']]
        self.write(self.rows)

    def write(self,rows):
        with self.source.open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.writer(f);w.writerow(self.fields);w.writerows(rows)

    def tearDown(self):self.tmp.cleanup()

    def test_export_keeps_every_row_full_precision_and_chinese(self):
        before=self.source.read_bytes();snapshot=read_snapshot(self.source)
        target=self.folder/'结果.csv';save_snapshot(snapshot,target)
        self.assertTrue(target.read_bytes().startswith(b'\xef\xbb\xbf'))
        with target.open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
        self.assertEqual(len(rows),3)
        self.assertEqual(rows[2]['记录振幅 (mVpp)'],'235.98440000000002')
        self.assertEqual(rows[2]['光功率 (mW)'],'49.56041666666667')
        self.assertEqual(rows[1]['结果'],'待补测')
        self.assertEqual(rows[1]['最终振幅 (mVpp)'],'')
        self.assertEqual(rows[1]['记录振幅 (mVpp)'],'329.2806')
        self.assertEqual(rows[2]['测量说明'],'精度保留')
        self.assertEqual(self.source.read_bytes(),before)

    def test_retry_uses_latest_row_but_covers_all_frequencies_in_order(self):
        self.write(self.rows+[ ['134','240','','49.7','50','1','ok','','后追加',''],
                              ['131','330','','49.5','50','1','ok','','补测结果',''] ])
        snapshot=read_snapshot(self.source)
        self.assertEqual(snapshot.original_count,5)
        self.assertEqual([r[0] for r in snapshot.rows],['130','131','134','140'])
        self.assertEqual(snapshot.rows[1][1:4],('330','49.5','达到目标'))

    def test_descending_measurement_retains_frequency_direction(self):
        self.write(list(reversed(self.rows)))
        self.assertEqual([r[0] for r in read_snapshot(self.source).rows],['140','131','130'])

    def test_snapshot_does_not_change_if_another_scan_changes_file(self):
        snapshot=read_snapshot(self.source)
        self.write(self.rows[:1])
        target=self.folder/'snapshot.csv';save_snapshot(snapshot,target)
        with target.open(encoding='utf-8-sig') as f:self.assertEqual(len(list(csv.DictReader(f))),3)

    def test_protects_source_trials_session_and_same_file_alias(self):
        before=self.source.read_bytes();snapshot=read_snapshot(self.source)
        for target in (self.source,self.folder/'original_trials.csv',self.folder/'original.session.json'):
            with self.assertRaises(ValueError):save_snapshot(snapshot,target)
        alias=self.folder/'alias.csv';os.link(self.source,alias)
        with self.assertRaises(ValueError):save_snapshot(snapshot,alias)
        self.assertEqual(self.source.read_bytes(),before)

    def test_protects_restored_source_as_well(self):
        other=self.folder/'earlier.csv';other.write_text('preserve',encoding='utf-8')
        self.rows[0][-1]=str(other);self.write(self.rows)
        with self.assertRaises(ValueError):save_snapshot(read_snapshot(self.source),other)
        self.assertEqual(other.read_text(encoding='utf-8'),'preserve')

    def test_protects_new_scan_started_after_snapshot(self):
        snapshot=read_snapshot(self.source)
        new=self.folder/'aom_equalizer_20261110_203000_123456.csv'
        new.write_text('active scan record',encoding='utf-8')
        with self.assertRaises(ValueError):save_snapshot(snapshot,new)
        alias=self.folder/'alias.csv';os.link(new,alias)
        with self.assertRaises(ValueError):save_snapshot(snapshot,alias)
        self.assertEqual(new.read_text(encoding='utf-8'),'active scan record')

    def test_old_window_does_not_open_save_dialog_during_new_scan(self):
        root=tk.Tk();root.withdraw()
        view=DataTableWindow(root,read_snapshot(self.source),can_save=lambda:False)
        try:
            with patch('data_table.filedialog.asksaveasfilename') as dialog:
                view.save_as();dialog.assert_not_called()
            self.assertIn('扫描正在进行',view.status.get())
        finally:root.destroy()

    def test_write_failure_keeps_existing_destination_and_cleans_temporary(self):
        target=self.folder/'existing.csv';target.write_text('old export',encoding='utf-8')
        with patch('data_table.os.replace',side_effect=PermissionError('locked file')):
            with self.assertRaises(PermissionError):save_snapshot(read_snapshot(self.source),target)
        self.assertEqual(target.read_text(encoding='utf-8'),'old export')
        self.assertEqual(list(self.folder.glob('.aom-export-*.tmp')),[])

    def test_empty_or_truncated_input_fails_visibly(self):
        self.write([])
        with self.assertRaisesRegex(ValueError,'还没有保存'):read_snapshot(self.source)
        with self.source.open('a',encoding='utf-8') as f:f.write('130,350\n')
        with self.assertRaisesRegex(ValueError,'未写完'):read_snapshot(self.source)

    def test_dialog_cancel_and_success_and_failure(self):
        root=tk.Tk();root.withdraw()
        window=DataTableWindow(root,read_snapshot(self.source));window.withdraw()
        try:
            self.assertIsNone(root.grab_current())
            self.assertEqual(len(window.table.get_children()),3)
            with patch('data_table.filedialog.asksaveasfilename',return_value=''),patch('data_table.save_snapshot') as save:
                window.save_as();save.assert_not_called()
            path=self.folder/'chosen.csv'
            with patch('data_table.filedialog.asksaveasfilename',return_value=str(path)):
                window.save_as()
            self.assertTrue(path.exists());self.assertIn('已保存 3 个频点',window.status.get())
            with patch('data_table.filedialog.asksaveasfilename',return_value=str(self.source)):
                window.save_as()
            self.assertIn('保存失败',window.status.get())
        finally:root.destroy()


if __name__=='__main__':unittest.main()
