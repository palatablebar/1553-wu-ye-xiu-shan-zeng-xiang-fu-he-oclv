from dataclasses import replace
from types import SimpleNamespace

import pytest

from scopebill.addendum_replay.contracts import Assessment
from scopebill.change_scorecard.contracts import Prediction
from scopebill.survey_budget.contracts import Job, Plan
from scopebill.survey_budget.joint_review import (
    JointGroup, JointTicket, MEASURE_IDS,
    coverage_declaration, joint_review_plan,
    proven_shared_measures, reconcile, reject_leaking_work_faces,
)
from scopebill.survey_budget.joint_review.service import (
    build_joint_ticket, calibrate_joint_risk, coverage_weighted_risk,
)

VALUES = (46.0, 7.0, 21.0, 43.5, 19.5, 36.0)
FIELDS = ('extra_pct', 'revisions', 'photos', 'quote_age', 'works', 'vendor_lag')


def make_assessment(key, cohort='甲区', values=VALUES, estimate=0.7, missing=()):
    raw = Prediction(key=key, cohort=cohort, day=90, score=estimate,
                     contributions=(0.1,) * 5, missing=tuple(missing),
                     excursions=(), outcome=None, observed=True,
                     input_values=tuple(values))
    return Assessment(raw, estimate, None, None, False)


def make_job(key, cohort='甲区', cost=2, state='已分派', reason='ABOVE_TRIGGER'):
    return Job(key=key, cohort=cohort, estimate=0.7, lower=None, upper=None,
               cost=cost, state=state, reason=reason, summary='s', priority=0.5,
               rule_hits=())


def make_plan(jobs, capacity=12):
    used = sum(job.cost for job in jobs if job.state == '已分派')
    groups = {}
    for job in jobs:
        if job.state == '已分派':
            groups[job.cohort] = groups.get(job.cohort, 0) + job.cost
    return Plan(jobs=tuple(jobs), capacity=capacity, balance=capacity - used,
                used_by_group=tuple(sorted(groups.items())))


def ticket(assess, job):
    return build_joint_ticket(assess, job.cost)


def build(assessments, plan, shared=None, calibration_rows=(), mode='balanced'):
    return joint_review_plan(assessments, plan, mode=mode,
                             shared_evidence=shared or {},
                             calibration_rows=calibration_rows)[0]


# --- 规则 1：凭证覆盖签单与实占工位双向核对 ---------------------------------

def test_voucher_covers_every_dispatched_ticket_and_station():
    a = [make_assessment('A'), make_assessment('B')]
    plan = make_plan([make_job('A', cost=2), make_job('B', cost=3)])
    jp = build(a, plan)
    detail = reconcile(jp, plan)
    assert frozenset(jp.ledger.covered_keys) == {'A', 'B'}
    assert jp.ledger.occupied == 5
    assert jp.ledger.occupied + jp.ledger.balance == plan.capacity
    assert {key for row in detail for key in row['覆盖签单']} == {'A', 'B'}


def test_reconcile_detects_cover_and_occupation_mismatch():
    a = [make_assessment('A'), make_assessment('B')]
    plan = make_plan([make_job('A', cost=2), make_job('B', cost=3)])
    jp = build(a, plan)
    # 篡改覆盖签单集合：覆盖与主派单不再一致
    tampered = replace(jp, ledger=replace(jp.ledger, covered_keys=('A',)))
    with pytest.raises(ArithmeticError, match='覆盖签单'):
        reconcile(tampered, plan)
    # 篡改占用工位与余额（台账内部自洽），但与主派单守恒关系被破坏
    bad_occ = replace(jp, ledger=replace(jp.ledger, occupied=4, balance=8))
    with pytest.raises(ArithmeticError, match='工位'):
        reconcile(bad_occ, plan)


# --- 规则 2：工位紧张先查高风险作业面，缺见证签单仍须暂停 ---------------------

def test_no_witness_ticket_is_paused_and_never_occupies_station(prediction):
    # photos=2 且 extra_pct=78，命中 NO_WITNESS 硬暂停
    raw = replace(prediction, key='PAUSE', input_values=(78, 8, 2, 22, 18, 24),
                  missing=())
    assess = Assessment(raw, 0.9, None, None, False)
    job = replace(make_job('PAUSE', cost=2), state='暂停', reason='NO_WITNESS',
                  rule_hits=('NO_WITNESS',))
    plan = make_plan([job])
    jp = build([assess], plan)
    assert jp.ledger.paused_keys == ('PAUSE',)
    assert jp.ledger.occupied == 0 and jp.ledger.covered_keys == ()
    assert all('PAUSE' not in group.tickets[0].key or group.state != '已合查分派'
               for group in jp.groups)


def test_high_risk_work_face_ranks_first_when_capacity_tight():
    low = [make_assessment(f'L{i}', cohort='低风险面', estimate=0.2) for i in range(2)]
    high = [make_assessment(f'H{i}', cohort='高风险面', estimate=0.95) for i in range(2)]
    jobs = [make_job('L0', '低风险面'), make_job('L1', '低风险面'),
            make_job('H0', '高风险面'), make_job('H1', '高风险面')]
    plan = make_plan(jobs, capacity=8)
    jp, order = joint_review_plan(low + high, plan, mode='balanced')
    assert order['高风险面'] < order['低风险面']
    by_face = {g.work_face: g for g in jp.groups if g.state == '已合查分派'}
    assert by_face['高风险面'].calibrated_risk > by_face['低风险面'].calibrated_risk


# --- 规则 3：只有证实共用的测项才省工位，单测照常计占用 -----------------------

def test_shared_measure_saves_station_only_with_evidence():
    a = [make_assessment('A'), make_assessment('B')]
    plan = make_plan([make_job('A', cost=2), make_job('B', cost=2)])
    jp = build(a, plan, shared={'works': ('A', 'B')})
    group = next(g for g in jp.groups if g.state == '已合查分派')
    assert group.shared_measures == frozenset({'works'})
    assert group.savings == 1 and group.group_cost == 3
    assert 'works' not in group.solo_measures
    assert jp.ledger.occupied == 3 and jp.ledger.savings == 1
    # 守恒：合查实占 + 省位 = 主方案占用
    assert jp.ledger.occupied + jp.ledger.savings == 4


def test_solo_measures_keep_full_occupation_without_evidence():
    a = [make_assessment('A'), make_assessment('B')]
    plan = make_plan([make_job('A', cost=2), make_job('B', cost=2)])
    jp = build(a, plan, shared={})
    group = next(g for g in jp.groups if g.state == '已合查分派')
    assert group.shared_measures == frozenset()
    assert group.solo_measures == frozenset(MEASURE_IDS)
    assert group.savings == 0 and group.group_cost == 4


def test_unproven_shared_claim_is_rejected_or_counted_as_solo():
    declared = {'A': frozenset(MEASURE_IDS), 'B': frozenset(MEASURE_IDS)}
    with pytest.raises(ArithmeticError):
        # 证据声称 B 覆盖 works，但 B 实际不覆盖该测项
        proven_shared_measures(
            {'A': frozenset(MEASURE_IDS), 'B': frozenset(('photos',))},
            {'works': ('A', 'B')},
        )
    proven = proven_shared_measures(declared, {'works': ('A', 'B')})
    assert proven == frozenset({'works'})
    with pytest.raises(ValueError):
        proven_shared_measures(declared, {'works': ('A',)})  # 单票不成共用


def test_group_cost_never_drops_below_one_station():
    assessments = [make_assessment(f'K{i}', VALUES, estimate=0.6) for i in range(3)]
    # 每张票 cost_alone=1，即便六个测项全部共用，组仍至少占 1 工位
    jobs = [make_job(f'K{i}', cost=1) for i in range(3)]
    plan = make_plan(jobs, capacity=6)
    shared = {measure: ('K0', 'K1', 'K2') for measure in MEASURE_IDS}
    jp = build(assessments, plan, shared=shared)
    group = next(g for g in jp.groups if g.state == '已合查分派')
    assert group.group_cost == 1 and group.savings == 2


# --- 规则 4：合查风险按实际覆盖范围预测并用校准窗校准 -------------------------

def test_coverage_risk_weights_actual_covered_measures():
    wide = make_assessment('W', estimate=0.9)
    narrow_assess = make_assessment('N', estimate=0.3)
    narrow = replace(build_joint_ticket(narrow_assess, 2),
                     covered_measures=frozenset(('photos',)))
    wide_ticket = build_joint_ticket(wide, 2)
    risk = coverage_weighted_risk((wide_ticket, narrow))
    # 覆盖 6 项的高风险票权重高于只覆盖 1 项的低风险票
    assert risk > (0.9 + 0.3) / 2


def _calib_rows(n=12, positives=6):
    rows = []
    for i in range(n):
        rows.append(SimpleNamespace(observed=True, outcome=1 if i < positives else 0,
                                    score=0.8 if i < positives else 0.2))
    return rows


def test_calibration_shrinks_risk_toward_prior_and_sparse_falls_back():
    rows = _calib_rows()
    calibrated, prior, size = calibrate_joint_risk(0.9, rows)
    assert prior is not None and size == 12
    assert calibrated < 0.9  # 向先验收缩
    sparse, none_prior, sparse_size = calibrate_joint_risk(0.9, _calib_rows(4, 2))
    assert none_prior is None and sparse == 0.9
    all_positive, p2, _ = calibrate_joint_risk(0.9, _calib_rows(12, 12))
    assert p2 is None and all_positive == 0.9


# --- 规则 5：同一作业面实绩不能跨训练与校准窗 ---------------------------------

def _history_row(key, cohort, outcome=1, observed=True, values=VALUES):
    return SimpleNamespace(key=key, cohort=cohort, observed=observed, outcome=outcome,
                           extra_pct=values[0], revisions=values[1], photos=values[2],
                           quote_age=values[3], works=values[4], vendor_lag=values[5])


def test_leakage_guard_passes_when_only_cohort_names_overlap():
    train = [_history_row('T1', '同区'), _history_row('T2', '同区', outcome=0)]
    calib = [_history_row('C1', '同区', values=(10, 1, 5, 10, 5, 10))]
    assert reject_leaking_work_faces(train, calib) == ()


def test_leakage_guard_rejects_same_key_across_windows():
    train = [_history_row('DUP', '同区')]
    calib = [_history_row('DUP', '同区', values=(10, 1, 5, 10, 5, 10))]
    with pytest.raises(ArithmeticError, match='跨训练与校准'):
        reject_leaking_work_faces(train, calib)


def test_leakage_guard_rejects_identical_readings_in_same_work_face():
    train = [_history_row('T1', '同区', values=VALUES)]
    calib = [_history_row('C9', '同区', values=VALUES)]  # 不同单号、同读数
    with pytest.raises(ArithmeticError, match='跨训练与校准'):
        reject_leaking_work_faces(train, calib)


def test_immature_records_are_not_treated_as_leakage():
    train = [_history_row('T1', '同区')]
    calib = [_history_row('C1', '同区', outcome=None, observed=False)]
    assert reject_leaking_work_faces(train, calib) == ()


# --- 规则 6：同一批重跑，覆盖签单与工位占用保持一致 ---------------------------

def test_rerun_is_identical_under_shuffled_inputs():
    assessments = [make_assessment(f'A{i}', cohort='甲区' if i % 2 else '乙区',
                                   estimate=0.3 + 0.05 * i) for i in range(6)]
    jobs = [make_job(f'A{i}', '甲区' if i % 2 else '乙区', cost=2) for i in range(6)]
    plan = make_plan(jobs, capacity=20)
    shared = {'works': tuple(f'A{i}' for i in range(6))}
    first, order1 = joint_review_plan(assessments, plan, mode='focus',
                                      shared_evidence=shared,
                                      calibration_rows=_calib_rows())
    second, order2 = joint_review_plan(tuple(reversed(assessments)), plan, mode='focus',
                                       shared_evidence=shared,
                                       calibration_rows=list(reversed(_calib_rows())))
    assert first == second and order1 == order2
    assert first.ledger.covered_keys == tuple(sorted(first.ledger.covered_keys))


def test_coverage_declaration_excludes_missing_measures():
    covered = coverage_declaration(VALUES, missing=('photos', 'quote_age'))
    assert 'photos' not in covered and 'quote_age' not in covered
    assert covered == frozenset(MEASURE_IDS) - {'photos', 'quote_age'}
    with pytest.raises(ValueError):
        coverage_declaration(VALUES, missing=('unknown_field',))


def test_joint_ticket_contract_validates_coverage():
    assess = make_assessment('A')
    good = build_joint_ticket(assess, 2)
    assert isinstance(good, JointTicket)
    with pytest.raises(ValueError):
        JointTicket(key='X', cohort='c', estimate=0.5, cost_alone=1,
                    covered_measures=frozenset(), solo_measures=frozenset(),
                    paused=False, pause_reason=None)
    with pytest.raises(ValueError, match='重复'):
        JointGroup(work_face='c', tickets=(good, good),
                   covered=good.covered_measures, shared_measures=frozenset(),
                   solo_measures=frozenset(), group_cost=1, savings=0,
                   coverage_risk=0.5, calibrated_risk=0.5,
                   state='已合查分派', reason='r', note='n')
