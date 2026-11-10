"""Read-only recovery of measurement CSVs and atomic session metadata."""
import csv
import json
import math
from pathlib import Path

from scan import ScanPoint


def session_path(csv_path):
    return Path(csv_path).with_suffix('.session.json')


def save_session(csv_path, settings, state, sources=(), error=None):
    destination = session_path(csv_path)
    payload = dict(version=1, csv=str(Path(csv_path).resolve()), settings=settings,
                   state=state, sources=[str(Path(p).resolve()) for p in sources], error=error)
    temporary = destination.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(destination)


def read_session(csv_path):
    payload = json.loads(session_path(csv_path).read_text(encoding='utf-8'))
    if payload.get('version') != 1 or not isinstance(payload.get('settings'), dict):
        raise ValueError('不支持的测量会话文件。')
    return payload


def _number(row, name, optional=False):
    text = row.get(name)
    if optional and text in (None, ''):
        return None
    try:
        value = float(text)
    except (TypeError, ValueError) as error:
        raise ValueError(f'测量记录的 {name} 缺失或不是数值。') from error
    if not math.isfinite(value):
        raise ValueError(f'测量记录的 {name} 不是有限数值。')
    return value


def point_from_row(row):
    rising = row.get('rising_to_limit', '').lower()
    if rising not in ('', 'true', 'false'):
        raise ValueError('上限处上升证据标记无效。')
    trials = _number(row, 'trials')
    if trials < 1 or trials != int(trials) or not row.get('status'):
        raise ValueError('测量记录缺少有效状态或采集次数。')
    point = ScanPoint(
        frequency_mhz=_number(row, 'frequency_mhz'),
        amplitude_mvpp=_number(row, 'amplitude_mvpp', True),
        power_mw=_number(row, 'output_power_mw', True),
        std_mw=_number(row, 'power_std_mw', True),
        status=row['status'], trials=int(trials),
        best_attempt_mvpp=_number(row, 'best_attempt_mvpp', True),
        rollover_amplitude_mvpp=_number(row, 'rollover_amplitude_mvpp', True),
        rollover_power_mw=_number(row, 'rollover_power_mw', True),
        reason=row.get('reason', ''),
        verification_error_mw=_number(row, 'verification_error_mw', True),
        maximum_kind=row.get('maximum_kind', ''),
        tested_min_mvpp=_number(row, 'tested_min_mvpp', True),
        tested_max_mvpp=_number(row, 'tested_max_mvpp', True),
        peak_bracket_low_mvpp=_number(row, 'peak_bracket_low_mvpp', True),
        peak_bracket_high_mvpp=_number(row, 'peak_bracket_high_mvpp', True),
        rising_to_limit=rising == 'true',
    )
    if point.status not in {'ok','unreachable','below_range','unstable','no_bracket','rollover',
                            'overrange','maximum_below_target','maximum_unconfirmed'}:
        raise ValueError('测量记录包含未知状态，不能自动跳过该频点。')
    if point.std_mw is not None and point.std_mw < 0:
        raise ValueError('测量标准差不能为负。')
    if point.status == 'maximum_below_target' and point.maximum_kind not in ('local_peak','rf_limit'):
        raise ValueError('最大值记录缺少确认类型。')
    if point.status in ('ok', 'maximum_below_target') and (point.amplitude_mvpp is None or point.power_mw is None):
        raise ValueError('已完成测量缺少振幅与功率配对数据。')
    if point.maximum_kind and (point.tested_min_mvpp is None or point.tested_max_mvpp is None):
        raise ValueError('峰值记录缺少已测范围。')
    if point.rising_to_limit and (point.status != 'maximum_below_target' or point.maximum_kind != 'rf_limit'):
        raise ValueError('只有已复测的 RF 上限结果可以携带上升证据。')
    return point


def load_measurements(paths, config, settings, simulate, *, meter_serial=None, generator=None):
    """Merge by frequency, newest file wins, retaining original provenance.

    CSVs are never written. Reject mismatched experiments rather than quietly
    treating them as already measured points of the current experiment.
    """
    grid = config.frequencies_mhz()
    merged = {}
    identity = None
    for path in sorted(map(Path, paths), key=lambda p: p.name):
        with path.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames or 'frequency_mhz' not in reader.fieldnames or 'status' not in reader.fieldnames:
                raise ValueError(f'{path.name} 不是测量结果 CSV（不能选择 trials 文件）。')
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError(f'{path.name} 有未写完的数据行，请等保存完成。')
                point = point_from_row(row)
                frequency = next((f for f in grid if math.isclose(f, point.frequency_mhz, abs_tol=1e-7, rel_tol=0)), None)
                if frequency is None:
                    raise ValueError(f'{point.frequency_mhz:g} MHz 不属于当前扫描网格。')
                expected = dict(target_power_mw=config.target_mw, tolerance_mw=config.tolerance_mw,
                                wavelength_nm=float(settings['wavelength']))
                for field, value in expected.items():
                    if not math.isclose(_number(row, field), value, rel_tol=1e-9, abs_tol=1e-9):
                        raise ValueError(f'{path.name} 的 {field} 与当前设置不同，不能混为同一轮续扫。')
                flag = row.get('simulation', '').lower()
                if flag not in ('true', 'false') or (flag == 'true') != simulate:
                    raise ValueError('不能混合模拟数据与实验数据。')
                if row.get('target_mode') != settings['mode']:
                    raise ValueError('恢复数据的功率/效率参考方式不同。')
                reference = row.get('incident_mw', '')
                configured_reference = settings.get('incident', '')
                if bool(reference) != bool(configured_reference) or (reference and not math.isclose(float(reference), float(configured_reference))):
                    raise ValueError('恢复数据的入射参考功率不同。')
                for amplitude in (point.amplitude_mvpp, point.best_attempt_mvpp):
                    if amplitude is not None and not config.min_mvpp <= amplitude <= config.max_mvpp:
                        raise ValueError('恢复数据中的振幅超过当前 RF 安全范围。')
                current_identity = row.get('meter_serial'), row.get('generator')
                if identity is not None and current_identity != identity:
                    raise ValueError('恢复文件使用的仪器不同，不能自动合并。')
                identity = current_identity
                if meter_serial is not None and current_identity[0] != meter_serial:
                    raise ValueError('当前功率计与恢复数据中的功率计不同。')
                if generator is not None and current_identity[1] != generator:
                    raise ValueError('当前发生器与恢复数据中的发生器不同。')
                row['source_csv'] = row.get('source_csv') or str(path.resolve())
                row['restored'] = 'True'
                merged[frequency] = (point, row)
    ordered = [merged[f] for f in grid if f in merged]
    return [pair[0] for pair in ordered], [pair[1] for pair in ordered]
