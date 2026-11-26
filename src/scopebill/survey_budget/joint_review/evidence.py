def reconcile(joint_plan, main_plan):
    """凭证核对：覆盖签单和实占工位必须双向对得上。

    以主派单方案为基准核对合查凭证：
    - 合查已覆盖签单必须与主方案已分派签单完全一致；
    - 合查实占工位 + 共用省位 = 主方案实占工位；
    - 缺见证暂停签单两边都不得占用工位。
    返回逐条作业面明细，任一不符直接抛出 ArithmeticError，禁止签发。
    """
    main_dispatched = frozenset(
        job.key for job in main_plan.jobs if job.state == '已分派'
    )
    main_occupied = sum(job.cost for job in main_plan.jobs if job.state == '已分派')
    joint_covered = frozenset(joint_plan.ledger.covered_keys)
    paused = frozenset(joint_plan.ledger.paused_keys)

    missing_in_joint = main_dispatched - joint_covered
    extra_in_joint = joint_covered - main_dispatched
    if missing_in_joint or extra_in_joint:
        raise ArithmeticError(
            '合查覆盖签单与主派单不一致：缺 %s，多 %s'
            % (sorted(missing_in_joint), sorted(extra_in_joint))
        )
    if paused & joint_covered:
        raise ArithmeticError('缺见证暂停签单不得占用合查工位')

    if joint_plan.ledger.occupied + joint_plan.ledger.savings != main_occupied:
        raise ArithmeticError(
            '合查实占工位与覆盖范围不符：凭证实占 %d + 省位 %d != 主占 %d'
            % (joint_plan.ledger.occupied, joint_plan.ledger.savings, main_occupied)
        )
    if joint_plan.ledger.occupied + joint_plan.ledger.balance != joint_plan.capacity:
        raise ArithmeticError('合查工位余额不平')

    # 组内凭证自洽：每组的覆盖签单、共用/单测测项与省位可逐项复核。
    rows = []
    for group in joint_plan.groups:
        standalone = sum(ticket.cost_alone for ticket in group.tickets)
        expected_cost = standalone - group.savings
        if expected_cost != group.group_cost:
            raise ArithmeticError('作业面 %s 省位与工位占用对不上' % group.work_face)
        if group.shared_measures & group.solo_measures:
            raise ArithmeticError('作业面 %s 共用项与单测项重叠' % group.work_face)
        rows.append({
            '作业面': group.work_face,
            '覆盖签单': tuple(ticket.key for ticket in group.tickets),
            '共用测项': tuple(sorted(group.shared_measures)),
            '仍需单测项': tuple(sorted(group.solo_measures)),
            '单测合计工位': standalone,
            '省工位': group.savings,
            '实占工位': group.group_cost if group.state == '已合查分派' else 0,
            '覆盖风险': group.coverage_risk,
            '校准风险': group.calibrated_risk,
            '处置': group.state,
            '原因': group.reason,
            '说明': group.note,
        })
    return rows


def joint_ledger_rows(joint_plan):
    """额度台账：占用、余额、省位、暂停逐项可核对。"""
    return [
        {'核对项': '合查作业面', '数量': len(joint_plan.groups)},
        {'核对项': '已覆盖签单', '数量': len(joint_plan.ledger.covered_keys)},
        {'核对项': '实占工位', '数量': joint_plan.ledger.occupied},
        {'核对项': '共用省位', '数量': joint_plan.ledger.savings},
        {'核对项': '工位余额', '数量': joint_plan.ledger.balance},
        {'核对项': '缺见证暂停签单', '数量': len(joint_plan.ledger.paused_keys)},
    ]


def joint_evidence(joint_plan, main_plan):
    """合查环节凭证：先做双向核对，再输出作业面明细与台账。"""
    detail = reconcile(joint_plan, main_plan)
    return detail, joint_ledger_rows(joint_plan)
