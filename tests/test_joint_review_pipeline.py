from scopebill.pipeline import preview, execute, open_fixture


def test_pipeline_exposes_joint_review_voucher_with_conservation():
    report = preview()
    jr = report['joint_review']
    assert {item['label'] for item in jr['summary']} >= {
        '合查作业面', '合查共用省位', '合查实占工位', '合查后工位余额', '缺见证暂停签单',
    }
    ledger = {row['核对项']: row['数量'] for row in jr['ledger']}
    # 无外部共用证据登记时省位为 0，合查实占工位等于主派单已分派签单的工位之和
    assert ledger['共用省位'] == 0
    main_occupied = sum(row['cost'] for row in report['rows'] if row['state'] == '已分派')
    assert ledger['实占工位'] == main_occupied
    assert ledger['实占工位'] + ledger['工位余额'] == report['capacity']
    # 覆盖签单数与主派单已分派签单数一致（凭证双向核对）
    assert ledger['已覆盖签单'] == sum(1 for row in report['rows'] if row['state'] == '已分派')    # 每个作业面明细都带覆盖签单、共用/单测测项与双风险
    for face in jr['work_faces']:
        assert face['覆盖签单'] and face['作业面']
        assert set(face['共用测项']).isdisjoint(face['仍需单测项'])
        assert 0 <= face['覆盖风险'] <= 1 and 0 <= face['校准风险'] <= 1


def test_joint_review_is_deterministic_across_runs_and_order():
    first = preview()
    second = preview()
    assert first['joint_review'] == second['joint_review']
    dataset = open_fixture()
    shuffled = execute(list(reversed(dataset['records'])))
    assert shuffled['joint_review'] == first['joint_review']
    assert shuffled['run_id'] == first['run_id']


def test_no_witness_pause_surfaces_in_joint_ledger():
    report = preview()
    paused = next(item['value'] for item in report['joint_review']['summary']
                  if item['label'] == '缺见证暂停签单')
    assert paused >= 1
    # 暂停签单不得出现在任何已合查分派的覆盖签单中
    paused_jobs = {row['key'] for row in report['rows'] if row['state'] == '暂停'}
    covered = set()
    for face in report['joint_review']['work_faces']:
        if face['处置'] == '已合查分派':
            covered.update(face['覆盖签单'])
    assert paused_jobs.isdisjoint(covered)


def test_high_risk_work_face_has_priority_order():
    report = preview()
    order = report['joint_review']['risk_order']
    # 风险序为作业面到优先级的确定化映射；排名跨已分派/候补组全局唯一，
    # 数值越小代表校准后覆盖风险越高、工位紧张时越先合查。
    faces = {face['作业面'] for face in report['joint_review']['work_faces']}
    assert set(order) == faces
    assert len(set(order.values())) == len(order)
    highest = min(order, key=order.get)
    face_risk = {f['作业面']: f['校准风险'] for f in report['joint_review']['work_faces']}
    # 每个作业面取其最高风险组参与排序，排名第一的作业面风险不低于其他面
    top_risk = {}
    for face, risk in face_risk.items():
        top_risk[face] = max(top_risk.get(face, 0.0), risk)
    assert top_risk[highest] == max(top_risk.values())
