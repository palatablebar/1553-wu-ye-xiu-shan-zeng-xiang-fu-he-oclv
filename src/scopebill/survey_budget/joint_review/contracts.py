from dataclasses import dataclass

# 复量测项编号：只有同作业面内、被多张签单覆盖且有共用证据登记的测项，
# 才允许在合查时合并占用；其余测项一律按单测照计工位。
MEASURE_IDS = (
    'extra_pct', 'revisions', 'photos', 'quote_age', 'works', 'vendor_lag',
)

# 合查覆盖风险向校准先验收缩时保留的最低信任度，避免先验覆盖全部信号。
MIN_SHRINK_TRUST = 0.08


@dataclass(frozen=True, slots=True)
class JointTicket:
    """单张签单在合查作业面上的覆盖登记。"""
    key: str
    cohort: str
    estimate: float
    cost_alone: int
    covered_measures: frozenset[str]
    solo_measures: frozenset[str]
    paused: bool
    pause_reason: str | None

    def __post_init__(self):
        if not self.covered_measures:
            raise ValueError('签单至少覆盖一个复量测项')
        if not self.covered_measures.issubset(MEASURE_IDS):
            raise ValueError('覆盖了未登记的复量测项')
        if self.solo_measures & self.covered_measures:
            raise ValueError('单测项与覆盖项不得重复登记')
        if self.cost_alone < 1:
            raise ValueError('单测工位占用至少为 1')
        if self.paused and not self.pause_reason:
            raise ValueError('暂停签单必须保留原因码')


@dataclass(frozen=True, slots=True)
class JointGroup:
    """同一作业面（cohort）内一次现场合查的覆盖与占用凭证。"""
    work_face: str
    tickets: tuple[JointTicket, ...]
    covered: frozenset[str]
    shared_measures: frozenset[str]
    solo_measures: frozenset[str]
    group_cost: int
    savings: int
    coverage_risk: float
    calibrated_risk: float
    state: str
    reason: str
    note: str

    def __post_init__(self):
        if len({ticket.key for ticket in self.tickets}) != len(self.tickets):
            raise ValueError('合查组内签单重复')
        # 共用测项必须落在每张成员签单的实际覆盖范围内。
        for measure in self.shared_measures:
            if any(measure not in ticket.covered_measures for ticket in self.tickets):
                raise ArithmeticError('共用测项超出成员签单的实际覆盖范围')
        if self.solo_measures & self.shared_measures:
            raise ValueError('单测项与共用项不得重叠')
        if self.group_cost < 1:
            raise ArithmeticError('合查组至少占用一个工位')
        if self.savings < 0:
            raise ArithmeticError('合查省位不得为负')
        if not 0.0 <= self.coverage_risk <= 1.0 or not 0.0 <= self.calibrated_risk <= 1.0:
            raise ValueError('合查风险必须落在概率区间')


@dataclass(frozen=True, slots=True)
class JointLedger:
    """凭证核对：覆盖签单与工位占用必须双向对得上。"""
    covered_keys: tuple[str, ...]
    occupied: int
    savings: int
    capacity: int
    balance: int
    paused_keys: tuple[str, ...]

    def __post_init__(self):
        if self.occupied + self.balance != self.capacity:
            raise ArithmeticError('合查工位占用与余额不平')
        if self.occupied < 0 or self.savings < 0 or self.balance < 0:
            raise ArithmeticError('合查台账出现负数')
        if len(self.covered_keys) != len(set(self.covered_keys)):
            raise ArithmeticError('合查凭证覆盖签单重复')


@dataclass(frozen=True, slots=True)
class JointPlan:
    groups: tuple[JointGroup, ...]
    singles: tuple[JointTicket, ...]
    ledger: JointLedger
    capacity: int
    policy_mode: str
    calibration_prior: float | None
    calibration_size: int

    def all_tickets(self):
        for group in self.groups:
            yield from group.tickets
        yield from self.singles
