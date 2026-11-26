from collections import defaultdict

_MEASURE_KEYS = ('extra_pct', 'revisions', 'photos', 'quote_age', 'works', 'vendor_lag')


def _field(row, name):
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name)


def _measure_fingerprint(row):
    """同一作业面内量测读数完全一致的实绩视为同一份现场成果。"""
    if isinstance(row, dict) or hasattr(row, _MEASURE_KEYS[0]):
        return tuple(_field(row, key) for key in _MEASURE_KEYS)
    # FeatureVector 等向量化表示只保留 input_values 元组，按固定字段序取值。
    return tuple(getattr(row, 'input_values'))


def work_faces_per_window(train_rows, calibration_rows):
    """汇总每个窗口、每个作业面出现的成熟实绩单号与量测指纹。

    只有带成熟标签（observed 且 outcome 非空）的记录才参与统计，
    与拟合/校准实际取用的样本口径保持一致，避免把未成熟单误判为泄漏。
    作业面（cohort）本身跨窗出现是正常的，守卫针对的是实绩复用，
    而不是作业面名称重叠。
    """
    def mature(row):
        return bool(_field(row, 'observed')) and _field(row, 'outcome') is not None

    faces = {
        'train': defaultdict(lambda: {'keys': set(), 'fingerprints': defaultdict(set)}),
        'calibration': defaultdict(lambda: {'keys': set(), 'fingerprints': defaultdict(set)}),
    }
    for window, rows in (('train', train_rows), ('calibration', calibration_rows)):
        for row in rows:
            if not mature(row):
                continue
            cohort = _field(row, 'cohort')
            faces[window][cohort]['keys'].add(_field(row, 'key'))
            faces[window][cohort]['fingerprints'][_measure_fingerprint(row)].add(_field(row, 'key'))
    return faces


def reject_leaking_work_faces(train_rows, calibration_rows):
    """同一作业面的实绩同时落入训练窗与校准窗即拒绝，防止样本泄漏。

    判定为泄漏的两类实绩复用：
    1. 同一单号在两个窗口都出现成熟实绩（时间窗半开，正常分窗不应发生）；
    2. 同一作业面内量测指纹完全相同的实绩跨窗出现（同一份现场成果被复用）。
    仅作业面名称重叠、但单据与读数都不同的，不算泄漏。
    """
    faces = work_faces_per_window(train_rows, calibration_rows)
    leaks = []
    for cohort in sorted(set(faces['train']) & set(faces['calibration'])):
        train_keys = faces['train'][cohort]['keys']
        cal_keys = faces['calibration'][cohort]['keys']
        shared_keys = train_keys & cal_keys
        shared_readings = set(faces['train'][cohort]['fingerprints']) & set(faces['calibration'][cohort]['fingerprints'])
        if not shared_keys and not shared_readings:
            continue
        reading_tickets = tuple(sorted(
            key for fingerprint in shared_readings
            for key in faces['calibration'][cohort]['fingerprints'][fingerprint]
        ))
        leaks.append({
            '作业面': cohort,
            '跨窗单号': tuple(sorted(shared_keys)),
            '同读数校准窗签单': reading_tickets,
        })
    if leaks:
        raise ArithmeticError(
            '同一作业面实绩跨训练与校准窗口：' + '、'.join(item['作业面'] for item in leaks)
        )
    return tuple(leaks)
