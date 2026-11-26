from .contracts import MEASURE_IDS


def coverage_declaration(measurements, missing=(), *, extra_covered=None):
    """由单据量测推导覆盖测项，并允许登记额外的现场覆盖项。

    规则：
    - 已读数（未缺测）的测项默认在现场覆盖范围内；
    - 缺测项不得计入覆盖，防止把空置字段当成可合查测项；
    - extra_covered 用于登记单据量测之外、但现场确实要复量的测项，
      仍须是 MEASURE_IDS 内的合法编号。
    """
    absent = frozenset(missing)
    if absent - frozenset(MEASURE_IDS):
        raise ValueError('缺测字段含未登记测项')
    covered = {name for name in MEASURE_IDS if name not in absent}
    if extra_covered:
        unknown = frozenset(extra_covered) - frozenset(MEASURE_IDS)
        if unknown:
            raise ValueError('额外覆盖项含未登记测项：' + '、'.join(sorted(unknown)))
        covered.update(extra_covered)
    return frozenset(covered)


def proven_shared_measures(declared_cover, shared_evidence):
    """核对共用证据，返回真正证实可合并的测项。

    declared_cover: {单号: frozenset(覆盖测项)}
    shared_evidence: {测项: frozenset(单号)}，表示该测项存在同场共用证据，
                     且证据实际覆盖列出的签单。

    只有同时满足以下条件才认定为共用、允许省工位：
    1. 测项编号合法；
    2. 证据至少覆盖两张签单（单张签单无所谓“共用”）；
    3. 被列入证据的每张签单都实际覆盖该测项；
    4. 该测项至少被组内两张签单覆盖。
    其余测项仍按单测照常计占用。
    """
    declared = dict(declared_cover)
    proven = {}
    for measure, witness_keys in shared_evidence.items():
        if measure not in MEASURE_IDS:
            raise ValueError('共用证据引用了未登记测项：' + str(measure))
        keys = frozenset(witness_keys)
        if len(keys) < 2:
            raise ValueError('共用证据至少覆盖两张签单：' + measure)
        unknown = keys - frozenset(declared)
        if unknown:
            raise KeyError('共用证据引用了未登记签单：' + '、'.join(sorted(unknown)))
        if any(measure not in declared[key] for key in keys):
            raise ArithmeticError('共用证据超出签单实际覆盖范围：' + measure)
        covering = {key for key in declared if measure in declared[key]}
        if len(covering & keys) < 2:
            raise ArithmeticError('共用测项未被两张以上签单覆盖：' + measure)
        proven[measure] = frozenset(keys)
    return frozenset(proven)
