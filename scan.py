"""Measure RF amplitude versus frequency near a reference optical power."""

import math
import time
from dataclasses import dataclass, field
from statistics import fmean, stdev
from typing import Callable


_MAX_FREQUENCIES = 10_000
_COARSE_INTERVALS = 12
_MAX_COARSE_STEP_MVPP = 5.0
_MIN_ENABLED_MVPP = 1.0
_BISECTION_TRIALS = 12
_READINGS_PER_TRIAL = 2
_STABLE_BATCHES = 3
_BATCH_INTERVAL_S = 0.25
_STABILITY_TIMEOUT_S = 8.0
_GUARD_INTERVAL_S = 0.1
_MAX_LOCAL_STEP_MVPP = 5.0
_LOCAL_TRIALS = 64
_UNSTABLE_WINDOWS = 3
_CONFIRMATION_RETRIES = 4


class ScanStopped(RuntimeError):
    """The operator requested a stop."""


class RFOffError(RuntimeError):
    """The final command to turn off RF did not succeed."""


@dataclass(frozen=True)
class ScanConfig:
    start_mhz: float
    stop_mhz: float
    step_mhz: float
    target_mw: float
    tolerance_mw: float
    min_mvpp: float
    max_mvpp: float
    settle_s: float = 3.0
    initial_mvpp: float | None = None
    amplitude_step_mvpp: float | None = None
    amplitude_step_source: Callable[[], float] | None = field(default=None, repr=False, compare=False)
    adaptive_search: bool = False
    max_step_mvpp: float = 15.0
    power_stop_mw: float | None = None

    def __post_init__(self):
        values = (
            self.start_mhz, self.stop_mhz, self.step_mhz, self.target_mw,
            self.tolerance_mw, self.min_mvpp, self.max_mvpp, self.settle_s,
        )
        if not all(map(math.isfinite, values)):
            raise ValueError("扫频参数必须是有限数值。")
        if min(self.start_mhz, self.stop_mhz, self.step_mhz) <= 0:
            raise ValueError("起始频率、结束频率和频率步长都必须大于零。")
        if not math.isfinite(max(self.start_mhz, self.stop_mhz) * 1e6):
            raise ValueError("频率超出可表示的范围。")
        if self.target_mw <= 0 or not 0 < self.tolerance_mw < self.target_mw:
            raise ValueError("功率目标须大于零，容差须大于零且小于功率目标。")
        if not 0 <= self.min_mvpp < self.max_mvpp:
            raise ValueError("振幅范围须满足 0 ≤ 最小值 < 最大值。")
        if self.min_mvpp == 0 and self.max_mvpp < _MIN_ENABLED_MVPP:
            raise ValueError("从 0 mVpp 开始时，安全上限须至少为 1 mVpp，发生器才能输出第一个有 RF 的点。")
        if self.settle_s < 0:
            raise ValueError("每点等待时间不能小于零。")
        if not math.isfinite(self.power_limit_mw):
            raise ValueError("功率目标和容差过大，无法生成有限的光功率保护阈值。")
        if self.initial_mvpp is not None:
            if not math.isfinite(self.initial_mvpp) or not self.min_mvpp <= self.initial_mvpp <= self.max_mvpp:
                raise ValueError("估计起始振幅必须是安全范围内的有限数值。")
            if 0 < self.initial_mvpp < _MIN_ENABLED_MVPP:
                raise ValueError("起始振幅须为 0 mVpp，或至少 1 mVpp；发生器无法输出介于两者之间的振幅。")
        if self.amplitude_step_mvpp is not None:
            _validate_amplitude_step(self.amplitude_step_mvpp, self)
        if not math.isfinite(self.max_step_mvpp) or self.max_step_mvpp <= 0:
            raise ValueError("最大调幅步长须为正的有限数。")
        if self.power_stop_mw is not None:
            if not math.isfinite(self.power_stop_mw) or self.power_stop_mw <= self.target_mw:
                raise ValueError("光功率关断值须为大于目标功率的有限数。")

    @property
    def power_limit_mw(self):
        if self.power_stop_mw is not None:
            return self.power_stop_mw
        return self.target_mw + max(2 * self.tolerance_mw, self.target_mw * 0.1)

    def frequencies_mhz(self):
        """Include both endpoints, even if the last step is shorter."""
        start, stop, step = self.start_mhz, self.stop_mhz, self.step_mhz
        if start == stop:
            return [start]

        steps = abs(stop - start) / step
        if steps > _MAX_FREQUENCIES - 1:
            raise ValueError(f"单次扫描最多支持 {_MAX_FREQUENCIES} 个频率点。")

        direction = 1 if stop > start else -1
        frequencies = [start]
        for index in range(1, math.ceil(steps)):
            frequency = start + direction * index * step
            if direction * (stop - frequency) <= math.ulp(stop):
                break
            if direction * (frequency - frequencies[-1]) <= 0:
                raise ValueError("步长太小，无法区分相邻频率。")
            frequencies.append(frequency)
        frequencies.append(stop)
        return frequencies


@dataclass(frozen=True)
class ScanPoint:
    frequency_mhz: float
    amplitude_mvpp: float | None
    power_mw: float
    std_mw: float
    status: str
    trials: int
    best_attempt_mvpp: float | None = None
    rollover_amplitude_mvpp: float | None = None
    rollover_power_mw: float | None = None
    reason: str = ""
    verification_error_mw: float | None = None
    maximum_kind: str = ""
    tested_min_mvpp: float | None = None
    tested_max_mvpp: float | None = None
    peak_bracket_low_mvpp: float | None = None
    peak_bracket_high_mvpp: float | None = None
    rising_to_limit: bool = False


@dataclass(frozen=True)
class _Trial:
    amplitude: float
    power: float
    std: float
    stable: bool = True
    sample_count: int = _READINGS_PER_TRIAL * _STABLE_BATCHES

    def error_with_scatter(self, target):
        return abs(self.power - target) + self.std


class _UnstableMeasurement(Exception):
    def __init__(self, trial, reason):
        super().__init__(reason)
        self.trial = trial
        self.reason = reason


class _PowerOverrange(Exception):
    def __init__(self, trial, limit):
        self.trial = trial
        self.limit = limit


def _precision(config):
    return min(config.tolerance_mw / 4, config.target_mw * 0.005)


def _on_target(config, trial):
    if config.adaptive_search:
        return config.target_mw - config.tolerance_mw <= trial.power <= config.target_mw
    # Search toward the center. This fine search goal is not the final
    # acceptance tolerance selected by the operator.
    return (abs(trial.power - config.target_mw) <= _precision(config)
            and trial.error_with_scatter(config.target_mw) <= config.tolerance_mw)


def _confirmation_failure(config, candidate, check):
    for label, trial in (("候选测量", candidate), ("独立复测", check)):
        if not trial.stable:
            return f"{label}未通过连续采样稳定性检查。"
        error = (abs(trial.power - config.target_mw) if config.adaptive_search
                 else trial.error_with_scatter(config.target_mw))
        if error > config.tolerance_mw:
            metric = "均值偏差" if config.adaptive_search else "均值偏差加标准差"
            return (f"{label} {trial.power:.6g} mW，{metric} {error:.6g} mW，"
                    f"超过设定容差 {config.tolerance_mw:g} mW。")
    difference = abs(check.power - candidate.power)
    if difference > config.tolerance_mw:
        return (f"两次测量相差 {difference:.6g} mW，"
                f"超过设定容差 {config.tolerance_mw:g} mW。")
    return None


def _repeatability_limit(config, first, second):
    noise = math.hypot(first.std / math.sqrt(first.sample_count),
                       second.std / math.sqrt(second.sample_count))
    return min(config.tolerance_mw, max(_precision(config) / 2, 3 * noise))


def _validate_amplitude_step(value, config):
    if not math.isfinite(value) or not 0 < value <= config.max_mvpp - config.min_mvpp:
        raise ValueError("调幅步长必须为正的有限数，且不能超过可用振幅范围。")
    return value


def _amplitude_step(config):
    value = config.amplitude_step_source() if config.amplitude_step_source is not None else config.amplitude_step_mvpp
    return None if value is None else _validate_amplitude_step(value, config)


def _local_step(config, power, gain=None):
    configured = _amplitude_step(config)
    if configured is not None:
        return configured
    error = abs(config.target_mw - power)
    if error >= max(2 * config.tolerance_mw, config.target_mw * 0.1):
        limit = _MAX_LOCAL_STEP_MVPP
    elif error >= max(config.tolerance_mw, config.target_mw * 0.04):
        limit = 2.0
    else:
        limit = 1.0
    if gain is None or gain <= 0:
        return min(1.0, limit)
    return min(limit, max(0.05, 0.7 * error / gain))


def _check_stopped(stop_event):
    if stop_event.is_set():
        raise ScanStopped("扫频已停止。")


def _set_scan_amplitude(generator, amplitude_mvpp, limit_mvpp):
    if amplitude_mvpp <= 0:
        raise ValueError("RF 开启时，发生器振幅必须为正值。")
    if amplitude_mvpp > limit_mvpp:
        raise ValueError("请求振幅超过 RF 安全上限。")
    actual_mvpp = generator.set_amplitude_mvpp(amplitude_mvpp)
    if actual_mvpp is None:
        return amplitude_mvpp
    if not math.isfinite(actual_mvpp) or actual_mvpp <= 0:
        raise RuntimeError("发生器返回了无效的振幅读数。")
    if actual_mvpp > limit_mvpp:
        raise RuntimeError(f"发生器振幅回读 {actual_mvpp:.9g} mVpp 超过 RF 安全上限 {limit_mvpp:.9g} mVpp。")
    return actual_mvpp


def _rising(lower, upper):
    mean_noise = math.hypot(lower.std / math.sqrt(lower.sample_count), upper.std / math.sqrt(upper.sample_count))
    return upper.power - lower.power > 2 * mean_noise


def _power_dropped(lower, upper):
    mean_noise = math.hypot(lower.std / math.sqrt(lower.sample_count), upper.std / math.sqrt(upper.sample_count))
    threshold = max(0.001, 0.01 * lower.power, 3 * mean_noise)
    return lower.power - upper.power > threshold


def _bisect(config, lower, upper, measure):
    for _ in range(_BISECTION_TRIALS):
        midpoint = lower.amplitude + (upper.amplitude - lower.amplitude) / 2
        if not lower.amplitude < midpoint < upper.amplitude:
            break
        trial = measure(midpoint, "refine")
        if _power_dropped(lower, trial):
            return None, (lower, trial)
        if _power_dropped(trial, upper):
            return None, (trial, upper)
        if (
            lower.power - trial.power > 2 * math.hypot(lower.std, trial.std)
            or trial.power - upper.power > 2 * math.hypot(upper.std, trial.std)
        ):
            break
        if _on_target(config, trial):
            return trial, None
        if trial.power < config.target_mw:
            lower = trial
        else:
            upper = trial
    return None, None


def _coarse_amplitudes(config):
    start = config.min_mvpp
    if start == 0:
        start = _MIN_ENABLED_MVPP
        yield start

    if _amplitude_step(config) is not None:
        amplitude = start
        while amplitude < config.max_mvpp:
            next_amplitude = min(config.max_mvpp, amplitude + _amplitude_step(config))
            if next_amplitude <= amplitude:
                raise ValueError("调幅步长太小，无法表示下一振幅。")
            amplitude = next_amplitude
            yield amplitude
        return

    width = config.max_mvpp - start
    if width > 0:
        intervals = max(_COARSE_INTERVALS, math.ceil(width / _MAX_COARSE_STEP_MVPP))
        for index in range(1, intervals + 1):
            amplitude = start + width * (index / intervals)
            yield min(amplitude, config.max_mvpp)


def _acquire(config, frequency_mhz, amplitude, generator, meter, stop_event, phase, on_sample):
    started = time.monotonic()
    precision = _precision(config)
    stability_span = max(precision / 2, min(config.tolerance_mw / 2, config.target_mw * 0.02))

    def report(batch, power, deviation, stable, stage, reason, readings=None, span=None):
        if on_sample is not None:
            on_sample({
                "frequency_mhz": frequency_mhz,
                "amplitude_mvpp": amplitude,
                "elapsed_s": time.monotonic() - started,
                "mean_mw": power,
                "std_mw": deviation,
                "stable": stable,
                "stage": stage,
                "phase": "guard" if stage == "guard" else phase,
                "control_phase": phase,
                "batch": batch,
                "reason": reason,
                "readings_mw": readings,
                "batch_mean_mw": fmean(readings) if readings else None,
                "batch_std_mw": stdev(readings) if readings and len(readings) > 1 else 0 if readings else None,
                "window_span_mw": span,
            })

    def read_power(batch, guard=False):
        _check_stopped(stop_event)
        value = float(meter.read_mw())
        if not math.isfinite(value):
            raise ValueError("光功率计返回了非有限读数。")
        limit = config.power_limit_mw
        if value >= limit:
            # Switch off before callbacks, disk writes, or any further waiting.
            generator.output_off()
            reason = f"测得 {value:.6g} mW 达到保护阈值 {limit:.6g} mW，已立即关闭 RF。"
            report(batch, value, 0.0, False, "guard", reason, [value], 0.0)
            raise _PowerOverrange(_Trial(amplitude, value, 0.0, False, 1), limit)
        if guard:
            report(batch, value, 0.0, False, "guard", "稳定等待期间仅检查功率保护", [value], 0.0)
        return value

    report(0, None, None, False, "settling",
           "同一振幅重新采样" if config.adaptive_search and phase in ("confirm", "retry") else "等待设定后的稳定时间")
    # A repeat at the same setting needs a fresh window, not another setting delay.
    settling = 0.0 if config.adaptive_search and phase in ("confirm", "retry") else config.settle_s
    guard_checks = max(1, math.ceil(settling / _GUARD_INTERVAL_S))
    guard_interval = settling / guard_checks
    settle_deadline = time.monotonic() + settling
    for index in range(guard_checks):
        guard_started = time.monotonic()
        if index and guard_started >= settle_deadline:
            break
        read_power(index + 1, guard=True)
        # Slow USB reads count toward settling; do not insist on 30 reads if
        # each call takes longer than the nominal 100 ms monitoring interval.
        now = time.monotonic()
        pause = max(0.0, min(guard_interval - (now - guard_started), settle_deadline - now))
        if stop_event.wait(pause):
            raise ScanStopped("扫频已停止。")
    sampling_started = time.monotonic()
    batches = []
    max_batches = math.floor(_STABILITY_TIMEOUT_S / _BATCH_INTERVAL_S) + 1
    for batch_index in range(1, max_batches + 1):
        readings = []
        for _ in range(_READINGS_PER_TRIAL):
            readings.append(read_power(batch_index))
        batches.append(readings)
        window = batches[-_STABLE_BATCHES:]
        samples = [value for batch in window for value in batch]
        power = fmean(samples)
        deviation = stdev(samples)
        batch_means = [fmean(batch) for batch in window]
        span = max(batch_means) - min(batch_means)
        enough_batches = len(window) == _STABLE_BATCHES
        stable = (
            enough_batches
            and span <= stability_span
            and deviation <= stability_span / 2
        )
        reason = "读数已稳定" if stable else "等待连续采样窗口稳定"
        report(batch_index, power, deviation, stable, "confirm" if phase == "confirm" else "sample", reason, readings, span)
        trial = _Trial(amplitude, power, deviation, stable, len(samples))
        if stable:
            return trial
        if time.monotonic() - sampling_started >= _STABILITY_TIMEOUT_S:
            break
        # Zero settling time is the explicit fast path for deterministic tests.
        pause = _BATCH_INTERVAL_S if config.settle_s else 0
        if stop_event.wait(pause):
            raise ScanStopped("扫频已停止。")
    return trial


def _find_point(config, frequency_mhz, generator, meter, stop_event, on_trial, on_sample,
                warm_start=None, start_phase="warm_start"):
    best = None
    history = []
    trials = 0
    start_amplitude = config.min_mvpp if warm_start is None else warm_start
    rf_enabled = start_amplitude > 0
    active_amplitude = start_amplitude

    def measure(amplitude, phase="coarse", apply=True):
        nonlocal best, trials, rf_enabled, active_amplitude
        _check_stopped(stop_event)
        if amplitude > 0 and apply:
            configured = _amplitude_step(config)
            if configured is not None and active_amplitude > 0 and not config.adaptive_search:
                amplitude = max(active_amplitude - configured, min(active_amplitude + configured, amplitude))
            elif config.adaptive_search and active_amplitude > 0:
                amplitude = max(active_amplitude - config.max_step_mvpp,
                                min(active_amplitude + config.max_step_mvpp, amplitude))
            amplitude = _set_scan_amplitude(generator, amplitude, config.max_mvpp)
            active_amplitude = amplitude
            if not rf_enabled:
                _check_stopped(stop_event)
                generator.output_on()
                rf_enabled = True
        attempts = _UNSTABLE_WINDOWS if config.adaptive_search else 1
        for attempt in range(attempts):
            trials += 1
            trial = _acquire(config, frequency_mhz, amplitude, generator, meter, stop_event,
                             phase if attempt == 0 else "retry", on_sample)
            if trial.stable and (best is None or trial.error_with_scatter(config.target_mw) < best.error_with_scatter(config.target_mw)):
                best = trial
            if trial.stable and amplitude > 0:
                history.append(trial)
            if on_trial is not None:
                on_trial(frequency_mhz, amplitude, trial.power)
            _check_stopped(stop_event)
            if trial.stable:
                return trial
        raise _UnstableMeasurement(trial, f"同一振幅已测 {attempts} 个完整采样窗口，仍持续漂移或波动过大，停止该频点。")

    def point(status, selected=None, rollover=None, diagnostic=None, reason="", maximum_kind="", bracket=None,
              rising_to_limit=False):
        reading = rollover[0] if rollover else selected or diagnostic or best
        trigger = rollover[1] if rollover else None
        return ScanPoint(
            frequency_mhz=frequency_mhz,
            amplitude_mvpp=selected.amplitude if selected is not None else None,
            power_mw=reading.power,
            std_mw=reading.std,
            status=status,
            trials=trials,
            best_attempt_mvpp=reading.amplitude if selected is None else None,
            rollover_amplitude_mvpp=trigger.amplitude if trigger is not None else None,
            rollover_power_mw=trigger.power if trigger is not None else None,
            reason=reason,
            verification_error_mw=reading.power - config.target_mw if selected is not None else None,
            maximum_kind=maximum_kind,
            tested_min_mvpp=min((t.amplitude for t in history), default=None),
            tested_max_mvpp=max((t.amplitude for t in history), default=None),
            peak_bracket_low_mvpp=bracket[0] if bracket else None,
            peak_bracket_high_mvpp=bracket[1] if bracket else None,
            rising_to_limit=rising_to_limit,
        )

    target, precision = config.target_mw, _precision(config)

    def confirmed(candidate):
        check = measure(candidate.amplitude, "confirm", apply=False)
        failure = _confirmation_failure(config, candidate, check)
        if failure is not None:
            return point("unstable", diagnostic=check, reason=failure)
        return point("ok", selected=check, reason="连续采样稳定，已收敛并通过独立复测。")

    def verified_rollover(peak, trigger):
        check = measure(trigger.amplitude, "rollover_check", apply=trigger.amplitude != active_amplitude)
        if abs(check.power - trigger.power) > _repeatability_limit(config, trigger, check) or not _power_dropped(peak, check):
            return point("unstable", diagnostic=check, reason="疑似回落的独立复测不一致，已停止该频点。")
        return point("rollover", rollover=(peak, check), reason="正振幅扫描中的光功率回落经稳定采样和复测确认。")

    def refine(lower, upper):
        if lower.amplitude == 0:
            return point("unreachable", reason="目标位于 RF 关闭与最小可输出振幅之间，无法在可输出范围内收敛。")
        selected, rollover = _bisect(config, lower, upper, measure)
        if rollover:
            return verified_rollover(*rollover)
        if selected is not None:
            return confirmed(selected)
        return point("unstable", reason="细化搜索未在限定次数内达到内部收敛误差。")

    def maximum_below_target(boundary=False):
        """Verify a bounded observed maximum, never infer a global optimum.

        Return a fresh trial to resume target seeking if refinement finds the
        target after all. A safety stop or exhausted search is not peak evidence.
        """
        minimum = config.min_mvpp or _MIN_ENABLED_MVPP
        safe_ceiling = max(t.amplitude for t in history)
        resolution = min(2.0, max(0.5, _amplitude_step(config) or 2.0))

        def margin(t):
            return max(precision / 2, 3 * t.std / math.sqrt(t.sample_count))

        def unresolved(t, reason):
            return point("maximum_unconfirmed", diagnostic=t, reason=reason)

        def visit(amplitude, phase="peak_refine"):
            # Travel only inside the observed safe envelope, observing every
            # intermediate step. Never jump back across an unmeasured interval.
            if not minimum <= amplitude <= safe_ceiling:
                raise ValueError("峰值复测请求超出已测安全区间。")
            t = None
            while abs(amplitude - active_amplitude) > 1e-8:
                before = active_amplitude
                final_leg = abs(amplitude - before) <= config.max_step_mvpp + 1e-8
                t = measure(amplitude, phase)
                if t.power >= target - config.tolerance_mw:
                    return t
                if final_leg:
                    # Setters return the actual, quantized instrument readback.
                    # Do not keep commanding an unrepresentable decimal value.
                    return t
                if abs(active_amplitude - before) < 1e-9:
                    raise _UnstableMeasurement(t, "发生器分辨率不足，无法细查峰值。")
            return t if t is not None else measure(amplitude, "confirm", apply=False)

        candidate = max(history, key=lambda t: t.power)
        if any(t.power + margin(t) >= target - config.tolerance_mw for t in history):
            return unresolved(candidate, "曾测到接近目标范围的读数，不能判定目标不可达；未登记最大值。")
        if candidate.power <= max(0.001, target * 0.001):
            return unresolved(candidate, "读数接近暗底，不能把无响应认作功率峰值。")

        # A maximum at the voltage ceiling is a constrained measured maximum,
        # not an optical turning point. For ties, prefer the ceiling itself.
        end = history[-1]
        at_ceiling = (boundary and end.amplitude >= config.max_mvpp - 1e-8
                      and candidate.power - end.power <= margin(candidate) + margin(end))
        if at_ceiling:
            if len({t.amplitude for t in history}) < 3:
                return unresolved(candidate, "已在 RF 上限，但有效振幅采样不足，不能确认区间最大值。")
            candidate = end
            check = visit(candidate.amplitude)
            if check.power >= target - config.tolerance_mw:
                return check
            if (abs(check.power - candidate.power) > _repeatability_limit(config, candidate, check)
                    or check.power + margin(check) >= target - config.tolerance_mw):
                return unresolved(check, "RF 上限处复测不一致或不确定度覆盖目标范围，不能确认最大值。")
            # A ceiling result alone does not prove an increasing response.
            # Keep the latest independent reading at each measured amplitude.
            by_amplitude = {t.amplitude: t for t in history}
            ordered = sorted(by_amplitude.values(), key=lambda t: t.amplitude)
            tail = [t for t in ordered if t.amplitude >= config.max_mvpp - 30]
            rising = (len(tail) >= 3
                      and tail[-1].power - tail[0].power > margin(tail[-1]) + margin(tail[0])
                      and all(b.power + margin(b) >= a.power - margin(a)
                              for a, b in zip(ordered, ordered[1:])))
            return point("maximum_below_target", selected=check, maximum_kind="rf_limit",
                         rising_to_limit=rising,
                         bracket=(check.amplitude, check.amplitude),
                         reason=(f"已到 RF 振幅上限 {config.max_mvpp:g} mVpp；该点复测仍低于目标范围。"
                                 "记录为本次已测区间内的限幅最大值，不代表超过上限后仍不可达。"))

        # Locate evidence of both flanks. If we started on the descending side,
        # step downward to find the missing left flank, never further upward.
        right_candidates = [t for t in history if t.amplitude > candidate.amplitude and _power_dropped(candidate, t)]
        if not right_candidates:
            return unresolved(candidate, "没有足够的下降侧证据，不能把当前最好读数当作峰值。")
        right = min(right_candidates, key=lambda t: t.amplitude)
        left = None
        descending_cursor = candidate.amplitude
        for _ in range(12):
            left_candidates = [t for t in history if t.amplitude < candidate.amplitude and _power_dropped(candidate, t)]
            if left_candidates:
                left = max(left_candidates, key=lambda t: t.amplitude)
                break
            amplitude = max(minimum, descending_cursor - min(config.max_step_mvpp, max(resolution, 5.0)))
            if amplitude >= descending_cursor:
                break
            # The lower flank may not have been visited before; extending down
            # is allowed, extending above the confirmed rollover is not.
            check = visit(amplitude)
            descending_cursor = check.amplitude
            if check.power >= target - config.tolerance_mw:
                return check
            if check.power > candidate.power:
                candidate = check
        if left is None:
            return unresolved(candidate, "未确认峰值的上升侧，保留当前读数但不登记最大值。")
        flank_left, flank_right = left, right
        # Interval refinement under the observed single-peak assumption.
        for _ in range(12):
            if right.amplitude - left.amplitude <= resolution:
                break
            if candidate.amplitude - left.amplitude >= right.amplitude - candidate.amplitude:
                amplitude = (left.amplitude + candidate.amplitude) / 2
            else:
                amplitude = (candidate.amplitude + right.amplitude) / 2
            check = visit(amplitude)
            if check.power >= target - config.tolerance_mw:
                return check
            if abs(check.amplitude - candidate.amplitude) < 1e-8:
                return unresolved(check, "发生器分辨率不足以细查峰值。")
            if check.amplitude < candidate.amplitude:
                if check.power > candidate.power:
                    right, candidate = candidate, check
                else:
                    left = check
            elif check.power > candidate.power:
                left, candidate = candidate, check
            else:
                right = check
        if right.amplitude - left.amplitude > resolution:
            return unresolved(candidate, "峰值定位尚未收敛，未登记最大值。")

        # Revisit both original flanks and then the peak. This rejects temporal
        # drift masquerading as a power maximum and preserves amplitude/power pairing.
        checked_flanks = []
        for flank in (flank_left, flank_right):
            check = visit(flank.amplitude, "peak_check")
            if check.power >= target - config.tolerance_mw:
                return check
            if abs(check.power - flank.power) > _repeatability_limit(config, flank, check):
                return unresolved(check, "峰值两侧复测发生漂移，不能确认最大值。")
            checked_flanks.append(check)
        check = visit(candidate.amplitude, "peak_check")
        if check.power >= target - config.tolerance_mw:
            return check
        if (abs(check.power - candidate.power) > _repeatability_limit(config, candidate, check)
                or not all(_power_dropped(check, flank) for flank in checked_flanks)
                or any(t.power + margin(t) >= target - config.tolerance_mw for t in history)):
            return unresolved(check, "峰值复测或与两侧的比较不确定，未登记最大值。")
        return point("maximum_below_target", selected=check, maximum_kind="local_peak",
                     bracket=(left.amplitude, right.amplitude),
                     reason=(f"两侧下降及峰值复测已确认，局部峰值 {check.power:.6g} mW @ {check.amplitude:.6g} mVpp，"
                             f"定位区间 {left.amplitude:.6g}–{right.amplitude:.6g} mVpp；仍低于目标范围。"
                             "仅代表本次已测响应分支，未越过回落区继续加幅。"))

    def adaptive_search(previous):
        # The voltage ceiling is NOT a measured upper bracket. Expand only after
        # observing a rising response; bisect only between actual measurements.
        lower = previous if previous.power < target else None
        upper = previous if previous.power >= target else None
        gains = []
        rising_count = 0
        progress_count = 0
        flat_count = 0
        confirmation_failures = 0
        minimum = config.min_mvpp if config.min_mvpp > 0 else _MIN_ENABLED_MVPP
        for _ in range(_LOCAL_TRIALS):
            if _on_target(config, previous):
                check = measure(previous.amplitude, "confirm", apply=False)
                if _on_target(config, check):
                    return point("ok", selected=check, reason="连续采样和独立复测均在设定功率范围内。")
                # A single noisy/outside repeat is not a failed frequency.
                # Re-read unchanged output before deciding which way to adjust.
                check = measure(previous.amplitude, "retry", apply=False)
                confirmation_failures += 1
                if confirmation_failures >= _CONFIRMATION_RETRIES and not _on_target(config, check):
                    return point("unstable", diagnostic=check,
                                 reason=f"已进行 {_CONFIRMATION_RETRIES} 轮复测并重新寻找振幅，读数仍反复偏出设定范围。")
                previous = check
                lower = check if check.power < target else None
                upper = check if check.power >= target else None
                gains.clear()
                rising_count = flat_count = progress_count = 0
                if _on_target(config, previous):
                    continue
            probe = min(_amplitude_step(config) or 2.0, config.max_step_mvpp)
            direction = 1 if previous.power < target else -1
            if direction < 0:
                # Above-target corrections use measured history, not midpoint
                # bisection. Prefer an actual below-target operating point.
                aim = target - min(config.tolerance_mw / 2, target * 0.01)
                candidates = [t for t in history if t.amplitude < previous.amplitude
                              and target - config.tolerance_mw <= t.power <= target]
                if candidates:
                    known = min(candidates, key=lambda t: (abs(t.power - aim), -history.index(t)))
                    proposal = known.amplitude
                    phase = "return_to_target"
                else:
                    below = [t for t in history if t.amplitude < previous.amplitude and t.power < aim]
                    if below:
                        known = max(below, key=lambda t: t.amplitude)
                        slope = (previous.power - known.power) / (previous.amplitude - known.amplitude)
                        proposal = previous.amplitude - (previous.power - aim) / slope
                    else:
                        slope = max(gains[-4:], default=2 * max(previous.power, 0.001) / max(previous.amplitude, 1))
                        proposal = previous.amplitude - max(probe, (previous.power - aim) / max(slope, 1e-9))
                    phase = "downward_predict"
                proposal = max(previous.amplitude - config.max_step_mvpp, proposal)
            elif lower is not None and upper is not None:
                proposal = (lower.amplitude + upper.amplitude) / 2
                phase = "refine"
            else:
                # First observe two small steps. A stable non-dark response can
                # then use larger steps; the measured gain still caps the rise.
                growing = progress_count if previous.power > max(0.001, target * 0.001) else 0
                growth = min(64.0, 4.0 ** min(3, max(0, growing - 1)))
                step = min(config.max_step_mvpp, probe * growth)
                proposal = previous.amplitude + direction * step
                phase = "adaptive"
            if proposal > previous.amplitude:
                # Empirical slope + noise margin. This reduces risk; it is not
                # a mathematical bound on an unknown optical plant's response.
                slope = max(gains[-4:], default=0.0)
                prior_slope = 2 * max(previous.power, 0) / max(previous.amplitude, 1)
                slope = 1.5 * slope if rising_count >= 2 else max(1.5 * slope, prior_slope)
                if slope > 0:
                    mean_noise = previous.std / math.sqrt(previous.sample_count)
                    budget = min(0.75 * max(0, target - previous.power),
                                 0.5 * max(0, config.power_limit_mw - previous.power - 3 * mean_noise))
                    proposal = min(proposal, previous.amplitude + budget / slope)
            proposal = max(minimum, min(config.max_mvpp, proposal))
            if abs(proposal - previous.amplitude) < 1e-9:
                at_limit = previous.amplitude >= config.max_mvpp - 1e-8
                if at_limit and previous.power < target - config.tolerance_mw:
                    result = maximum_below_target(boundary=True)
                    if isinstance(result, ScanPoint):
                        return result
                    previous = result
                    lower = result if result.power < target else None
                    upper = result if result.power >= target else None
                    gains.clear()
                    rising_count = flat_count = progress_count = 0
                    continue
                return point("unreachable", diagnostic=previous,
                             reason=(f"已到 RF 振幅上限 {config.max_mvpp:g} mVpp，功率仍未进入设定范围。"
                                     if at_limit else "没有可继续调整的安全步长或可输出振幅，停止该频点。"))
            current = measure(proposal, phase)
            a, b = sorted((previous, current), key=lambda reading: reading.amplitude)
            if a.amplitude == b.amplitude:
                return point("unstable", diagnostic=current, reason="步长小于发生器分辨率，停止该频点。")
            if _power_dropped(a, b):
                if current.amplitude > previous.amplitude:
                    check = measure(current.amplitude, "rollover_check", apply=False)
                    if _power_dropped(a, check) and abs(check.power - current.power) > _repeatability_limit(config, current, check):
                        current = check
                        check = measure(current.amplitude, "retry", apply=False)
                        if _power_dropped(a, check) and abs(check.power - current.power) > _repeatability_limit(config, current, check):
                            return point("unstable", diagnostic=check,
                                         reason="疑似回落已重复采样，仍持续漂移，停止该频点。")
                    if _power_dropped(a, check):
                        result = maximum_below_target()
                        if isinstance(result, ScanPoint):
                            return result
                        check = result
                    # A transient dip recovered: discard the stale bracket and
                    # slope history, then continue from the fresh measurement.
                    previous = check
                    lower = check if check.power < target else None
                    upper = check if check.power >= target else None
                    gains.clear()
                    rising_count = flat_count = progress_count = 0
                    continue
                return point("unstable", diagnostic=current, reason="减幅后功率反而上升，停止该频点。")
            if _rising(a, b):
                noise = math.hypot(a.std / math.sqrt(a.sample_count), b.std / math.sqrt(b.sample_count))
                gains.append((b.power - a.power + 3 * noise) / (b.amplitude - a.amplitude))
                rising_count += 1
                flat_count = 0
            else:
                rising_count = 0
                flat_count += 1
            progress_count += 1
            if current.power < target:
                lower = current
            else:
                upper = current
            dark_limit = max(0.001, config.target_mw * 0.001)
            if flat_count >= 3 and direction > 0 and current.power <= dark_limit:
                return point("no_bracket", diagnostic=current,
                             reason="连续三次加幅仍接近暗底且无可靠上升，停止加幅，请检查光路。")
            previous = current
        return point("unreachable", diagnostic=previous, reason="自适应搜索达到次数上限，停止该频点。")

    try:
        phase = start_phase if warm_start is not None else "baseline" if start_amplitude == 0 else "coarse"
        previous = measure(start_amplitude, phase)
        if config.adaptive_search:
            return adaptive_search(previous)
        if _on_target(config, previous):
            return confirmed(previous)
        if warm_start is not None:
            gain = None
            minimum = config.min_mvpp if config.min_mvpp > 0 else _MIN_ENABLED_MVPP
            for _ in range(_LOCAL_TRIALS):
                step = _local_step(config, previous.power, gain)
                direction = 1 if previous.power < target else -1
                amplitude = min(config.max_mvpp, max(minimum, previous.amplitude + direction * step))
                if amplitude == previous.amplitude:
                    return point("unreachable", reason="相邻频点的局部调整已达到安全振幅边界。")
                current = measure(amplitude, "local")
                lower, upper = sorted((previous, current), key=lambda trial: trial.amplitude)
                if _power_dropped(lower, upper):
                    if direction > 0:
                        return verified_rollover(lower, upper)
                    return point("unstable", diagnostic=current, reason="减小振幅时功率反而上升，局部斜率异常，已停止该频点。")
                if _on_target(config, current):
                    return confirmed(current)
                if lower.power <= target <= upper.power:
                    if _rising(lower, upper):
                        return refine(lower, upper)
                    return point("unstable", diagnostic=current, reason="局部目标区间的斜率无法可靠确认。")
                if upper.amplitude == lower.amplitude:
                    return point("unstable", diagnostic=current, reason="发生器振幅分辨率不足以完成当前局部调整。")
                gain = (upper.power - lower.power) / (upper.amplitude - lower.amplitude)
                previous = current
            return point("unreachable", reason="相邻频点在限定的局部调整次数内未收敛，后续频点将重新初始化。")
        if previous.power - previous.std > target + precision:
            return point("below_range", reason="最小振幅的稳定读数已高于目标，当前范围无法向下调节。")
        # RF-off light may contain residual tails and is not an RF power peak.
        peak = previous if previous.amplitude > 0 else None
        uncertain_crossing = False

        for amplitude in _coarse_amplitudes(config):
            current = measure(amplitude)
            if peak is not None and _power_dropped(peak, current):
                return verified_rollover(peak, current)
            if peak is None or current.power > peak.power:
                peak = current
            if _on_target(config, current):
                return confirmed(current)
            crosses_target = previous.power <= target <= current.power
            if _rising(previous, current) and crosses_target:
                return refine(previous, current)
            if crosses_target:
                uncertain_crossing = True
            previous = current

        if uncertain_crossing or abs(best.power - target) <= precision:
            return point("unstable", reason="观测到目标附近读数，但无法确认稳定的上升区间。")
        return point("unreachable", reason="在安全振幅范围内未达到目标功率。")
    except _UnstableMeasurement as failure:
        return point("unstable", diagnostic=failure.trial, reason=failure.reason)
    except _PowerOverrange as failure:
        return point("overrange", diagnostic=failure.trial,
                     reason=f"功率达到保护阈值 {failure.limit:.6g} mW，已关闭 RF 并停止整轮扫描。")


def _neighbor_start(point, config):
    """Return only a supported seed; a local peak/failed reading is not a seed."""
    if point is None:
        return None, "warm_start"
    if point.status == "ok":
        return point.amplitude_mvpp, "warm_start"
    if (config.adaptive_search and point.status == "maximum_below_target"
            and point.maximum_kind == "rf_limit" and point.rising_to_limit
            and point.amplitude_mvpp is not None
            and math.isclose(point.amplitude_mvpp, config.max_mvpp, abs_tol=1e-4, rel_tol=0)
            and point.power_mw < config.target_mw - config.tolerance_mw):
        minimum = config.min_mvpp or _MIN_ENABLED_MVPP
        # At 350 mVpp leave 50 mVpp headroom; smaller ranges leave 15%.
        headroom = min(50.0, config.max_mvpp * .15)
        return max(minimum, config.max_mvpp-headroom), "limit_warm_start"
    return None, "warm_start"


def run_scan(config, generator, meter, stop_event, on_point, on_trial=None, on_sample=None, *, completed_points=()):
    """Read one point per frequency and leave RF off even on interruption.

    The generator's prepare and set methods must preserve the output state.
    Hardware amplitude setters return the instrument readback in mVpp.
    A trial at 0 mVpp reads the meter with the generator output disabled.
    Callbacks receive complete points and optional per-trial mean readings.
    on_sample receives dictionaries for settling and individual sample batches.
    """
    frequencies = config.frequencies_mhz()
    completed = {}
    for point in completed_points:
        match = next((f for f in frequencies if math.isclose(f, point.frequency_mhz, rel_tol=0, abs_tol=1e-7)), None)
        if match is None or match in completed:
            raise ValueError("恢复数据的频点不在当前网格中或有重复。")
        if point.status == "overrange":
            raise ValueError("上次扫描触发了光功率保护，不能自动续扫；请检查后另开新扫描。")
        amplitude = point.amplitude_mvpp
        if amplitude is not None and (not math.isfinite(amplitude) or not config.min_mvpp <= amplitude <= config.max_mvpp):
            raise ValueError("恢复振幅超出当前安全范围。")
        completed[match] = point
    pending = [f for f in frequencies if f not in completed]
    points = [completed[f] for f in frequencies if f in completed]
    if not pending:
        return points
    scan_error = None
    previous_point = None
    try:
        generator.output_off()
        _check_stopped(stop_event)
        _amplitude_step(config)
        prepare_meter = getattr(meter, "prepare_scan", None)
        if prepare_meter is not None:
            prepare_meter(config.power_limit_mw)
        generator.prepare(pending[0] * 1e6)
        for frequency in frequencies:
            _check_stopped(stop_event)
            if frequency in completed:
                previous_point = completed[frequency]
                continue
            generator.set_frequency_hz(frequency * 1e6)
            warm_start = None
            previous_amplitude, start_phase = _neighbor_start(previous_point, config)
            if previous_amplitude is not None and previous_amplitude > 0:
                minimum = config.min_mvpp if config.min_mvpp > 0 else _MIN_ENABLED_MVPP
                warm_start = max(minimum, previous_amplitude if config.adaptive_search else previous_amplitude * 0.98)
            elif config.initial_mvpp is not None and config.initial_mvpp > 0:
                warm_start = config.initial_mvpp
                start_phase = "initial"
            initial_amplitude = config.min_mvpp if warm_start is None else warm_start
            if initial_amplitude > 0:
                initial_amplitude = _set_scan_amplitude(generator, initial_amplitude, config.max_mvpp)
                if warm_start is not None:
                    warm_start = initial_amplitude
                _check_stopped(stop_event)
                generator.output_on()
            point = _find_point(config, frequency, generator, meter, stop_event, on_trial, on_sample,
                                warm_start, start_phase)
            generator.output_off()
            points.append(point)
            on_point(point)
            if point.status == "overrange":
                break
            previous_point = point
        _check_stopped(stop_event)
        return sorted(points, key=lambda p: frequencies.index(next(f for f in frequencies if abs(f-p.frequency_mhz)<1e-7)))
    except BaseException as error:
        scan_error = error
        raise
    finally:
        try:
            generator.output_off()
        except Exception as off_error:
            message = f"最终 RF 关断失败，请在发生器面板确认输出状态。{off_error}"
            if scan_error is not None:
                message += f" 扫频此前中断。{scan_error}"
            raise RFOffError(message) from off_error
        restore_meter = getattr(meter, "restore_scan", None)
        if restore_meter is not None:
            try:
                restore_meter()
            except Exception as restore_error:
                if scan_error is None or isinstance(scan_error, ScanStopped):
                    raise RuntimeError(f"RF 已关闭，但功率计原量程恢复失败：{restore_error}") from restore_error
                scan_error.add_note(f"RF 已关闭，但功率计原量程恢复失败：{restore_error}")
