"""DG5352 Pro and Thorlabs adapters for quasi-static AOM scans."""

from __future__ import annotations

import ctypes as C
from dataclasses import dataclass
from datetime import datetime
import math
import threading
from time import sleep

import devices
from devices import PM100D, RigolDG, SimulatedPM100D, SimulatedRigol


_OFF = ("0", "OFF")
_ON = ("1", "ON")
# DG5000 Pro Programming Guide, chapters 3.10 and 3.12.
_MODES = ("AM", "FM", "PM", "ASK", "FSK", "PSK", "PWM", "SUM", "SWE", "BURS", "FUNC:ADV")
_METER_WARNINGS = {0x3FFC0901, 0x3FFC0902, 0x3FFC0903}
_METER_MODELS = {"PM100D", "PM100D3", "PM100USB"}


@dataclass(frozen=True)
class LoadReadback:
    ohms: float
    time_text: str


def parse_output_load(reply: str) -> float:
    """DG5000 Pro guide 3.10.3: HighZ is reported as 9.9E+37."""
    text = reply.strip().strip('"').upper()
    if text in {"INF", "INFINITY", "HIGHZ"}:
        return math.inf
    try:
        value = float(text)
    except ValueError as error:
        raise ValueError(f"CH1 输出负载回读无效：{reply!r}") from error
    if math.isfinite(value) and math.isclose(value, 9.9e37, rel_tol=1e-12):
        return math.inf
    if not math.isfinite(value) or not 1 <= value <= 10000:
        raise ValueError(f"CH1 输出负载回读超出有效范围：{reply!r}")
    return value


def _is_supported_rigol(identity: str) -> bool:
    fields = identity.upper().split(",")
    return (
        len(fields) >= 2
        and fields[0].strip().startswith("RIGOL")
        and "".join(fields[1].split()) == "DG5352PRO"
    )


def _validate_frequency(frequency_hz: float) -> None:
    if not math.isfinite(frequency_hz) or not 1e-6 <= frequency_hz <= 350e6:
        raise ValueError("DG5352 Pro 正弦频率须在 1 µHz 至 350 MHz 内。")


def discover_rigol_generators() -> list[dict[str, str]]:
    """Query instrument identities only. Discovery never enables or disables RF."""
    if devices.pyvisa is None:
        raise RuntimeError("请先运行 python -m pip install -r requirements.txt 安装 PyVISA。")
    return [item for item in devices.discover_rigol_generators() if _is_supported_rigol(item["identity"])]


def discover_power_meters() -> list[dict]:
    """Enumerate meter identity and availability without opening a session."""
    if C.sizeof(C.c_void_p) != 8 or not devices.TLPMX_DLL.exists():
        raise RuntimeError("需要 64 位 Python 及 Thorlabs TLPMX 驱动。")
    dll = C.WinDLL(str(devices.TLPMX_DLL))
    u32, u16 = C.c_uint32, C.c_uint16
    signatures = {
        "findRsrc": [u32, C.POINTER(u32)],
        "getRsrcName": [u32, u32, C.c_char_p],
        "getRsrcInfo": [u32, u32, C.c_char_p, C.c_char_p, C.c_char_p, C.POINTER(u16)],
        "errorMessage": [u32, C.c_int32, C.c_char_p],
    }
    for name, args in signatures.items():
        function = getattr(dll, "TLPMX_" + name)
        function.argtypes, function.restype = args, C.c_int32

    def call(name, *args):
        status = getattr(dll, "TLPMX_" + name)(*args)
        if status < 0:
            message = C.create_string_buffer(1024)
            dll.TLPMX_errorMessage(0, status, message)
            raise RuntimeError(f"功率计扫描 {name} 失败：{message.value.decode(errors='replace')}")

    count = u32()
    call("findRsrc", 0, C.byref(count))
    meters = []
    for index in range(count.value):
        resource, model, serial, maker = [C.create_string_buffer(1024) for _ in range(4)]
        available = u16()
        call("getRsrcName", 0, index, resource)
        call("getRsrcInfo", 0, index, model, serial, maker, C.byref(available))
        address = resource.value.decode(errors="replace").strip()
        model_name = model.value.decode(errors="replace").strip()
        number = serial.value.decode(errors="replace").strip()
        verified = model_name.upper() in _METER_MODELS and number.lower() not in ("", "n/a")
        if number.lower() == "n/a":
            number = ""
        if not verified and "0x8072" in address.lower():
            model_name = "PM100USB"
        meters.append({
            "model": model_name or "未知功率计", "serial": number, "resource": address,
            "available": bool(available.value) and verified, "identity_verified": verified,
        })
    return meters


class VerifiedRigol(RigolDG):
    def __init__(self, resource, log, timeout_ms=3000):
        if devices.pyvisa is None:
            raise RuntimeError("请先安装 PyVISA 和可用的 VISA 驱动。")
        self.log = log
        self.lock = threading.RLock()
        self.resource = resource
        self.instrument = None
        self.output_enabled = None
        self.load_readback = None
        self.load_error = ""
        self._profile_ready = False
        self._frequency_ready = False
        self._amplitude_ready = False
        self.manager = devices.pyvisa.ResourceManager()
        try:
            self.instrument = self.manager.open_resource(resource)
            self.instrument.timeout = timeout_ms
            self.instrument.read_termination = "\n"
            self.instrument.write_termination = "\n"
            self.identity = self.instrument.query("*IDN?").strip()
            if not _is_supported_rigol(self.identity):
                raise RuntimeError(f"此程序要求 RIGOL DG5352 Pro，发现 {self.identity!r}。")
            self.output_off()
        except Exception:
            self._close_visa()
            raise

    def _expect(self, command, accepted):
        reply = self.instrument.query(command).strip().strip('"').upper()
        if reply not in accepted:
            raise RuntimeError(f"DG CH1 {command} 回读 {reply!r}，要求 {' 或 '.join(accepted)}。")

    def read_output_load(self) -> LoadReadback:
        """Read the configured CH1 load only; do not change RF or voltage."""
        with self.lock:
            try:
                value = parse_output_load(self.instrument.query(":OUTP1:LOAD?"))
            except Exception as error:
                self.load_readback = None
                self.load_error = str(error)
                raise
            self.load_error = ""
            self.load_readback = LoadReadback(value, datetime.now().strftime("%H:%M:%S"))
            return self.load_readback

    def _expect_number(self, command, expected, tolerance, unit):
        actual = self._query_float(command)
        if abs(actual - expected) > tolerance:
            raise RuntimeError(
                f"DG CH1 {command} 回读 {actual:g} {unit}，设定 {expected:g} {unit}。"
            )
        return actual

    def prepare(self, frequency_hz: float) -> None:
        with self.lock:
            self._profile_ready = False
            self._frequency_ready = False
            self._amplitude_ready = False
            self.output_off()
            _validate_frequency(frequency_hz)
            # Refuse channel coupling so this scan cannot retune another output.
            for channel in (1, 2):
                for setting in ("TRAC", "FREQ:COUP", "VOLT:COUP", "PHAS:COUP"):
                    self._expect(f":SOUR{channel}:{setting}?", _OFF)
            self.load_readback = None
            self._write(":OUTP1:LOAD 50")
            self._write(":SOUR1:VOLT:UNIT VPP")
            # APPL:SIN selects Continuous mode and sets all sine parameters in one command.
            self._write(f":SOUR1:APPL:SIN {frequency_hz:.12g},0.001VPP,0,0")
            self._expect(":OUTP1?", _OFF)
            for mode in _MODES:
                self._expect(f":SOUR1:{mode}:STAT?", _OFF)
            actual_load = self.read_output_load().ohms
            if not math.isfinite(actual_load) or abs(actual_load - 50) > 0.01:
                raise RuntimeError(f"DG CH1 输出负载回读 {actual_load:g} Ω，扫描要求 50 Ω。")
            waveform = self.instrument.query(":SOUR1:APPL?").strip().strip('"').split(",", 1)[0]
            if waveform.strip().upper() != "SIN":
                raise RuntimeError(f"DG CH1 波形回读 {waveform!r}，要求 SIN。")
            self._expect(":SOUR1:VOLT:UNIT?", ("VPP",))
            self._expect_number(":SOUR1:VOLT:OFFS?", 0.0, 1e-6, "V")
            self._expect_number(":SOUR1:FREQ?", frequency_hz, 0.01, "Hz")
            self._frequency_ready = True
            self._profile_ready = True

    def set_frequency_hz(self, frequency_hz: float) -> None:
        with self.lock:
            self._frequency_ready = False
            _validate_frequency(frequency_hz)
            super().set_frequency_hz(frequency_hz)
            self._expect_number(":SOUR1:FREQ?", frequency_hz, 0.01, "Hz")
            self._frequency_ready = True

    def set_amplitude_mvpp(self, amplitude_mvpp: float) -> float:
        with self.lock:
            self._amplitude_ready = False
            super().set_amplitude_mvpp(amplitude_mvpp)
            actual_vpp = self._expect_number(":SOUR1:VOLT?", amplitude_mvpp / 1000.0, 0.0001, "Vpp")
            self._amplitude_ready = True
            return actual_vpp * 1000

    def output_on(self) -> None:
        with self.lock:
            if not (self._profile_ready and self._frequency_ready and self._amplitude_ready):
                raise RuntimeError("打开 RF 前须验证正弦配置、频率和幅度回读。")
            self.output_enabled = None
            self._write(":OUTP1 ON")
            self._expect(":OUTP1?", _ON)
            self.output_enabled = True

    def output_off(self, quiet: bool = False) -> None:
        with self.lock:
            if self.instrument is None:
                return
            self.output_enabled = None
            try:
                self._write(":OUTP1 OFF", quiet=quiet)
                self._expect(":OUTP1?", _OFF)
            except Exception as error:
                raise RuntimeError(f"无法确认 DG CH1 的 RF 已关闭，请检查发生器面板。{error}") from error
            self.output_enabled = False

    def _close_visa(self) -> None:
        self.load_readback = None
        try:
            if self.instrument is not None:
                self.instrument.close()
        finally:
            self.instrument = None
            if self.manager is not None:
                manager, self.manager = self.manager, None
                manager.close()

    def close(self) -> None:
        with self.lock:
            try:
                self.output_off(quiet=True)
            finally:
                self._close_visa()


class VerifiedPowerMeter(PM100D):
    def __init__(self, serial, wavelength_nm):
        super().__init__(serial, wavelength_nm)
        try:
            model = self.identity["model"].strip().upper()
            if model not in _METER_MODELS:
                raise RuntimeError(f"此程序支持 PM100D、PM100D3 或 PM100USB，发现 {model!r}。")
            get_unit = self.dll.TLPMX_getPowerUnit
            get_unit.argtypes = [C.c_uint32, C.POINTER(C.c_int16), C.c_uint16]
            get_unit.restype = C.c_int32
            unit = C.c_int16(-1)
            self.call("getPowerUnit", self.session, C.byref(unit), 1)
            if unit.value != 0 or not math.isfinite(self.identity["wavelength_nm"]):
                raise RuntimeError("功率计读数单位或波长回读异常。")
        except Exception:
            self.close()
            raise

    def call(self, name, *args):
        status = super().call(name, *args)
        if status in _METER_WARNINGS:
            raise RuntimeError(f"TLPMX {name} 超量程、欠量程或无效读数（0x{status:08X}）。")
        return status

    def prepare_scan(self, power_limit_mw):
        """Preselect a fixed range before light turns on; restore it after RF off."""
        if not math.isfinite(power_limit_mw) or power_limit_mw <= 0:
            raise ValueError("功率计量程要求必须为正数。")
        if getattr(self, "_scan_range_state", None) is not None:
            raise RuntimeError("功率计上次扫描的量程尚未恢复。")
        signatures = {
            "getPowerAutorange": [C.c_uint32, C.POINTER(C.c_uint16), C.c_uint16],
            "getPowerRange": [C.c_uint32, C.c_int16, C.POINTER(C.c_double), C.c_uint16],
            "setPowerAutoRange": [C.c_uint32, C.c_uint16, C.c_uint16],
            "setPowerRange": [C.c_uint32, C.c_double, C.c_uint16],
        }
        for name, args in signatures.items():
            fn = getattr(self.dll, "TLPMX_" + name)
            fn.argtypes, fn.restype = args, C.c_int32
        auto, previous = C.c_uint16(), C.c_double()
        self.call("getPowerAutorange", self.session, C.byref(auto), 1)
        self.call("getPowerRange", self.session, 0, C.byref(previous), 1)
        if auto.value not in (0, 1) or not math.isfinite(previous.value) or previous.value <= 0:
            raise RuntimeError("功率计原量程回读无效，未开始扫描。")
        self._scan_range_state = auto.value, previous.value
        # The vendor documents that setPowerRange selects a supported range
        # for this power in W and disables autoranging. Verify both properties.
        self.call("setPowerRange", self.session, power_limit_mw / 1000.0, 1)
        actual = C.c_double()
        self.call("getPowerRange", self.session, 0, C.byref(actual), 1)
        self.call("getPowerAutorange", self.session, C.byref(auto), 1)
        if auto.value != 0 or not math.isfinite(actual.value) or actual.value < power_limit_mw / 1000.0:
            raise RuntimeError("功率计固定量程未覆盖光功率保护阈值，未开启 RF。")
        self.scan_range_mw = actual.value * 1000.0
        return self.scan_range_mw

    def restore_scan(self):
        state = getattr(self, "_scan_range_state", None)
        if state is None:
            return
        self._scan_range_state = None
        errors = []
        for name, value in (("setPowerRange", state[1]), ("setPowerAutoRange", state[0])):
            try:
                self.call(name, self.session, value, 1)
            except Exception as error:
                errors.append(error)
        if errors:
            for error in errors[1:]:
                errors[0].add_note(f"恢复功率计设置时另有错误：{error}")
            raise errors[0]


class LoadAwareSimulatedRigol(SimulatedRigol):
    def __init__(self, log):
        super().__init__(log)
        self.load_ohms = 50.0
        self.load_readback = None
        self.load_error = ""

    def read_output_load(self):
        self.load_readback = LoadReadback(self.load_ohms, datetime.now().strftime("%H:%M:%S"))
        return self.load_readback

    def prepare(self, frequency_hz):
        super().prepare(frequency_hz)
        self.load_ohms = 50.0
        self.read_output_load()


class FrequencySimMeter(SimulatedPM100D):
    def read_mw(self) -> float:
        sleep(0.015)  # Emulate asynchronous instrument acquisition in preview mode.
        self._index += 1
        noise = 0.0012 * math.sin(self._index * 0.73)
        if self.mode == "dark":
            return 0.002 + noise / 10
        if self.mode == "incident":
            return 10.0 + noise
        if not self.generator.output_enabled:
            return 0.014 + noise
        detuning = (self.generator.frequency_hz - 100e6) / 1e6
        peak_amplitude = 460 + 5 * detuning + detuning**2
        peak_power = 7.4 * math.exp(-((detuning / 12) ** 2))
        fraction = min(2, self.generator.amplitude_mvpp / peak_amplitude)
        return 0.014 + peak_power * math.sin(math.pi * fraction / 2) ** 2 + noise


def connect(simulate, resource, serial, wavelength_nm, log):
    """Return (generator, meter), with real CH1 RF confirmed off before returning."""
    if not math.isfinite(wavelength_nm) or wavelength_nm <= 0:
        raise ValueError("功率计波长必须为正数。")
    if simulate:
        generator = LoadAwareSimulatedRigol(log)
        return generator, FrequencySimMeter(generator, wavelength_nm)
    serial = (serial or "").strip()
    # A cached availability flag can remain false after a USB reconnection.
    # Explicitly selected identities are checked by init and the live read.
    meters = [item for item in discover_power_meters()
              if item["model"].strip().upper() in _METER_MODELS
              and item.get("serial") and (serial or item["available"])]
    matches = [item for item in meters if not serial or item["serial"] == serial]
    if len(matches) != 1:
        if serial:
            raise ValueError(f"功率计 {serial} 当前未连接或不可用，请刷新并重新选择。")
        if not matches:
            raise ValueError("未找到可用功率计。请确认 USB 连接，并关闭占用该设备的测量程序后刷新。")
        raise ValueError("检测到多台可用功率计，请先选择型号和序列号。")
    serial = matches[0]["serial"]
    resource = (resource or "").strip()
    if not resource:
        matches = discover_rigol_generators()
        if len(matches) != 1:
            raise RuntimeError(f"自动发现 {len(matches)} 台 DG5352 Pro。请核对仪器并填写 VISA 地址。")
        resource = matches[0]["resource"]
    generator = VerifiedRigol(resource, log)
    try:
        meter = VerifiedPowerMeter(serial, wavelength_nm)
    except Exception as error:
        try:
            generator.close()
        except Exception as cleanup_error:
            raise RuntimeError(f"{error}\n关闭发生器时也报错：{cleanup_error}") from error
        raise
    return generator, meter


def set_meter_position(meter, position):
    if position not in ("incident", "output", "dark"):
        raise ValueError(f"未知探头位置 {position!r}。")
    if isinstance(meter, FrequencySimMeter):
        meter.mode = position
