"""Hardware adapters for RIGOL DG5352 Pro and Thorlabs PM100D/PM100USB."""

from __future__ import annotations

import ctypes as C
import math
from pathlib import Path
import re
import threading
import time
from typing import Callable

try:
    import pyvisa
except ImportError:  # pragma: no cover - reported cleanly at runtime
    pyvisa = None


TLPMX_DLL = Path(r"C:\Program Files\IVI Foundation\VISA\Win64\Bin\TLPMX_64.dll")


def discover_rigol_generators(timeout_ms: int = 1200) -> list[dict[str, str]]:
    if pyvisa is None:
        raise RuntimeError("未安装 PyVISA。请先运行 install_dependencies.cmd。")
    manager = pyvisa.ResourceManager()
    found: list[dict[str, str]] = []
    try:
        for resource in manager.list_resources():
            instrument = None
            try:
                instrument = manager.open_resource(resource)
                instrument.timeout = timeout_ms
                instrument.read_termination = "\n"
                instrument.write_termination = "\n"
                identity = instrument.query("*IDN?").strip()
                if re.search(r"(?:^|,)\s*DG\d", identity.upper()):
                    found.append({"resource": resource, "identity": identity})
            except Exception:
                continue
            finally:
                if instrument is not None:
                    try:
                        instrument.close()
                    except Exception:
                        pass
    finally:
        manager.close()
    return found


class RigolDG:
    def __init__(self, resource: str, log: Callable[[str], None], timeout_ms: int = 5000):
        if pyvisa is None:
            raise RuntimeError("未安装 PyVISA。请先运行 install_dependencies.cmd。")
        if not resource.strip():
            raise ValueError("请填写信号发生器 VISA 地址。")
        self.log = log
        self.lock = threading.RLock()
        self.manager = pyvisa.ResourceManager()
        self.instrument = None
        self.output_enabled = False
        try:
            self.instrument = self.manager.open_resource(resource.strip())
            self.instrument.timeout = timeout_ms
            self.instrument.read_termination = "\n"
            self.instrument.write_termination = "\n"
            self.identity = self.instrument.query("*IDN?").strip()
            if not re.search(r"(?:^|,)\s*DG\d", self.identity.upper()):
                raise RuntimeError(f"所选 VISA 设备不是 RIGOL DG 系列：{self.identity!r}")
            self.resource = resource.strip()
            self.output_off(quiet=True)
        except Exception:
            self.close()
            raise

    def _write(self, command: str, quiet: bool = False) -> None:
        if not quiet:
            self.log(f"DG << {command}")
        self.instrument.write(command)

    def _query_float(self, command: str) -> float:
        response = self.instrument.query(command).strip()
        value = float(response)
        if not math.isfinite(value):
            raise RuntimeError(f"DG 对 {command} 返回无效数值：{response!r}")
        return value

    def prepare(self, frequency_hz: float) -> None:
        with self.lock:
            self.output_off(quiet=True)
            self._write(":OUTP1:LOAD 50")
            self._write(":SOUR1:FUNC SIN")
            self._write(":SOUR1:VOLT:UNIT VPP")
            self.set_frequency_hz(frequency_hz)
            actual = self._query_float(":SOUR1:FREQ?")
            if abs(actual - frequency_hz) > max(1.0, frequency_hz * 1e-8):
                raise RuntimeError(f"频率回读不一致：设定 {frequency_hz:g} Hz，回读 {actual:g} Hz")

    def set_frequency_hz(self, frequency_hz: float) -> None:
        if not math.isfinite(frequency_hz) or frequency_hz <= 0:
            raise ValueError("RF 频率必须大于 0 Hz。")
        with self.lock:
            self._write(f":SOUR1:FREQ {frequency_hz:.12g}")

    def set_amplitude_mvpp(self, amplitude_mvpp: float) -> None:
        if not math.isfinite(amplitude_mvpp) or amplitude_mvpp <= 0:
            raise ValueError("RF 幅度必须大于 0 mVpp。")
        with self.lock:
            self._write(f":SOUR1:VOLT {amplitude_mvpp / 1000.0:.12g}")

    def output_on(self) -> None:
        with self.lock:
            self._write(":OUTP1 ON")
            self.output_enabled = True

    def output_off(self, quiet: bool = False) -> None:
        with self.lock:
            if self.instrument is None:
                return
            try:
                self._write(":OUTP1 OFF", quiet=quiet)
            finally:
                self.output_enabled = False

    def close(self) -> None:
        with getattr(self, "lock", threading.RLock()):
            if getattr(self, "instrument", None) is not None:
                try:
                    self.output_off(quiet=True)
                except Exception:
                    pass
                try:
                    self.instrument.close()
                except Exception:
                    pass
                self.instrument = None
            if getattr(self, "manager", None) is not None:
                try:
                    self.manager.close()
                except Exception:
                    pass
                self.manager = None


class PM100D:
    """Thin TLPMX adapter; signatures match the installed vendor TLPMX.h."""

    def __init__(self, serial: str = "", wavelength_nm: float = 556.0):
        if C.sizeof(C.c_void_p) != 8:
            raise RuntimeError("本程序要求 64 位 Python 和 TLPMX_64.dll。")
        if not TLPMX_DLL.exists():
            raise RuntimeError(f"未找到 Thorlabs TLPMX 驱动：{TLPMX_DLL}")
        self.dll = C.WinDLL(str(TLPMX_DLL))
        self.session = C.c_uint32(0)
        self.lock = threading.RLock()
        u32, u16, i16, dbl = C.c_uint32, C.c_uint16, C.c_int16, C.c_double
        signatures = {
            "findRsrc": [u32, C.POINTER(u32)],
            "getRsrcName": [u32, u32, C.c_char_p],
            "getRsrcInfo": [u32, u32, C.c_char_p, C.c_char_p, C.c_char_p, C.POINTER(u16)],
            "init": [C.c_char_p, u16, u16, C.POINTER(u32)],
            "close": [u32],
            "setTimeoutValue": [u32, u32],
            "setWavelength": [u32, dbl, u16],
            "getWavelength": [u32, i16, C.POINTER(dbl), u16],
            "setPowerUnit": [u32, i16, u16],
            "measPower": [u32, C.POINTER(dbl), u16],
            "errorMessage": [u32, C.c_int32, C.c_char_p],
        }
        for name, args in signatures.items():
            function = getattr(self.dll, "TLPMX_" + name)
            function.argtypes, function.restype = args, C.c_int32

        count = u32()
        self.call("findRsrc", self.session, C.byref(count))
        matches: list[tuple[bytes, str, str]] = []
        for index in range(count.value):
            resource, model, number, maker = [C.create_string_buffer(1024) for _ in range(4)]
            available = u16()
            self.call("getRsrcName", self.session, index, resource)
            self.call("getRsrcInfo", self.session, index, model, number, maker, C.byref(available))
            actual_serial = number.value.decode(errors="replace").strip()
            if not serial.strip() or actual_serial == serial.strip():
                matches.append((resource.value, model.value.decode(errors="replace"), actual_serial))
        if len(matches) != 1:
            names = ", ".join(item[2] for item in matches) or "无"
            raise RuntimeError(
                f"未找到唯一的功率计 {serial or '(任意序列号)'}；匹配设备：{names}；"
                f"TLPMX 共发现 {count.value} 台兼容设备。"
            )
        resource, model, actual_serial = matches[0]
        try:
            self.call("init", resource, 1, 0, C.byref(self.session))
            self.call("setTimeoutValue", self.session, 1500)
            self.call("setWavelength", self.session, float(wavelength_nm), 1)
            self.call("setPowerUnit", self.session, 0, 1)  # 0 = W
            readback = dbl()
            self.call("getWavelength", self.session, 0, C.byref(readback), 1)
            if abs(readback.value - wavelength_nm) > 0.1:
                raise RuntimeError("功率计波长回读不一致。")
            self.identity = {
                "model": model,
                "serial": actual_serial,
                "resource": resource.decode(errors="replace"),
                "wavelength_nm": readback.value,
                "unit": "W",
                "driver": str(TLPMX_DLL),
            }
        except Exception:
            self.close()
            raise

    def call(self, name: str, *args):
        status = getattr(self.dll, "TLPMX_" + name)(*args)
        if status < 0:
            message = C.create_string_buffer(1024)
            self.dll.TLPMX_errorMessage(self.session, status, message)
            raise RuntimeError(
                f"TLPMX {name}: {status}: {message.value.decode(errors='replace')}"
            )
        return status

    def read_mw(self) -> float:
        with self.lock:
            power = C.c_double()
            self.call("measPower", self.session, C.byref(power), 1)
            value = power.value * 1000.0
            if not math.isfinite(value) or abs(value) > 1e6:
                raise RuntimeError("功率计返回无效或超量程读数。")
            return value

    def close(self) -> None:
        with getattr(self, "lock", threading.RLock()):
            if getattr(self, "session", C.c_uint32(0)).value:
                try:
                    self.dll.TLPMX_close(self.session)
                finally:
                    self.session.value = 0


class SimulatedRigol:
    def __init__(self, log: Callable[[str], None]):
        self.log = log
        self.identity = "SIMULATED,RIGOL DG5352 Pro,0,0"
        self.resource = "SIM::DG5352"
        self.amplitude_mvpp = 0.0
        self.frequency_hz = 100e6
        self.output_enabled = False

    def prepare(self, frequency_hz: float) -> None:
        self.output_off()
        self.frequency_hz = frequency_hz

    def set_frequency_hz(self, frequency_hz: float) -> None:
        self.frequency_hz = frequency_hz

    def set_amplitude_mvpp(self, amplitude_mvpp: float) -> None:
        self.amplitude_mvpp = amplitude_mvpp

    def output_on(self) -> None:
        self.output_enabled = True

    def output_off(self, quiet: bool = False) -> None:
        self.output_enabled = False

    def close(self) -> None:
        self.output_off()


class SimulatedPM100D:
    def __init__(self, generator: SimulatedRigol, wavelength_nm: float = 556.0):
        self.generator = generator
        self.identity = {
            "model": "SIM-PM100D",
            "serial": "SIM0001",
            "resource": "SIM::PM100D",
            "wavelength_nm": wavelength_nm,
            "unit": "W",
            "driver": "simulation",
        }
        self.mode = "incident"
        self._index = 0

    def read_mw(self) -> float:
        self._index += 1
        phase = self._index * 0.73
        noise = 0.0025 * math.sin(phase) + 0.0012 * math.sin(phase * 2.7)
        if self.mode == "dark":
            return 0.0002 + noise * 0.01
        if self.mode == "incident":
            return 10.0 * (1.0 + noise)
        amplitude = max(0.0, self.generator.amplitude_mvpp)
        normalized = max(0.0, (amplitude - 250.0) / 250.0)
        efficiency = 0.72 * math.sin(min(normalized, 1.15) * math.pi / 2.3) ** 2
        if not self.generator.output_enabled:
            efficiency = 0.0
        return 0.0002 + 10.0 * efficiency * (1.0 + noise)

    def close(self) -> None:
        pass


def interruptible_sleep(stop_event: threading.Event, seconds: float) -> bool:
    """Sleep in short slices; return False when interrupted."""
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        if stop_event.wait(min(0.05, deadline - time.monotonic())):
            return False
    return not stop_event.is_set()
