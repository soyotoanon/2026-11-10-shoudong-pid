"""Desktop UI for measuring AOM drive amplitude versus RF frequency."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime
import json
import math
from pathlib import Path
from queue import Empty, Queue
from statistics import mean
from threading import Event, Lock
import tkinter as tk
from tkinter import ttk, filedialog
import traceback

from instruments import LoadReadback, connect, discover_power_meters, set_meter_position
from scan import ScanConfig, ScanStopped, run_scan
from sessions import load_measurements, read_session, save_session, session_path
from data_table import DataTableWindow, read_snapshot


POWER = "输出功率 (mW)"
EFFICIENCY = "衍射效率 (%)"
ADAPTIVE = "自适应 + 区间二分"
FIXED = "固定步长"
BACKGROUND, WHITE, INK = "#edf2f5", "#ffffff", "#172e3e"
MUTED, LINE, TEAL = "#526b7b", "#dbe6ec", "#007e76"
ORANGE = "#b65619"
GOLD = "#996400"
FONT = "Microsoft YaHei UI"
STATUS = {
    "ok": "达标", "unreachable": "目标未达到", "below_range": "目标低于起点",
    "unstable": "读数不稳", "no_bracket": "未找到上升区间", "rollover": "复测确认功率下降，已停止加幅",
    "overrange": "光功率触发关断，整轮停止",
    "maximum_below_target": "已复测最大值，目标未达到",
    "maximum_unconfirmed": "最大值未确认",
}
CSV_FIELDS = (
    "frequency_mhz", "amplitude_mvpp", "output_power_mw", "power_std_mw",
    "target_power_mw", "error_mw", "efficiency_pct", "incident_mw", "tolerance_mw",
    "status", "trials", "best_attempt_mvpp", "target_mode", "target_efficiency_pct",
    "wavelength_nm", "generator", "meter_model", "meter_serial", "simulation",
    "rollover_amplitude_mvpp", "rollover_power_mw", "settle_s", "reason", "verification_error_mw", "power_limit_mw", "initial_mvpp", "meter_range_mw", "amplitude_step_mvpp",
    "plot_amplitude_mvpp", "plot_status", "plot_reason",
    "search_mode", "max_step_mvpp",
    "maximum_kind", "tested_min_mvpp", "tested_max_mvpp", "peak_bracket_low_mvpp", "peak_bracket_high_mvpp",
    "source_csv", "restored", "rising_to_limit",
)
TRIAL_FIELDS = (
    "frequency_mhz", "amplitude_mvpp", "elapsed_s", "mean_mw", "std_mw",
    "stable", "stage", "batch", "reason", "phase", "readings_mw",
    "batch_mean_mw", "batch_std_mw", "window_span_mw", "control_phase", "amplitude_step_mvpp",
)
PHASES = {
    "baseline": "零幅基线", "coarse": "步进测量", "refine": "细化测量",
    "confirm": "独立复测", "rollover_check": "下降复测",
    "warm_start": "沿用上点振幅", "initial": "估计起点测量", "local": "自适应调幅", "guard": "光功率保护",
    "adaptive": "按实测斜率调幅", "retry": "保持振幅重新采样",
    "peak_refine": "定位功率峰值", "peak_check": "复测峰值及两侧",
    "return_to_target": "回到已测目标附近振幅", "downward_predict": "按实测响应向下调幅",
    "limit_warm_start": "上点持续上升至 RF 上限，留余量起步",
}


def point_display(point, config):
    """Plot the measured value against the user's range, independently of QA."""
    amplitude = point.amplitude_mvpp if point.amplitude_mvpp is not None else point.best_attempt_mvpp
    power = point.power_mw
    if (amplitude is None or not math.isfinite(amplitude) or not 0 <= amplitude <= config.max_mvpp
            or power is None or not math.isfinite(power)):
        return None, "no_reading", "无有效的功率与振幅配对读数"
    lower, upper = config.target_mw - config.tolerance_mw, config.target_mw + config.tolerance_mw
    inside = lower <= power <= upper
    if (not inside and point.status == "maximum_below_target"
            and point.maximum_kind in ("local_peak", "rf_limit")):
        kind = "局部峰值" if point.maximum_kind == "local_peak" else "限幅最大实测值"
        reason = (f"{kind}：{power:.6g} mW @ {amplitude:.6g} mVpp，未达到设定功率范围。"
                  f"已测振幅 {point.tested_min_mvpp:g}–{point.tested_max_mvpp:g} mVpp。{point.reason}")
        return amplitude, "maximum", reason
    label = "范围内" if inside else "功率低于设定范围" if power < lower else "功率高于设定范围"
    reason = f"{label}：实测 {power:.6g} mW，设定范围 {lower:.6g}–{upper:.6g} mW。"
    if not inside and point.reason:
        reason += " " + point.reason
    return amplitude, "in_range" if inside else "out_of_range", reason


def plot_limits(points, config, zoom=False):
    if not zoom:
        return 0.0, config.max_mvpp
    values = [point_display(point, config)[0] for point in points]
    values = [value for value in values if value is not None]
    if not values:
        values = [config.initial_mvpp or 0]
    return max(0, min(values)-50), max(values)+50


class EqualizerApp:
    def __init__(self, root, simulate=False, data_dir=None, *, auto_discover=True):
        self.root = root
        self.simulate = simulate
        self.data_dir = Path(data_dir or Path(__file__).resolve().parent / "measurements")
        self.generator = self.meter = None
        self.device_settings = None
        self.points = []
        self.recovery_paths = ()
        self.restored_rows = []
        self.restored_frequencies = set()
        self.csv_path = None
        self.trials_path = None
        self.config = None
        self.incident = None
        self.total_points = 0
        self.busy = self.closing = False
        self.stop_event = Event()
        self.amplitude_step_lock = Lock()
        self.live_amplitude_step = 5.0
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aom-instruments")
        self.events = Queue()
        defaults = {
            "resource": "", "serial": "", "wavelength": "556", "mode": POWER,
            "target": "3.0", "tolerance": "0.1", "incident": "",
            "start": "95" if simulate else "", "stop": "105" if simulate else "",
            "step": "0.5", "amp_min": "5" if simulate else "220",
            "amp_max": "350" if simulate else "",
            "amp_step": "5", "search_mode": ADAPTIVE, "max_step": "15",
            "power_stop": "4" if simulate else "",
        }
        self.vars = {key: tk.StringVar(root, value) for key, value in defaults.items()}
        self.meter_choice = tk.StringVar(root, "")
        self.meter_choices = {}
        self.power_meters = []
        self.meter_selection_explicit = False
        self.discovery_warning = None
        self.inputs = []
        self.notice = tk.StringVar(root, "先连接仪器，再设定目标和扫描范围。")
        self.device_state = tk.StringVar(root, "等待连接")
        self.load_state = tk.StringVar(root, "未连接（未知）")
        self.trial_state = tk.StringVar(root, "等待扫描")
        self.file_state = tk.StringVar(root, "结果与逐次采样记录将写入两个 CSV。")
        self.metrics = [tk.StringVar(root, "—") for _ in range(3)]
        self._build()
        self._update_controls()
        self.root.after(40, self._poll)
        if auto_discover:
            self.root.after_idle(self.refresh_power_meters)
        else:
            self.device_state.set('未连接，可查看和保存已有数据')

    def _build(self):
        root = self.root
        root.title("AOM 振幅—频率测量")
        root.geometry("1500x940")
        root.minsize(1180, 800)
        root.configure(bg=BACKGROUND)
        root.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TEntry", padding=(5, 2), fieldbackground=WHITE, bordercolor=LINE)
        style.configure("TCombobox", padding=(5, 2), fieldbackground=WHITE, bordercolor=LINE)
        style.configure("TButton", padding=(8, 5), font=(FONT, 11), background="#edf4f7", bordercolor=LINE)
        style.configure("Treeview", font=(FONT, 11), rowheight=30, background=WHITE, fieldbackground=WHITE)
        style.configure("Treeview.Heading", font=(FONT, 11, 'bold'))
        style.configure("Primary.TButton", background=TEAL, foreground=WHITE, bordercolor=TEAL)
        style.map("Primary.TButton", background=[("disabled", "#dae5e7"), ("active", "#006b65")],
                  foreground=[("disabled", MUTED)])
        style.configure("Horizontal.TProgressbar", background=TEAL, troughcolor="#e9f0f3", bordercolor=WHITE)

        header = tk.Frame(root, bg=INK, padx=24, pady=13)
        header.pack(fill="x")
        self._label(header, "AOM 振幅—频率测量", size=20, bold=True, bg=INK, fg=WHITE).pack(anchor="w")
        self._label(header, "逐点扫描 RF 频率，记录参考光功率附近的振幅",
                    bg=INK, fg="#b7ceda").pack(anchor="w", pady=(2, 0))
        badge = self._label(header, "模拟预览" if self.simulate else "实验仪器",
                            bold=True, bg="#245563", fg="#e8f6f5", padx=12, pady=5)
        badge.place(relx=1.0, x=-24, rely=0.5, anchor="e")

        body = tk.Frame(root, bg=BACKGROUND, padx=16, pady=14)
        body.pack(fill="both", expand=True)
        sidebar_box = tk.Frame(body, bg=WHITE, highlightthickness=1, highlightbackground=LINE)
        sidebar_box.pack(side="left", fill="y", padx=(0, 12))
        controls = tk.Frame(sidebar_box, bg=WHITE, padx=10, pady=10)
        controls.pack(side='bottom', fill='x')
        sidebar_scroll = tk.Scrollbar(sidebar_box, orient='vertical')
        sidebar_scroll.pack(side='right', fill='y')
        sidebar_canvas = tk.Canvas(sidebar_box, width=405, bg=WHITE, highlightthickness=0,
                                   yscrollcommand=sidebar_scroll.set)
        sidebar_canvas.pack(side='left', fill='both', expand=True)
        sidebar_scroll.configure(command=sidebar_canvas.yview)
        sidebar = tk.Frame(sidebar_canvas, bg=WHITE, padx=12, pady=10)
        sidebar_window = sidebar_canvas.create_window(0, 0, window=sidebar, anchor='nw')
        sidebar.bind('<Configure>', lambda _:sidebar_canvas.configure(scrollregion=sidebar_canvas.bbox('all')))
        sidebar_canvas.bind('<Configure>', lambda e:sidebar_canvas.itemconfigure(sidebar_window, width=e.width))
        sidebar_canvas.bind('<MouseWheel>', lambda e:sidebar_canvas.yview_scroll(-int(e.delta/120), 'units'))
        self._section(sidebar, "01  仪器连接", 0)
        self._field(sidebar, 1, "DG VISA 地址", "resource")
        self._label(sidebar, "光功率计").grid(row=2, column=0, sticky="w")
        self.meter_combo = ttk.Combobox(sidebar, textvariable=self.meter_choice, width=23, state="readonly", font=(FONT,11))
        self.meter_combo.grid(row=2, column=1, sticky="ew", pady=1)
        self.meter_combo.bind("<<ComboboxSelected>>", self._select_power_meter)
        self.inputs.append((self.meter_combo, "readonly"))
        self._field(sidebar, 3, "功率计波长 (nm)", "wavelength")
        self.connect_button = ttk.Button(sidebar, text="连接仪器（RF 关闭）", command=self.connect_devices)
        self.connect_button.grid(row=4, column=0, sticky="ew", pady=(4, 0), padx=(0, 4))
        self.refresh_button = ttk.Button(sidebar, text="刷新功率计", command=self.refresh_power_meters)
        self.refresh_button.grid(row=4, column=1, sticky="ew", pady=(4, 0))
        connection_info = tk.Frame(sidebar, bg=WHITE)
        connection_info.grid(row=5, columnspan=2, sticky="ew", pady=(3, 0))
        self._label(connection_info, variable=self.device_state, fg=MUTED, wraplength=350).pack(anchor="w")
        load_header = tk.Frame(connection_info, bg=WHITE)
        load_header.pack(fill="x", pady=(6, 0))
        self._label(load_header, "CH1 输出负载", bold=True).pack(side="left")
        self.load_refresh_button = ttk.Button(load_header, text="刷新负载", command=self.refresh_output_load)
        self.load_refresh_button.pack(side="right")
        self._label(connection_info, variable=self.load_state, fg=TEAL, wraplength=350).pack(anchor="w", pady=(2, 0))
        self._label(connection_info, "扫描启动时设为 50 Ω；此处显示仪器负载设定。", size=8, fg=MUTED,
                    wraplength=350).pack(anchor="w", pady=(2, 0))

        self._section(sidebar, "02  测量条件", 6)
        self._field(sidebar, 7, "参考条件", "mode", (POWER, EFFICIENCY))
        self.target_label = self._field(sidebar, 8, "参考功率 (mW)", "target")
        self._field(sidebar, 9, "允许误差 (mW)", "tolerance")
        self._field(sidebar, 10, "入射参考 (mW)", "incident")
        self.incident_button = ttk.Button(sidebar, text="采集入射参考", command=self.capture_incident)
        self.incident_button.grid(row=11, columnspan=2, sticky="ew", pady=(4, 0))
        self._label(sidebar, "测参考时探头放入射侧，扫频前移回输出侧。", size=8, fg=MUTED).grid(
            row=12, columnspan=2, sticky="w", pady=(3, 0))

        self._section(sidebar, "03  扫描参数", 13)
        self._pair(sidebar, 14, "起始 (MHz)", "start", "终止 (MHz)", "stop")
        self._field(sidebar, 16, "频率步长 (MHz)", "step")
        self._pair(sidebar, 17, "首点估计 (mVpp)", "amp_min", "RF 安全上限 (mVpp)", "amp_max")
        self._label(sidebar, "基础步长 (mVpp)").grid(row=19, column=0, sticky="w", pady=(4, 0))
        step_frame = tk.Frame(sidebar, bg=WHITE)
        step_frame.grid(row=19, column=1, sticky="ew", pady=(4, 0))
        self.amplitude_step_entry = self._entry(step_frame, "amp_step")
        self.amplitude_step_entry.configure(width=7)
        self.amplitude_step_entry.bind("<Return>", lambda _: self.apply_amplitude_step())
        self.amplitude_step_entry.pack(side="left", fill="x", expand=True)
        self.amplitude_step_button = ttk.Button(step_frame, text="应用", width=5, command=self.apply_amplitude_step)
        self.amplitude_step_button.pack(side="right", padx=(3, 0))
        self._field(sidebar, 20, "搜索方式", "search_mode", (ADAPTIVE, FIXED))
        self._field(sidebar, 21, "自动步长上限 (mVpp)", "max_step")
        self._field(sidebar, 22, "光功率关断 (mW，可空)", "power_stop")
        self._label(sidebar, "目标附近缩步；确认上升至上限后，下点留余量起步。\nRF上限始终有效；光功率关断可留空用默认值。", size=8, fg=MUTED, wraplength=300).grid(
            row=23, columnspan=2, sticky="w", pady=(3, 4))
        self.start_button = ttk.Button(controls, text="新扫描", style="Primary.TButton", command=self.start_scan)
        self.start_button.pack(side='left', fill='x', expand=True, padx=(0,5))
        self.stop_button = ttk.Button(controls, text="停止", command=self.stop_scan)
        self.stop_button.pack(side='right', fill='x', expand=True)
        sidebar.columnconfigure(0, weight=1, minsize=128)
        sidebar.columnconfigure(1, weight=1, minsize=145)
        self.vars["mode"].trace_add("write", self._update_mode)

        main = tk.Frame(body, bg=BACKGROUND)
        main.pack(side="left", fill="both", expand=True)
        cards = tk.Frame(main, bg=BACKGROUND)
        cards.pack(fill="x", pady=(0, 10))
        for column, (caption, value) in enumerate(zip(("已记录频点", "记录振幅", "实测输出功率"), self.metrics)):
            card = tk.Frame(cards, bg=WHITE, padx=13, pady=7, highlightthickness=1, highlightbackground=LINE)
            card.grid(row=0, column=column, sticky="ew", padx=(0, 8 if column < 2 else 0))
            cards.columnconfigure(column, weight=1, uniform="cards")
            self._label(card, caption, size=9, fg=MUTED).pack(anchor="w")
            self._label(card, variable=value, size=17, bold=True).pack(anchor="w")

        toolbar = tk.Frame(main, bg=BACKGROUND)
        toolbar.pack(fill='x', pady=(0,8))
        self.load_button = ttk.Button(toolbar, text='载入测量', command=self.load_measurement_files)
        self.load_button.pack(side='left')
        self.resume_button = ttk.Button(toolbar, text='继续 / 补测', style='Primary.TButton', command=self.resume_scan)
        self.resume_button.pack(side='left', padx=(8,0))
        self.data_button = ttk.Button(toolbar, text='查看 / 保存数据', command=self.show_data_table)
        self.data_button.pack(side='left', padx=(8,0))
        self._label(toolbar, '● 达到目标（○ 恢复）   ◆ 上限内达不到', bg=BACKGROUND, size=9, fg=MUTED).pack(side='right')
        content = tk.Frame(main, bg=BACKGROUND)
        content.pack(fill='both', expand=True)
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=2)
        content.rowconfigure(0, weight=1, uniform='plots')
        content.rowconfigure(1, weight=1, uniform='plots')
        table_frame = tk.Frame(content, bg=WHITE, padx=8, pady=8, highlightthickness=1, highlightbackground=LINE)
        table_frame.grid(row=0, column=0, rowspan=2, sticky='nsew', padx=(0,10))
        self._label(table_frame, '测量记录', bold=True, size=12).pack(anchor='w', pady=(0,7))
        table_body = tk.Frame(table_frame, bg=WHITE)
        table_body.pack(fill='both',expand=True)
        self.table = ttk.Treeview(table_body, columns=('frequency','amplitude','power','status'), show='headings', height=7)
        for key, label, width in [('frequency','MHz',67),('amplitude','mVpp',85),('power','mW',85),('status','状态',110)]:
            self.table.heading(key,text=label)
            self.table.column(key,width=width,minwidth=55,anchor='center')
        table_scroll = ttk.Scrollbar(table_body,orient='vertical',command=self.table.yview)
        table_scroll.pack(side='right',fill='y')
        self.table.configure(yscrollcommand=table_scroll.set)
        self.table.pack(fill='both',expand=True)
        self.table.tag_configure('restored',foreground=MUTED)
        self.table.bind('<<TreeviewSelect>>',self._selected_record)
        self._label(table_frame, '结果说明（点击图中点可定位）', bold=True, size=10).pack(anchor='w',pady=(10,4))
        details = tk.Frame(table_frame, bg=WHITE)
        details.pack(fill='x')
        self.annotation_text = tk.Text(details, height=6, width=32, wrap='word', font=(FONT,10),
                                       bg=WHITE, fg=MUTED, relief='flat',state='disabled')
        detail_scroll = ttk.Scrollbar(details,orient='vertical',command=self.annotation_text.yview)
        detail_scroll.pack(side='right',fill='y')
        self.annotation_text.configure(yscrollcommand=detail_scroll.set)
        self.annotation_text.pack(fill='x',expand=True)
        self.chart = tk.Canvas(content, bg=WHITE, width=400, height=230, highlightthickness=1, highlightbackground=LINE)
        self.chart.grid(row=0,column=1,sticky='nsew',pady=(0,5))
        self.zoom_chart = tk.Canvas(content, bg=WHITE, width=400, height=230, highlightthickness=1, highlightbackground=LINE)
        self.zoom_chart.grid(row=1,column=1,sticky='nsew',pady=(5,0))
        for chart in (self.chart,self.zoom_chart):
            chart.bind("<Configure>", lambda _: self._draw())
        footer = tk.Frame(main, bg=WHITE, padx=12, pady=9, highlightthickness=1, highlightbackground=LINE)
        footer.pack(fill="x", pady=(10, 0))
        self._label(footer, variable=self.trial_state, fg=TEAL, bold=True, wraplength=675).pack(anchor="w")
        self.progress = ttk.Progressbar(footer, mode="determinate")
        self.progress.pack(fill="x", pady=5)
        self.notice_label = self._label(footer, variable=self.notice, wraplength=675)
        self.notice_label.pack(anchor="w")
        self._label(footer, variable=self.file_state, size=8, fg=MUTED, wraplength=675).pack(anchor="w", pady=(2, 0))

    @staticmethod
    def _label(parent, text="", *, variable=None, size=9, bold=False, bg=WHITE, fg=INK, **kwargs):
        return tk.Label(parent, text=text, textvariable=variable, font=(FONT, size+2, "bold" if bold else "normal"),
                        bg=bg, fg=fg, anchor="w", justify="left", **kwargs)

    def _section(self, parent, text, row):
        self._label(parent, text, size=10, bold=True, fg=TEAL).grid(
            row=row, columnspan=2, sticky="w", pady=(8 if row else 0, 4))

    def _entry(self, parent, key, options=None):
        if options:
            widget = ttk.Combobox(parent, textvariable=self.vars[key], values=options, width=15, state="readonly",font=(FONT,11))
        else:
            widget = ttk.Entry(parent, textvariable=self.vars[key], width=15, font=(FONT, 11))
        self.inputs.append((widget, "readonly" if options else "normal"))
        return widget

    def _field(self, parent, row, label, key, options=None):
        caption = self._label(parent, label, wraplength=145)
        caption.grid(row=row, column=0, sticky="w")
        self._entry(parent, key, options).grid(row=row, column=1, sticky="ew", pady=1)
        return caption

    def _pair(self, parent, row, left, left_key, right, right_key):
        for column, label, key in ((0, left, left_key), (1, right, right_key)):
            self._label(parent, label, size=8, fg=MUTED).grid(row=row, column=column, sticky="w", pady=(1, 0))
            self._entry(parent, key).grid(row=row + 1, column=column, sticky="ew", padx=(0, 4 if column == 0 else 0))

    def _update_mode(self, *_):
        self.target_label.configure(text="参考效率 (%)" if self.vars["mode"].get() == EFFICIENCY else "参考功率 (mW)")

    def _update_controls(self):
        for widget, idle_state in self.inputs:
            widget.configure(state="disabled" if self.busy else idle_state)
        step_state = "normal" if not self.busy or self.operation == "scan" else "disabled"
        self.amplitude_step_entry.configure(state=step_state)
        self.amplitude_step_button.configure(state=step_state)
        self.connect_button.configure(state="disabled" if self.busy else "normal")
        self.refresh_button.configure(state="disabled" if self.busy else "normal")
        self.load_refresh_button.configure(state="normal" if not self.busy and self.generator is not None else "disabled")
        ready = (not self.busy and self.generator is not None and self.meter is not None
                 and self.device_settings is not None)
        state = "normal" if ready else "disabled"
        self.start_button.configure(state=state)
        self.load_button.configure(state='disabled' if self.busy else 'normal')
        self.data_button.configure(state='normal' if not self.busy and self.points and self.csv_path else 'disabled')
        resolved = sum(point_display(p,self.config)[1] in ('in_range','maximum') for p in self.points) if self.config else 0
        self.resume_button.configure(state='normal' if ready and self.recovery_paths and resolved<self.total_points else 'disabled')
        self.incident_button.configure(state=state)
        self.stop_button.configure(state="normal" if self.busy and self.operation == "scan" else "disabled")

    def _read_amplitude_step(self):
        with self.amplitude_step_lock:
            return self.live_amplitude_step

    def _update_load_display(self):
        """Render an immutable worker readback; never perform USB I/O on Tk."""
        reading = getattr(self.generator, "load_readback", None)
        error = getattr(self.generator, "load_error", "")
        if self.generator is None:
            text = "未连接（未知）"
        elif isinstance(reading, LoadReadback):
            value = "高阻（High-Z）" if math.isinf(reading.ohms) else f"{reading.ohms:g} Ω 负载"
            text = f"{'模拟 · ' if self.simulate else ''}{value}  ·  {reading.time_text} 回读"
        elif isinstance(error, str) and error:
            text = "读取失败（未知），可刷新重试"
        else:
            text = "尚未读取（未知）"
        if self.load_state.get() != text:
            self.load_state.set(text)

    def refresh_output_load(self):
        if self.busy or self.generator is None:
            return
        self._submit("load", self.generator.read_output_load)

    def apply_amplitude_step(self):
        if self.busy and self.operation != "scan":
            return
        try:
            value = self._number("amp_step", "调幅步长")
            limit = self.config.max_mvpp - self.config.min_mvpp if self.busy else self._number("amp_max", "RF 安全上限")
            if not 0 < value <= limit:
                raise ValueError("调幅步长须大于0，且不能超过可用振幅范围。")
        except ValueError as error:
            self._say(str(error), True)
            return
        with self.amplitude_step_lock:
            self.live_amplitude_step = value
        self._say(f"基础步长已设为 {value:g} mVpp，下一次调幅生效；自适应模式受最大步长限制。")

    def _say(self, message, error=False):
        self.notice_label.configure(fg=ORANGE if error else INK)
        self.notice.set(message)

    def _number(self, key, name):
        try:
            value = float(self.vars[key].get().strip())
        except ValueError as exc:
            raise ValueError(f"请填写有效的{name}。") from exc
        if not math.isfinite(value):
            raise ValueError(f"{name}必须是有限数值。")
        return value

    def _connection_settings(self):
        return (self.vars["resource"].get().strip(), self.vars["serial"].get().strip(),
                self._number("wavelength", "波长"))

    def _set_power_meters(self, records):
        previous_serial = self.vars["serial"].get().strip()
        self.power_meters = [dict(record) for record in records]
        self.meter_choices = {}
        connected_serial = self.meter.identity.get("serial", "") if self.meter is not None else ""
        for index, record in enumerate(self.power_meters, 1):
            model = str(record.get("model", "未知型号")).strip()
            serial = str(record.get("serial", "")).strip()
            label = f"{model} · {serial or '未识别序列号'}"
            if serial and serial == connected_serial and record.get("current_connection", False):
                label += "（当前连接）"
            elif not record.get("available", False) or not serial:
                label += "（不可用）"
            if label in self.meter_choices:
                label += f" [{index}]"
            self.meter_choices[label] = record
        self.meter_combo.configure(values=tuple(self.meter_choices))
        usable = {
            label: record for label, record in self.meter_choices.items()
            if record.get("available", False) and record.get("serial", "").strip()
        }
        previous = next((label for label, record in self.meter_choices.items()
                         if str(record.get("serial", "")).strip() == previous_serial and previous_serial), None)
        if previous is not None:
            self.meter_choice.set(previous)
        elif not previous_serial and len(usable) == 1:
            label, record = next(iter(usable.items()))
            self.meter_choice.set(label)
            self.vars["serial"].set(record["serial"].strip())
            self.meter_selection_explicit = False
        else:
            self.meter_choice.set("")
            self.vars["serial"].set("")
            self.meter_selection_explicit = False
        if len(usable) > 1 and not self.meter_choice.get():
            self._say(f"发现 {len(usable)} 台可用功率计，请选择型号和序列号后连接。")
        elif usable:
            self._say(f"已识别 {len(usable)} 台可用功率计。选择后点击连接仪器。")
        else:
            self._say("未发现可用功率计。请检查连接、设备占用或驱动后刷新。", True)

    def _select_power_meter(self, _event=None):
        if self.busy:
            return
        record = self.meter_choices.get(self.meter_choice.get())
        if record is None:
            return
        serial = str(record.get("serial", "")).strip()
        self.vars["serial"].set(serial)
        self.meter_selection_explicit = bool(serial)
        if not self.meter_selection_explicit:
            self._say("所选功率计当前不可用，请检查连接或设备占用后刷新。", True)
        elif not record.get("available", False):
            self._say(f"已选择 {serial}。上次检测为不可用，点击连接仪器将重新验证实际连接。")
        else:
            self._say(f"已选择 {record.get('model', '')} · {serial}，点击连接仪器应用选择。")

    def refresh_power_meters(self):
        if self.busy:
            return
        self.discovery_warning = None
        self._say("正在识别已连接的光功率计…")

        def action():
            if self.simulate:
                records = [{"model": "SIM-PM100D", "serial": "SIM0001", "resource": "simulation", "available": True}]
            else:
                records = discover_power_meters()
            if self.meter is None:
                return records
            try:
                reading = float(self.meter.read_mw())
                if not math.isfinite(reading):
                    raise ValueError("功率计返回了非有限读数。")
                identity = self.meter.identity
                if not identity.get("model") or not identity.get("serial"):
                    raise ValueError("当前连接缺少已确认的功率计型号或序列号。")
            except Exception as error:
                self._invalidate_meter(error)
                self.discovery_warning = f"当前功率计的读数验证失败，请重新连接。{error}"
                return records
            return self._merge_connected_meter(records, identity)

        self._submit("discover", action)

    @staticmethod
    def _merge_connected_meter(records, identity):
        records = [dict(record) for record in records]
        current = {
            "model": identity["model"], "serial": identity["serial"],
            "resource": identity.get("resource", ""), "available": True,
            "current_connection": True, "identity_verified": True,
        }
        matches = [index for index, record in enumerate(records) if record.get("serial") == current["serial"]]
        if not matches:
            parts = current["resource"].upper().split("::")
            usb_id = parts[1:3] if len(parts) >= 3 and parts[0].startswith("USB") else None
            matches = []
            for index, record in enumerate(records):
                if record.get("serial"):
                    continue
                address = record.get("resource", "").upper()
                candidate = address.split("::")
                same_usb_id = usb_id is not None and candidate[0].startswith("USB") and candidate[1:3] == usb_id
                same_address = bool(address) and address == current["resource"].upper()
                if same_usb_id or same_address:
                    matches.append(index)
        if len(matches) == 1:
            records[matches[0]] = current
        else:
            records.append(current)
        return records

    def _require_current_connection(self):
        if self._connection_settings() != self.device_settings:
            raise ValueError("仪器地址、序列号或波长已修改，请重新连接。")

    def _submit(self, operation, action):
        self.operation = operation
        self.busy = True
        self.stop_event.clear()
        self._update_controls()

        def work():
            try:
                result = action()
            except Exception as exc:
                if not isinstance(exc, ScanStopped):
                    try:
                        self.data_dir.mkdir(parents=True, exist_ok=True)
                        record = {
                            "time": datetime.now().astimezone().isoformat(),
                            "operation": operation,
                            "scan_csv": str(self.csv_path) if operation == "scan" else None,
                            "error": str(exc),
                            "traceback": "".join(traceback.format_exception(exc)),
                        }
                        with (self.data_dir / "connection_errors.jsonl").open("a", encoding="utf-8") as stream:
                            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    except Exception as log_error:
                        exc.add_note(f"错误日志未能保存：{log_error}")
                self.events.put(("done", operation, None, exc))
            else:
                self.events.put(("done", operation, result, None))

        self.worker.submit(work)

    def _disconnect(self):
        # A stale USB handle must not prevent the other device from closing.
        # Clear ownership even when a driver reports a disconnect failure.
        generator, self.generator = self.generator, None
        meter, self.meter = self.meter, None
        self.device_settings = None
        errors = []
        actions = []
        if generator is not None:
            actions.extend((("关闭 RF", generator.output_off), ("释放发生器连接", generator.close)))
        if meter is not None:
            actions.append(("释放功率计连接", meter.close))
        for label, action in actions:
            try:
                action()
            except Exception as error:
                errors.append((label, error))
        if errors:
            first = errors[0][1]
            for label, error in errors[1:]:
                first.add_note(f"{label}时另有错误：{error}")
            raise first

    def _check_meter_read(self):
        value = float(self.meter.read_mw())
        if not math.isfinite(value):
            raise ValueError("功率计预检返回非有限读数，请重新连接仪器。")
        return value

    def _invalidate_meter(self, error):
        meter, self.meter = self.meter, None
        self.device_settings = None
        if meter is not None:
            try:
                meter.close()
            except Exception as cleanup_error:
                error.add_note(f"关闭失效功率计时另有错误。{cleanup_error}")

    def connect_devices(self):
        if self.busy:
            return
        try:
            resource, serial, wavelength = self._connection_settings()
            if wavelength <= 0:
                raise ValueError("波长必须大于 0 nm。")
            if not serial and len(self.power_meters) > 1:
                raise ValueError("检测到多台功率计，请明确选择型号和序列号。")
        except ValueError as exc:
            self._say(str(exc), True)
            return
        self.device_state.set("正在连接…")
        self._say("地址和序列号留空时自动选择唯一设备。")

        def action():
            self.device_settings = None
            try:
                self._disconnect()
                self.generator, self.meter = connect(self.simulate, resource, serial, wavelength, lambda _: None)
                self.generator.output_off()
                self._check_meter_read()
                read_load = getattr(self.generator, "read_output_load", None)
                if callable(read_load):
                    try:
                        read_load()
                    except Exception as load_error:
                        # A failed optional display read must not erase a good
                        # meter session. The adapter clears stale data; the UI
                        # reports its error. Scan prepare still verifies 50 Ω.
                        self.generator.load_readback = None
                        self.generator.load_error = str(load_error)
                return resource, serial, wavelength
            except Exception as error:
                self._invalidate_meter(error)
                try:
                    self._disconnect()
                except Exception as cleanup_error:
                    error.add_note(f"释放连接失败后的仪器会话时另有错误：{cleanup_error}")
                raise

        self._submit("connect", action)

    def capture_incident(self):
        if self.busy or self.meter is None or self.generator is None or self.device_settings is None:
            return
        try:
            self._require_current_connection()
        except ValueError as exc:
            self._say(str(exc), True)
            return
        self._say("正在采集入射功率。请保持探头在 AOM 入射侧。")

        def action():
            self.generator.output_off()
            set_meter_position(self.meter, "incident")
            values = []
            try:
                for _ in range(8):
                    if self.stop_event.is_set():
                        raise ScanStopped()
                    values.append(self.meter.read_mw())
            finally:
                set_meter_position(self.meter, "output")
            reference = mean(values)
            if not math.isfinite(reference) or reference <= 0:
                raise ValueError("入射功率读数必须大于 0 mW，请确认探头位置和量程。")
            return reference

        self._submit("incident", action)

    def _settings_snapshot(self):
        return {key: variable.get() for key, variable in self.vars.items()}

    def _configuration(self):
        mode = self.vars['mode'].get()
        target = self._number('target', '目标值')
        incident = None
        if self.vars['incident'].get().strip() or mode == EFFICIENCY:
            incident = self._number('incident', '入射参考')
            if incident <= 0:
                raise ValueError('入射参考必须大于 0 mW。')
        if mode == EFFICIENCY and not 0 < target <= 100:
            raise ValueError('目标效率必须大于 0% 且不超过 100%。')
        config = ScanConfig(
            start_mhz=self._number('start','起始频率'), stop_mhz=self._number('stop','终止频率'),
            step_mhz=self._number('step','频率步长'), target_mw=target * incident / 100 if mode == EFFICIENCY else target,
            tolerance_mw=self._number('tolerance','允许误差'), min_mvpp=0,
            max_mvpp=self._number('amp_max','RF 安全上限'), initial_mvpp=self._number('amp_min','首点估计'),
            amplitude_step_mvpp=self._number('amp_step','调幅步长'), amplitude_step_source=self._read_amplitude_step,
            adaptive_search=self.vars['search_mode'].get() == ADAPTIVE,
            max_step_mvpp=self._number('max_step','最大步长'),
            power_stop_mw=self._number('power_stop','光功率关断值') if self.vars['power_stop'].get().strip() else None,
        )
        return config, mode, target, incident

    def restore_measurements(self, paths, *, load_settings=True):
        if self.busy:
            raise ValueError('扫描进行中，不能载入另一组数据。')
        paths = tuple(map(Path, paths))
        if not paths:
            return
        if load_settings:
            latest = sorted(paths, key=lambda p: p.name)[-1]
            if session_path(latest).exists():
                for key, value in read_session(latest)['settings'].items():
                    if key in self.vars:
                        self.vars[key].set(value)
        config, _, _, incident = self._configuration()
        points, rows = load_measurements(paths, config, self._settings_snapshot(), self.simulate)
        self.config, self.incident = config, incident
        self.points, self.restored_rows = points, rows
        self.restored_frequencies = {p.frequency_mhz for p in points}
        self.recovery_paths = paths
        self.csv_path = sorted(paths, key=lambda p: p.name)[-1]
        self.trials_path = self.csv_path.with_name(self.csv_path.stem + '_trials.csv')
        self.total_points = len(config.frequencies_mhz())
        self.progress.configure(maximum=self.total_points, value=len(points))
        self.metrics[0].set(f'{len(points)} / {self.total_points}')
        if points:
            last = points[-1]
            amplitude, _, _ = point_display(last, config)
            self.metrics[1].set(f'{amplitude:.2f} mVpp' if amplitude is not None else '—')
            self.metrics[2].set(f'{last.power_mw:.4f} mW' if last.power_mw is not None else '—')
        pending = sum(point_display(p,config)[1] not in ('in_range','maximum') for p in points)
        self.trial_state.set(f'已恢复 {len(points)} 条记录 · 待补测 {pending} 个 · 未测 {self.total_points-len(points)} 个')
        self.file_state.set('来源：' + ' / '.join(p.name for p in paths))
        self._say('已恢复原始数据；继续测量将跳过已完成频点，原文件保留。')
        self._refresh_table()
        self._draw()
        self._update_controls()

    def load_measurement_files(self):
        if self.busy:
            return
        paths = filedialog.askopenfilenames(parent=self.root, title='载入测量结果（可选多份，不能选 trials）',
                                          initialdir=self.data_dir, filetypes=[('测量 CSV','*.csv')])
        if paths:
            try:
                self.restore_measurements(paths)
            except Exception as error:
                self._say(f'未载入：{error}', True)

    def restore_latest_session(self):
        candidates = sorted(self.data_dir.glob('*.session.json'), key=lambda p:p.stat().st_mtime, reverse=True)
        for path in candidates:
            csv_path = path.with_name(path.name.removesuffix('.session.json') + '.csv')
            if csv_path.exists() and csv_path.name.startswith('sim_') == self.simulate:
                try:
                    self.restore_measurements([csv_path])
                except Exception as error:
                    self._say(f'自动恢复未完成：{error}', True)
                break

    def resume_scan(self):
        self.start_scan(resume=True)

    def show_data_table(self):
        if self.busy or not self.points or self.csv_path is None:
            return
        try:
            snapshot = read_snapshot(self.csv_path)
            self.data_window = DataTableWindow(self.root,snapshot,can_save=lambda:not self.busy)
            self.data_window.lift()
        except Exception as error:
            self._say(f'无法打开数据表：{error}',True)

    def start_scan(self, *, resume=False):
        if self.busy or self.meter is None or self.generator is None or self.device_settings is None:
            return
        try:
            self._require_current_connection()
            wavelength = self.meter.identity["wavelength_nm"]
            config, mode, target, incident = self._configuration()
            frequencies = config.frequencies_mhz()
            if not self.simulate and (min(frequencies) < 1e-12 or max(frequencies) > 350):
                raise ValueError("DG5352 Pro 的正弦频率上限是 350 MHz，下限是 1 µHz。")
            recovered, recovered_rows = [], []
            if resume:
                if not self.recovery_paths:
                    raise ValueError('请先载入待继续的测量结果。')
                recovered, recovered_rows = load_measurements(self.recovery_paths, config, self._settings_snapshot(), self.simulate,
                    meter_serial=self.meter.identity['serial'], generator=self.generator.identity)
                if any(p.status == 'overrange' for p in recovered):
                    raise ValueError('上次触发光功率保护，不能自动续扫；请检查后另开新扫描。')
                resolved = [p for p in recovered if point_display(p,config)[1] in ('in_range','maximum')]
                if len(resolved) == len(frequencies):
                    self._say('全部频点已经完成，数据保持显示，无需重新扫描。')
                    return
        except (ValueError, OverflowError, OSError) as exc:
            self._say(f"请检查设置。{exc}", True)
            return
        self.config, self.incident = config, incident
        with self.amplitude_step_lock:
            self.live_amplitude_step = config.amplitude_step_mvpp
        sources = self.recovery_paths if resume else ()
        self.previous_recovery_paths = sources
        self.points = list(recovered)
        self.restored_rows = recovered_rows
        self.restored_frequencies = {p.frequency_mhz for p in recovered}
        self.total_points = len(frequencies)
        stem = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.csv_path = self.data_dir / f"{'sim_' if self.simulate else ''}aom_equalizer_{stem}.csv"
        self.trials_path = self.csv_path.with_name(f"{self.csv_path.stem}_trials.csv")
        self.recovery_paths = (self.csv_path,)
        settings = self._settings_snapshot()
        self.file_state.set(f"正在写入 {self.csv_path.name} 及对应 _trials.csv")
        self.metrics[0].set(f"{len(recovered)} / {self.total_points}")
        if not resume:
            self.metrics[1].set("—")
            self.metrics[2].set("—")
        self.progress.configure(maximum=self.total_points, value=len(recovered))
        self.trial_state.set('从第一个未完成频点继续…' if resume else "准备第一个频点…")
        self._say(f"测量条件 {config.target_mw:.4g} ± {config.tolerance_mw:.4g} mW。"
                  f"起始 {config.initial_mvpp:g} mVpp，{self.vars['search_mode'].get()}，基础步长 {config.amplitude_step_mvpp:g} mVpp；"
                  f"固定功率计量程，每次调节后至少等3秒；读到 {config.power_limit_mw:g} mW 时关断。")
        self._draw()
        self._refresh_table()

        def acquire_scan():
            self.generator.output_off()
            self._check_meter_read()
            set_meter_position(self.meter, "output")
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with self.csv_path.open("x", encoding="utf-8-sig", newline="") as stream, \
                    self.trials_path.open("x", encoding="utf-8-sig", newline="") as trials_stream:
                writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
                trials_writer = csv.DictWriter(trials_stream, fieldnames=TRIAL_FIELDS)
                writer.writeheader()
                trials_writer.writeheader()
                for row in recovered_rows:
                    writer.writerow({key: row.get(key, '') for key in CSV_FIELDS})
                stream.flush()
                trials_stream.flush()
                save_session(self.csv_path, settings, 'running', sources)

                def point_ready(point):
                    power = point.power_mw
                    plot_amplitude, plot_status, plot_reason = point_display(point, config)
                    writer.writerow(dict(
                        frequency_mhz=point.frequency_mhz, amplitude_mvpp=point.amplitude_mvpp,
                        output_power_mw=power, power_std_mw=point.std_mw, target_power_mw=config.target_mw,
                        error_mw=None if power is None else power - config.target_mw,
                        efficiency_pct=None if incident is None or power is None else 100 * power / incident,
                        incident_mw=incident, tolerance_mw=config.tolerance_mw, status=point.status,
                        trials=point.trials, best_attempt_mvpp=point.best_attempt_mvpp, target_mode=mode,
                        target_efficiency_pct=target if mode == EFFICIENCY else None, wavelength_nm=wavelength,
                        generator=self.generator.identity, meter_model=self.meter.identity["model"],
                        meter_serial=self.meter.identity["serial"],
                        simulation=self.simulate,
                        rollover_amplitude_mvpp=point.rollover_amplitude_mvpp,
                        rollover_power_mw=point.rollover_power_mw,
                        settle_s=config.settle_s,
                        reason=getattr(point, "reason", ""),
                        verification_error_mw=getattr(point, "verification_error_mw", None),
                        power_limit_mw=config.power_limit_mw,
                        initial_mvpp=config.initial_mvpp,
                        meter_range_mw=getattr(self.meter, "scan_range_mw", None),
                        amplitude_step_mvpp=self._read_amplitude_step(),
                        plot_amplitude_mvpp=plot_amplitude,
                        plot_status=plot_status,
                        plot_reason=plot_reason,
                        search_mode="adaptive" if config.adaptive_search else "fixed",
                        max_step_mvpp=config.max_step_mvpp,
                        maximum_kind=point.maximum_kind,
                        tested_min_mvpp=point.tested_min_mvpp,
                        tested_max_mvpp=point.tested_max_mvpp,
                        peak_bracket_low_mvpp=point.peak_bracket_low_mvpp,
                        peak_bracket_high_mvpp=point.peak_bracket_high_mvpp,
                        rising_to_limit=point.rising_to_limit,
                        source_csv=str(self.csv_path.resolve()), restored=False,
                    ))
                    stream.flush()
                    self.events.put(("point", point))

                def trial_ready(frequency, amplitude, power):
                    self.events.put(("trial", frequency, amplitude, power))

                def sample_ready(sample):
                    row = {key: sample.get(key) for key in TRIAL_FIELDS}
                    row["amplitude_step_mvpp"] = self._read_amplitude_step()
                    if row["readings_mw"] is not None:
                        row["readings_mw"] = json.dumps(row["readings_mw"])
                    trials_writer.writerow(row)
                    trials_stream.flush()
                    self.events.put(("sample", dict(sample)))

                resume_options = {'completed_points': resolved} if resume else {}
                return run_scan(config, self.generator, self.meter, self.stop_event, point_ready,
                                trial_ready, on_sample=sample_ready, **resume_options)

        def action():
            try:
                return acquire_scan()
            except ScanStopped:
                raise
            except Exception as error:
                self._invalidate_meter(error)
                raise

        self._submit("scan", action)

    def stop_scan(self):
        if self.busy and self.operation == "scan":
            self.stop_event.set()
            self._say("正在停止扫描并关闭 RF…")

    def close(self):
        if self.closing:
            return
        self.closing = True
        if self.busy:
            self.stop_event.set()
        else:
            self._submit("close", self._disconnect)

    def _poll(self):
        self._update_load_display()
        try:
            while True:
                kind, *args = self.events.get_nowait()
                if kind == "point":
                    self._point(args[0])
                elif kind == "trial":
                    frequency, amplitude, power = args
                    self.trial_state.set(f"正在测量  {frequency:g} MHz  ·  {amplitude:.2f} mVpp  ·  {power:.4f} mW")
                elif kind == "sample":
                    self._sample(args[0])
                elif not self._finish(*args):
                    return
        except Empty:
            self.root.after(40, self._poll)

    def _point(self, point):
        self.points = [p for p in self.points if abs(p.frequency_mhz-point.frequency_mhz)>1e-7] + [point]
        self.points.sort(key=lambda p:p.frequency_mhz, reverse=self.config.stop_mhz<self.config.start_mhz)
        self.restored_frequencies.discard(point.frequency_mhz)
        self.progress.configure(value=len(self.points))
        self.metrics[0].set(f"{len(self.points)} / {self.total_points}")
        amplitude, plot_status, reason = point_display(point, self.config)
        self.metrics[1].set(f"{amplitude:.2f} mVpp" if amplitude is not None else "—")
        self.metrics[2].set(f"{point.power_mw:.4f} mW" if point.power_mw is not None else "—")
        label = {"in_range": "达到目标", "out_of_range": "待补测", "no_reading": "待读数", "maximum": "上限内达不到，已复测最大值"}[plot_status]
        self.trial_state.set(f"{point.frequency_mhz:g} MHz  ·  {label}  ·  {point.trials} 次测量")
        if point.status == "overrange":
            reason += " " + point.reason
        self._say(f"{point.frequency_mhz:g} MHz  {reason}", plot_status != "in_range")
        self._refresh_table()
        self._draw()

    def _refresh_table(self):
        selection = self.table.selection()
        self.table.delete(*self.table.get_children())
        for index, point in enumerate(self.points):
            amplitude, state, _ = point_display(point,self.config)
            restored = point.frequency_mhz in self.restored_frequencies
            label = {'in_range':'达到目标','out_of_range':'待补测','maximum':'上限内达不到','no_reading':'待读数'}[state]
            self.table.insert('', 'end', iid=str(index), values=(f'{point.frequency_mhz:g}',
                f'{amplitude:.3f}' if amplitude is not None else '—',
                f'{point.power_mw:.3f}' if point.power_mw is not None else '—', label),
                tags=('restored',) if restored else ())
        if selection and self.table.exists(selection[0]):
            self.table.selection_set(selection[0])

    def _selected_record(self, _=None):
        selected = self.table.selection()
        if not selected:
            return
        point = self.points[int(selected[0])]
        source = next((r.get('source_csv','') for r in self.restored_rows
                       if abs(float(r['frequency_mhz'])-point.frequency_mhz)<1e-7), str(self.csv_path or ''))
        self._say(f'{point.frequency_mhz:g} MHz：{point_display(point,self.config)[2]} 来源：{Path(source).name}')
        self._draw()

    def _select_plot_point(self, index):
        if self.table.exists(str(index)):
            self.table.selection_set(str(index))
            self.table.see(str(index))
            self._selected_record()

    def _refresh_annotations(self):
        if self.config is None:
            return
        view = self.annotation_text.yview()[0]
        self.annotation_text.configure(state='normal')
        self.annotation_text.delete('1.0','end')
        for point in self.points:
            amplitude, state, reason = point_display(point,self.config)
            if state == 'in_range':
                continue
            if state == 'maximum':
                kind = '限幅最大值' if point.maximum_kind == 'rf_limit' else '已测分支峰值'
                text = (f'{point.frequency_mhz:g} MHz · 上限内达不到\n'
                        f'{kind} {point.power_mw:.4g} mW @ {amplitude:.4g} mVpp\n')
            else:
                text = f'{point.frequency_mhz:g} MHz · 待补测，不作能否达到的结论\n{reason}\n'
            self.annotation_text.insert('end',text+'\n')
        if not self.annotation_text.get('1.0','end').strip():
            self.annotation_text.insert('end','图中显示达到目标的振幅，以及已复测确认的上限内最大值。')
        self.annotation_text.configure(state='disabled')
        self.annotation_text.yview_moveto(view)

    def _sample(self, sample):
        phase = PHASES.get(sample.get("phase"), "测量")
        prefix = (f"{phase}  {sample['frequency_mhz']:g} MHz  ·  "
                  f"{sample['amplitude_mvpp']:.2f} mVpp  ·  {sample['elapsed_s']:.1f} 秒")
        if sample["stage"] == "settling":
            status = sample.get("reason") or "等待至少3秒后采样"
        elif sample["stage"] == "guard":
            status = f"安全监测  {sample['mean_mw']:.5g} mW"
        else:
            status = (f"第 {sample['batch']} 组  {sample['mean_mw']:.5g} mW  "
                      f"± {sample['std_mw']:.2g} mW  ·  "
                      f"{'读数稳定' if sample['stable'] else '继续检查稳定性'}")
        self.trial_state.set(f"{prefix}\n{status}")

    def _finish(self, operation, result, error):
        self.busy = False
        if operation == "close" and error is None:
            self.worker.shutdown(wait=False)
            self.root.destroy()
            return False
        if operation == "connect":
            if error is None:
                model = self.meter.identity["model"]
                serial = self.meter.identity["serial"]
                self.vars["serial"].set(serial)
                self.device_settings = result[0], serial, result[2]
                self.meter_selection_explicit = True
                records = [record for record in self.power_meters if record.get("serial") != serial]
                records.append({"model": model, "serial": serial, "resource": self.meter.identity.get("resource", ""),
                                "available": True, "current_connection": True})
                self._set_power_meters(records)
                self.device_state.set(f"DG5352 Pro / {model} · {serial}")
                if not self.recovery_paths:
                    self.vars["incident"].set("")
                self._say("仪器已连接，功率读取预检通过，RF 已关闭。" if not self.simulate else "模拟仪器已连接，可以开始扫描。")
                load_error = getattr(self.generator, "load_error", "")
                if isinstance(load_error, str) and load_error:
                    self._say(f"仪器已连接，RF 已关闭；CH1 负载读取失败：{load_error}", True)
            else:
                self.device_state.set("连接失败")
        if operation == "load" and error is None:
            self._say("CH1 输出负载已回读；未改变负载设置、振幅或 RF 开关。")
        if operation == "discover" and error is None:
            self._set_power_meters(result)
            if self.discovery_warning is not None:
                self.device_settings = None
                self.device_state.set("当前功率计读数验证失败")
                self._say(self.discovery_warning, True)
        if operation == "incident" and error is None:
            self.vars["incident"].set(f"{result:.8g}")
            self._say(f"入射参考 {result:.4f} mW。请把探头移回输出侧。")
        if operation == "scan":
            if self.csv_path.exists():
                try:
                    state = ('stopped' if isinstance(error,ScanStopped) else 'error' if error is not None
                             else 'protected' if any(p.status=='overrange' for p in self.points)
                             else 'incomplete' if any(point_display(p,self.config)[1] not in ('in_range','maximum') for p in self.points)
                             else 'completed')
                    save_session(self.csv_path,self._settings_snapshot(),state,error=str(error) if error else None)
                except Exception as save_error:
                    if error is not None:
                        error.add_note(f'保存续扫设置失败：{save_error}')
                    else:
                        error = RuntimeError(f'测量数据已写入CSV，但保存续扫设置失败：{save_error}')
            elif getattr(self,'previous_recovery_paths',()):
                self.recovery_paths = self.previous_recovery_paths
            displays = [point_display(point, self.config) for point in self.points]
            passed = sum(state == "in_range" for _, state, _ in displays)
            outside = sum(state == "out_of_range" for _, state, _ in displays)
            maxima = sum(state == "maximum" for _, state, _ in displays)
            missing = len(self.points) - passed - outside - maxima
            rollovers = sum(point.status == "rollover" for point in self.points)
            summary = f"{passed} 个达到目标  ·  {maxima} 个上限内达不到"
            if outside + missing:
                summary += f"  ·  {outside+missing} 个待补测（不计入结果）"
            if rollovers:
                summary += f"  ·  {rollovers} 个复测确认功率下降"
            self.trial_state.set(summary)
            if self.csv_path.exists():
                self.file_state.set(f"已保存 {self.csv_path.name} 及对应 _trials.csv")
            else:
                self.file_state.set("未创建扫描 CSV。")
            if error is None:
                flagged = [(point, display[2]) for point, display in zip(self.points, displays)
                           if display[1] != "in_range"]
                message = "本轮测量结束，RF 已关闭。圆点达到目标，菱形为已测区间内达不到目标的最大值。"
                if outside + missing:
                    message += ' 待补测记录保留在表中，点击“继续 / 补测”重测这些频点。'
                if flagged:
                    last, reason = flagged[-1]
                    message += f" {last.frequency_mhz:g} MHz {reason}"
                if any(point.status == "overrange" for point in self.points):
                    message += " 光功率保护已触发，扫描提前结束。"
                self._say(message, bool(flagged))
            elif isinstance(error, ScanStopped):
                self._say("已停止，RF 已关闭，保留已完成的频点。")
                error = None
        if error is not None:
            message = str(error) or type(error).__name__
            notes = getattr(error, "__notes__", ())
            if notes:
                message += "\n" + "\n".join(notes)
            if operation in ("scan", "connect") and self.meter is None:
                self.device_state.set("功率计连接失效，请重新连接")
                message += "\n请重新连接仪器后再测量。"
            self._say(message, True)
        self._update_load_display()
        self._update_controls()
        if self.closing:
            if operation == "close":
                self.closing = False
            else:
                self._submit("close", self._disconnect)
        return True

    def _draw(self):
        self._refresh_annotations()
        self._draw_chart(self.chart)
        self._draw_chart(self.zoom_chart, zoom=True)

    def _draw_chart(self, canvas, zoom=False):
        canvas.delete("all")
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < 100 or height < 180:
            return
        title = '细节图 · 已测数据上下各 50 mVpp' if zoom else '全量程图 · RF 振幅 (mVpp)'
        canvas.create_text(17, 12, text=title, fill=INK, font=(FONT, 12, "bold"), anchor="nw")
        canvas.create_text(width / 2, height - 9, text="RF 频率 (MHz)", fill=MUTED, font=(FONT, 10), anchor="s")
        left, right, top, bottom = 69, width - 27, 45, height - 45
        if self.config is None:
            canvas.create_rectangle(left, top, right, bottom, outline=LINE)
            canvas.create_text((left + right) / 2, (top + bottom) / 2, text="扫描后显示数据", fill=MUTED, font=(FONT, 10))
            return
        config = self.config
        x0, x1 = sorted((config.start_mhz, config.stop_mhz))
        if x0 == x1:
            span = max(math.ulp(x0), min(config.step_mhz / 2, x0 / 2))
            x0, x1 = x0 - span, x1 + span
        y0, y1 = plot_limits(self.points,config,zoom)

        def x(value):
            return left + (value - x0) * (right - left) / (x1 - x0)

        def y(value):
            return bottom - (value - y0) * (bottom - top) / (y1 - y0)

        for index in range(6):
            value = y0 + index * (y1 - y0) / 5
            canvas.create_line(left, y(value), right, y(value), fill=LINE)
            canvas.create_text(left - 6, y(value), text=f"{value:.1f}", fill=MUTED, font=(FONT, 10), anchor="e")
        for index in range(6):
            value = x0 + index * (x1 - x0) / 5
            canvas.create_line(x(value), top, x(value), bottom, fill=LINE)
            canvas.create_text(x(value), bottom + 3, text=f"{value:.7g}", fill=MUTED, font=(FONT, 10), anchor="n")
        if zoom and y0 < config.max_mvpp < y1:
            canvas.create_line(left,y(config.max_mvpp),right,y(config.max_mvpp),fill=ORANGE,dash=(4,4))
            canvas.create_text(right-3,y(config.max_mvpp)-3,text=f'RF 上限 {config.max_mvpp:g}',
                               anchor='se',fill=ORANGE,font=(FONT,8))
        previous = None
        previous_index = None
        grid = config.frequencies_mhz()
        selected = self.table.selection()

        def bind_marker(item, row_index, px, py):
            canvas.tag_bind(item, '<Button-1>', lambda event, i=row_index: self._select_plot_point(i))
            if selected and selected[0] == str(row_index):
                canvas.create_oval(px-8,py-8,px+8,py+8,outline=INK,width=2)

        for row_index, point in enumerate(self.points):
            index = next(i for i,f in enumerate(grid) if abs(f-point.frequency_mhz)<1e-7)
            value, plot_status, _ = point_display(point, config)
            if plot_status != "in_range":
                previous = None
                if plot_status == "maximum":
                    px, py = x(point.frequency_mhz), y(value)
                    marker = canvas.create_polygon(px, py - 5, px + 5, py, px, py + 5, px - 5, py,
                                          fill=GOLD, outline=GOLD)
                    bind_marker(marker,row_index,px,py)
                # Unresolved attempts stay in the table and the separate detail
                # panel, never masquerading as a third physical outcome.
                continue
            px, py = x(point.frequency_mhz), y(value)
            if previous is not None and previous_index is not None and index==previous_index+1:
                canvas.create_line(*previous, px, py, fill=TEAL, width=2)
            restored = point.frequency_mhz in self.restored_frequencies
            marker = canvas.create_oval(px - 3, py - 3, px + 3, py + 3, fill=WHITE if restored else TEAL, outline=TEAL, width=1 if restored else 0)
            bind_marker(marker,row_index,px,py)
            previous = (px, py)
            previous_index = index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulate", action="store_true", help="Use simulated instruments")
    parser.add_argument("--offline", action="store_true", help="View saved data without instrument discovery")
    options = parser.parse_args()
    root = tk.Tk()
    app = EqualizerApp(root, simulate=options.simulate, auto_discover=not options.offline)
    app.restore_latest_session()
    root.mainloop()


if __name__ == "__main__":
    main()
