from collections import defaultdict
from math import fsum, isfinite

from .contracts import (
    JointGroup, JointLedger, JointPlan, JointTicket,
    MIN_SHRINK_TRUST,
)
from .coverage import coverage_declaration, proven_shared_measures


def _pause_reason(assessment):
    """NO_WITNESS 是签发硬规则：同场缺见证的签单即使工位紧张也必须暂停。"""
    hits = getattr(assessment, 'rule_hits', None)
    raw = assessment.raw
    if hits is None:
        # 退回量测口径自行判定，保持与 survey_budget.service.signals 一致。
        from ..service import signals
        hits = tuple(notice.rule_id for notice in signals(tuple(raw.input_values), assessment.estimate, raw.missing))
    if 'NO_WITNESS' in hits:
        return 'NO_WITNESS'
    return None


def build_joint_ticket(assessment, cost):
    raw = assessment.raw
    pause = _pause_reason(assessment)
    covered = coverage_declaration(raw.input_values, raw.missing)
    return JointTicket(
        key=raw.key,
        cohort=raw.cohort,
        estimate=round(assessment.estimate, 6),
        cost_alone=int(cost),
        covered_measures=covered,
        solo_measures=frozenset(),  # 组级统一核算，单票先不预占
        paused=pause is not None,
        pause_reason=pause,
    )


def coverage_weighted_risk(tickets):
    """合查风险按实际覆盖范围预测：覆盖测项越多，现场该票权重越高。"""
    weights = [max(1, len(ticket.covered_measures)) for ticket in tickets]
    total = sum(weights)
    return fsum(ticket.estimate * weight for ticket, weight in zip(tickets, weights, strict=True)) / total


def calibrate_joint_risk(risk, calibration_rows):
    """用独立校准窗实绩对合查风险做先验收缩与校准。

    calibration_rows 为校准窗内带成熟二元实绩的记录（含 outcome 字段）。
    样本不足或正负单一时不强行校准，保留原始覆盖风险并返回空先验，
    与 addendum_replay 的稀疏校准口径一致，转由人工判读兜底。
    """
    mature = [
        row for row in calibration_rows
        if getattr(row, 'observed', True) and getattr(row, 'outcome', None) in (0, 1)
    ]
    size = len(mature)
    positives = sum(1 for row in mature if row.outcome == 1)
    if size < 8 or positives in (0, size):
        return risk, None, size
    prior = (positives + 1) / (size + 2)
    scores = [getattr(row, 'score', getattr(row, 'estimate', prior)) for row in mature]
    spread = fsum((score - prior) ** 2 for score in scores)
    trust = min(1.0, max(MIN_SHRINK_TRUST, size / (size + 16.0)))
    if spread > 0:
        centered = fsum(
            (score - prior) * (row.outcome - prior)
            for score, row in zip(scores, mature, strict=True)
        )
        trust = min(1.0, max(MIN_SHRINK_TRUST, centered / (spread + 0.08 * size)))
    return risk * trust + prior * (1.0 - trust), prior, size


def _group_cost(tickets, shared):
    standalone = sum(ticket.cost_alone for ticket in tickets)
    # 每个证实共用的测项在该作业面只复量一次，省下一次重复占用；
    # 仍需单测的测项（solo）照常计占用，合查组至少保留一个工位。
    savings = min(standalone - 1, len(shared))
    return standalone - savings, savings


def build_joint_plan(assessments, plan, shared_evidence, *, mode, calibration_rows=()):
    """依据主派单方案在作业面粒度组建合查单。

    assessments: addendum_replay.Assessment 序列（当班）
    plan: survey_budget.Plan，主派单结果；合查准入完全跟随主方案，
          保证同一批重跑时覆盖签单与工位占用与主凭证一致。
    shared_evidence: {测项: (单号, ...)} 同场共用证据登记。
    """
    if mode not in ('balanced', 'focus'):
        raise ValueError('未知合查策略')

    by_key = {sample.raw.key: sample for sample in assessments}
    jobs = {job.key: job for job in plan.jobs}
    if set(by_key) != set(jobs):
        raise ArithmeticError('合查签单与主派单清单不一致')

    declared = {
        sample.raw.key: coverage_declaration(sample.raw.input_values, sample.raw.missing)
        for sample in assessments
    }
    shared = proven_shared_measures(declared, shared_evidence)

    tickets = {key: build_joint_ticket(by_key[key], jobs[key].cost) for key in by_key}
    paused = tuple(sorted(key for key, ticket in tickets.items() if ticket.paused))

    # 合查凭证只覆盖实际拿到工位的已分派签单；候补、暂停、人工判读、观察单
    # 不占合查工位。候补签单按作业面单独留组，供下一班高风险优先复排。
    admitted_members = defaultdict(list)
    waiting_members = defaultdict(list)
    for key in sorted(tickets):
        ticket = tickets[key]
        state = jobs[key].state
        if ticket.paused or state not in ('已分派', '候补'):
            continue
        (admitted_members if state == '已分派' else waiting_members)[ticket.cohort].append(ticket)

    groups = []
    priors = []
    cal_size = 0

    def assemble(work_face, members, admitted):
        nonlocal cal_size
        members = tuple(sorted(members, key=lambda item: item.key))
        covered = frozenset().union(*(ticket.covered_measures for ticket in members))
        face_shared = frozenset(
            measure for measure in shared
            if sum(measure in ticket.covered_measures for ticket in members) >= 2
        )
        solo = covered - face_shared
        reduced_cost, savings = _group_cost(members, face_shared)
        # 候补组尚未进场，按单测口径登记占用，省位只在实际合查分派时生效。
        group_cost = reduced_cost if admitted else sum(ticket.cost_alone for ticket in members)
        raw_risk = coverage_weighted_risk(members)
        if not isfinite(raw_risk):
            raise ValueError('合查覆盖风险必须有限')
        calibrated, prior, cal_size = calibrate_joint_risk(raw_risk, calibration_rows)
        if prior is not None and admitted:
            priors.append(prior)
        if admitted:
            state, reason, note = '已合查分派', 'JOINT_ADMITTED', '跟随主派单整组合查，覆盖签单与工位占用双向核对'
        else:
            state, reason = '合查候补', 'MEMBER_WAITING'
            note = '工位只够合查时按高风险作业面优先，组内待工位签单整组待命'
        return JointGroup(
            work_face=work_face,
            tickets=members,
            covered=covered,
            shared_measures=face_shared,
            solo_measures=solo,
            group_cost=group_cost,
            savings=savings if admitted else 0,
            coverage_risk=round(raw_risk, 6),
            calibrated_risk=round(calibrated, 6),
            state=state,
            reason=reason,
            note=note,
        )

    for work_face in sorted(set(admitted_members) | set(waiting_members)):
        if admitted_members.get(work_face):
            groups.append(assemble(work_face, admitted_members[work_face], True))
        if waiting_members.get(work_face):
            groups.append(assemble(work_face, waiting_members[work_face], False))

    # 工位只够合查时先查高风险作业面：按校准后覆盖风险降序输出排序键，
    # 全部键确定化，保证同一批重跑结论一致；输出仍按作业面稳定排列。
    risk_order = {
        group.work_face: rank
        for rank, group in enumerate(sorted(groups, key=lambda g: (-g.calibrated_risk, g.work_face)))
    }
    groups.sort(key=lambda group: group.work_face)

    admitted = [group for group in groups if group.state == '已合查分派']
    occupied = sum(group.group_cost for group in admitted)
    admitted_keys = tuple(sorted(key for group in admitted for ticket in group.tickets
                                 for key in (ticket.key,)))
    total_savings = sum(group.savings for group in groups)

    # 工位守恒：主方案实占 = 合查实占 + 共用省位；余额随之增加省位。
    main_occupied = sum(job.cost for job in plan.jobs if job.state == '已分派')
    if occupied + total_savings != main_occupied:
        raise ArithmeticError(
            '合查工位与主派单占用不平：合查 %d + 省位 %d != 主占 %d'
            % (occupied, total_savings, main_occupied)
        )

    ledger = JointLedger(
        covered_keys=admitted_keys,
        occupied=occupied,
        savings=total_savings,
        capacity=plan.capacity,
        balance=plan.capacity - occupied,
        paused_keys=paused,
    )
    return JointPlan(
        groups=tuple(groups),
        singles=(),
        ledger=ledger,
        capacity=plan.capacity,
        policy_mode=mode,
        calibration_prior=round(fsum(priors) / len(priors), 6) if priors else None,
        calibration_size=cal_size,
    ), risk_order


def joint_review_plan(assessments, plan, *, mode, shared_evidence=None, calibration_rows=()):
    """对外入口：无共用证据时按纯单测占用组建合查面（savings 恒为 0）。"""
    joint, risk_order = build_joint_plan(
        assessments, plan, shared_evidence or {},
        mode=mode, calibration_rows=calibration_rows,
    )
    return joint, risk_order
