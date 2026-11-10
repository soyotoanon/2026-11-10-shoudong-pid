"""Read-only measurement snapshots and CSV export; no instrument access."""
from __future__ import annotations

import csv
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import tempfile
import tkinter as tk
from tkinter import filedialog, ttk


COLUMNS = (
    ('frequency_mhz', '频率 (MHz)'),
    ('record_amplitude_mvpp', '记录振幅 (mVpp)'),
    ('output_power_mw', '光功率 (mW)'),
    ('result_label', '结果'),
    ('target_power_mw', '目标功率 (mW)'),
    ('tolerance_mw', '允许误差 (mW)'),
    ('power_std_mw', '功率标准差 (mW)'),
    ('error_mw', '功率偏差 (mW)'),
    ('trials', '测量次数'),
    ('amplitude_mvpp', '最终振幅 (mVpp)'),
    ('best_attempt_mvpp', '尝试振幅 (mVpp)'),
    ('maximum_kind', '最大值类型'),
    ('reason', '测量说明'),
    ('wavelength_nm', '功率计波长 (nm)'),
    ('meter_model', '功率计型号'),
    ('meter_serial', '功率计序列号'),
    ('source_csv', '原始数据来源'),
)
LABELS = dict(COLUMNS)
LABELS.update({
    'status':'原始测量状态', 'target_mode':'参考方式', 'target_efficiency_pct':'目标效率 (%)',
    'efficiency_pct':'实测效率 (%)', 'incident_mw':'入射参考 (mW)', 'generator':'信号发生器',
    'simulation':'模拟测量', 'settle_s':'调节后等待 (s)', 'verification_error_mw':'复测偏差 (mW)',
    'power_limit_mw':'光功率关断值 (mW)', 'initial_mvpp':'首点估计 (mVpp)',
    'meter_range_mw':'功率计量程 (mW)', 'amplitude_step_mvpp':'基础步长 (mVpp)',
    'max_step_mvpp':'自动步长上限 (mVpp)', 'search_mode':'搜索方式',
    'rollover_amplitude_mvpp':'回落触发振幅 (mVpp)', 'rollover_power_mw':'回落触发功率 (mW)',
    'tested_min_mvpp':'已测最低振幅 (mVpp)', 'tested_max_mvpp':'已测最高振幅 (mVpp)',
    'peak_bracket_low_mvpp':'峰值区间下界 (mVpp)', 'peak_bracket_high_mvpp':'峰值区间上界 (mVpp)',
    'rising_to_limit':'持续上升至RF上限', 'restored':'恢复记录',
    'plot_amplitude_mvpp':'原图振幅 (mVpp)', 'plot_status':'原图状态', 'plot_reason':'原图说明',
})


@dataclass(frozen=True)
class MeasurementSnapshot:
    source: Path
    keys: tuple[str, ...]
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    protected_paths: tuple[Path, ...]
    original_count: int


def _finite(value):
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def _result(row):
    power, target, tolerance = (_finite(row.get(k)) for k in
                                ('output_power_mw','target_power_mw','tolerance_mw'))
    amplitude = row.get('amplitude_mvpp') or row.get('best_attempt_mvpp') or ''
    if _finite(amplitude) is None or None in (power,target,tolerance):
        return amplitude, '待读数'
    if abs(power-target) <= tolerance + 1e-12:
        return amplitude, '达到目标'
    if (power < target-tolerance and row.get('status') == 'maximum_below_target'
            and row.get('maximum_kind') in ('rf_limit','local_peak')):
        return amplitude, '上限内达不到'
    return amplitude, '待补测'


def read_snapshot(path):
    """Copy a finished CSV once; latest row wins for a remeasured frequency."""
    source = Path(path).resolve()
    with source.open('r', encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream, strict=True)
        fields = reader.fieldnames
        if not fields or not {'frequency_mhz','output_power_mw','status'}.issubset(fields):
            raise ValueError('请选择测量结果 CSV，而不是逐次采样 trials 文件。')
        merged, protected, count = {}, {source}, 0
        for row in reader:
            if None in row or any(v is None for v in row.values()):
                raise ValueError('数据文件有未写完的行，请等测量保存完成后再打开。')
            frequency = _finite(row['frequency_mhz'])
            if frequency is None:
                raise ValueError('测量文件包含无效频率，未导出。')
            count += 1
            row['record_amplitude_mvpp'], row['result_label'] = _result(row)
            row['source_csv'] = row.get('source_csv') or str(source)
            protected.add(Path(row['source_csv']).resolve())
            # Dict assignment preserves the original sweep order, also when
            # a retry appears at the end of the file.
            merged[frequency] = row
    if not merged:
        raise ValueError('这轮还没有保存频点，请在完成至少一个频点后查看。')
    keys = tuple(k for k, _ in COLUMNS) + tuple(k for k in fields if k not in dict(COLUMNS))
    headers = tuple(LABELS.get(key,key) for key in keys)
    for original in tuple(protected):
        protected.add(original.with_name(original.stem+'_trials.csv'))
        protected.add(original.with_suffix('.session.json'))
    order = list(merged)
    descending = len(order)>1 and order[1]<order[0]
    return MeasurementSnapshot(source, keys, headers,
        tuple(tuple(merged[f].get(key,'') for key in keys) for f in sorted(merged,reverse=descending)),
        tuple(protected), count)


def save_snapshot(snapshot, destination):
    """Write full stored precision atomically, never over original records."""
    destination = Path(destination).resolve()
    if destination.suffix.lower() != '.csv':
        raise ValueError('请将数据保存为 .csv 文件，可用 Excel 打开。')
    # Another scan may have started since this window was opened. Protect its
    # files too, including hard-link aliases, without opening an instrument.
    original_name = re.compile(r'^(?:sim_)?aom_equalizer_\d{8}_\d{6}_\d{6}(?:_trials)?\.csv$')
    protected = set(snapshot.protected_paths)
    protected.update(p.resolve() for p in snapshot.source.parent.glob('*.csv') if original_name.fullmatch(p.name))
    if destination.parent == snapshot.source.parent and original_name.fullmatch(destination.name):
        raise ValueError('该文件名用于原始测量记录，请使用“数据表”或其他新文件名。')
    for source in protected:
        if destination == source or (destination.exists() and source.exists() and destination.samefile(source)):
            raise ValueError('不能覆盖原始测量或采样记录，请另选文件名。')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8-sig', newline='',
                prefix='.aom-export-', suffix='.tmp', dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.writer(stream)
            writer.writerow(snapshot.headers)
            writer.writerows(snapshot.rows)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


class DataTableWindow(tk.Toplevel):
    def __init__(self, parent, snapshot, can_save=None):
        super().__init__(parent)
        self.snapshot = snapshot
        self.can_save = can_save
        self.title(f'完整数据表 — {snapshot.source.stem}')
        self.geometry('1260x720')
        self.minsize(800,450)
        self.configure(bg='#edf2f5')
        self.status = tk.StringVar(self, f'共 {len(snapshot.rows)} 个频点；保存保留原始数值精度。')
        header = tk.Frame(self,bg='#edf2f5',padx=16,pady=12)
        header.pack(fill='x')
        tk.Label(header,text='完整测量数据',font=('Microsoft YaHei UI',17,'bold'),
                 bg='#edf2f5',fg='#172e3e').pack(side='left')
        self.save_button = ttk.Button(header,text='保存数据 CSV…',command=self.save_as)
        self.save_button.pack(side='right')
        tk.Label(self,text=str(snapshot.source.name),font=('Microsoft YaHei UI',10),
                 bg='#edf2f5',fg='#526b7b',anchor='w').pack(fill='x',padx=16,pady=(0,10))
        body = ttk.Frame(self)
        body.pack(fill='both',expand=True,padx=16)
        body.columnconfigure(0,weight=1);body.rowconfigure(0,weight=1)
        self.table = ttk.Treeview(body,columns=snapshot.keys,show='headings',selectmode='extended')
        self.table.grid(row=0,column=0,sticky='nsew')
        ys = ttk.Scrollbar(body,orient='vertical',command=self.table.yview)
        xs = ttk.Scrollbar(body,orient='horizontal',command=self.table.xview)
        ys.grid(row=0,column=1,sticky='ns');xs.grid(row=1,column=0,sticky='ew')
        self.table.configure(yscrollcommand=ys.set,xscrollcommand=xs.set)
        for key,label in zip(snapshot.keys,snapshot.headers):
            self.table.heading(key,text=label)
            self.table.column(key,width=220 if key in ('reason','source_csv','generator','plot_reason') else 135,
                              minwidth=90,stretch=False,anchor='center')
        for index,row in enumerate(snapshot.rows):
            display=[]
            for key,value in zip(snapshot.keys,row):
                number=_finite(value)
                if number is not None and (key.endswith(('_mw','_mvpp')) or key in ('frequency_mhz','wavelength_nm')):
                    value=f'{number:.6g}'
                display.append(value or '—')
            self.table.insert('', 'end', iid=str(index), values=display)
        self.status_label = tk.Label(self,textvariable=self.status,font=('Microsoft YaHei UI',10),
            bg='#edf2f5',fg='#526b7b',anchor='w',justify='left',wraplength=1180)
        self.status_label.pack(fill='x',padx=16,pady=(10,4))
        tk.Label(self,text='表格为打开时的数据副本。横向滚动可查看全部字段，保存不会改动原始记录。',
            font=('Microsoft YaHei UI',10),bg='#edf2f5',fg='#526b7b',anchor='w').pack(fill='x',padx=16,pady=(0,12))
        # Non-modal: no grab_set / wait_window and no scan worker interaction.

    def save_as(self):
        if self.can_save is not None and not self.can_save():
            self.status.set('扫描正在进行，请在本轮结束后保存。表格数据已保留。')
            return
        path = filedialog.asksaveasfilename(parent=self,title='保存测量数据',
            defaultextension='.csv',initialdir=self.snapshot.source.parent,
            initialfile=self.snapshot.source.stem+'_数据表.csv',
            filetypes=[('CSV 表格（Excel 可打开）','*.csv')],confirmoverwrite=True)
        if not path:
            return
        if self.can_save is not None and not self.can_save():
            self.status.set('扫描正在进行，请在本轮结束后保存。表格数据已保留。')
            return
        try:
            saved = save_snapshot(self.snapshot,path)
        except Exception as error:
            self.status.set(f'保存失败：{error}')
            self.status_label.configure(fg='#b65619')
            return
        self.status.set(f'已保存 {len(self.snapshot.rows)} 个频点：{saved}')
        self.status_label.configure(fg='#007e76')


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv',type=Path)
    args=parser.parse_args()
    snapshot=read_snapshot(args.csv)
    root=tk.Tk()
    root.withdraw()
    window=DataTableWindow(root,snapshot)
    window.protocol('WM_DELETE_WINDOW',root.destroy)
    root.mainloop()


if __name__=='__main__':
    main()
