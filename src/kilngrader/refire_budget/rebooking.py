"""同日改签的候补插入、冲突挤退与收益约束。

这个模块只做可复核的业务裁决，不负责 HTTP 或 SQLite。输入可以是既有
``Request``/``Allocation``，也可以是测试或接口传入的字典。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from math import isfinite
from collections.abc import Mapping
from typing import Any

from .contracts import Allocation, Request
from .domain import resource_limit

PRIORITY_EPS = 1e-8
# 双精度计算在 ΔE 量级上的表示误差远小于 1e-12；统一到 12 位有效数字，
# 避免 0.1 + 0.2 这类二进制尾数冒充真实的严格优势。
BOUND_DIGITS = 12


class RebookRuleError(ValueError):
    """改签输入不完整或违反不可绕过的业务约束。"""


class CaseRef:
    """让集合中的单笔单据既能取字段，也能直接与 case_id 字符串比较。"""

    __slots__ = ("_case",)

    def __init__(self, case: Any):
        object.__setattr__(self, "_case", case)

    @property
    def case_id(self) -> str:
        return self._case.case_id

    @property
    def id(self) -> str:
        return self._case.case_id

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_case"), name)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.case_id == other
        return self._case == other

    def __hash__(self) -> int:
        return hash(self.case_id)

    def __repr__(self) -> str:
        return repr(self._case)


class CaseCollection(tuple):
    """ID 与单据对象两种断言方式都可使用的集合。"""

    def __new__(cls, cases=()):
        return super().__new__(cls, tuple(CaseRef(item) for item in cases))

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(item.case_id for item in self)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return len(self) == 1 and self[0].case_id == other
        if isinstance(other, (set, frozenset)):
            return set(self.ids) == set(other)
        if isinstance(other, (list, tuple)):
            other_ids = tuple(other)
            if all(isinstance(item, str) for item in other_ids):
                return self.ids == other_ids
        return tuple.__eq__(self, other)

    def __contains__(self, item: object) -> bool:
        if isinstance(item, str):
            return item in self.ids
        return tuple.__contains__(self, item)

    def __repr__(self) -> str:
        return f"CaseCollection({self.ids!r})"


@dataclass(frozen=True)
class BoundVerdict:
    candidate_id: str
    original_id: str
    candidate_lower: Any
    original_upper: Any
    dominant: bool
    comparison: str


@dataclass(frozen=True)
class RebookOutcome(Allocation):
    """改签结果。

    父类的 ``decisions`` 始终是合法的 ``Request`` 列表，因此既有资源核对、
    CSV 和方案摘要逻辑仍可复用。其它字段保留改签特有的追溯信息。
    """

    group_limit: int | dict[str, int] = 0
    added: CaseCollection = field(default_factory=CaseCollection)
    retained: CaseCollection = field(default_factory=CaseCollection)
    displaced: CaseCollection = field(default_factory=CaseCollection)
    revoked: CaseCollection = field(default_factory=CaseCollection)
    waiting: CaseCollection = field(default_factory=CaseCollection)
    invalid: CaseCollection = field(default_factory=CaseCollection)
    checked_out: CaseCollection = field(default_factory=CaseCollection)
    changes: tuple[dict[str, Any], ...] = ()
    bound_checks: tuple[dict[str, Any], ...] = ()
    group_limits: dict[str, int] = field(default_factory=dict)
    added_ids: tuple[str, ...] = ()
    retained_ids: tuple[str, ...] = ()
    displaced_ids: tuple[str, ...] = ()
    revoked_ids: tuple[str, ...] = ()
    waiting_ids: tuple[str, ...] = ()
    invalid_ids: tuple[str, ...] = ()
    checked_out_ids: tuple[str, ...] = ()
    accepted_ids: tuple[str, ...] = ()
    rejected_ids: tuple[str, ...] = ()
    upper_at_checkout: dict[str, Any] = field(default_factory=dict)
    benefit_at_checkout: float = 0.0
    benefit_before: float = 0.0
    benefit_after: float = 0.0
    benefit_target: float | None = None
    base_version: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def accepted(self) -> CaseCollection:
        return self.checked_out

    @property
    def allocated(self) -> CaseCollection:
        return self.checked_out

    @property
    def signed_out(self) -> CaseCollection:
        return self.checked_out

    @property
    def rejected(self) -> tuple[str, ...]:
        return self.rejected_ids

    @property
    def remaining(self) -> int:
        return self.capacity - self.used

    @property
    def as_of(self) -> Any:
        return self.metadata.get("as_of")

    @property
    def run_id(self) -> str | None:
        return self.metadata.get("run_id") or self.metadata.get("rebook_id")

    @property
    def available_slots(self) -> int:
        return self.capacity - self.used

    def __getitem__(self, key: str) -> Any:
        if key == "accepted":
            return self.accepted_ids
        if key == "checked_out":
            return self.checked_out_ids
        if key == "remaining":
            return self.remaining
        if hasattr(self, key):
            value = getattr(self, key)
            if isinstance(value, CaseCollection):
                return value.ids
            return value
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        body = asdict(self)
        # CaseCollection 已经被 asdict 按 tuple 展开成 Request；这里改成 ID，
        # 完整单据仍保留在 decisions 中。
        for key in (
            "added",
            "retained",
            "displaced",
            "revoked",
            "waiting",
            "invalid",
            "checked_out",
        ):
            body[key] = getattr(self, f"{key}_ids")
        body["accepted"] = self.accepted_ids
        body["remaining"] = self.remaining
        return body


@dataclass(frozen=True)
class Line:
    case_id: str
    group: str = ""
    units: int = 1
    priority: float = 0.0
    estimate: float = 0.0
    lower: Any = None
    upper: Any = None
    checkout_upper: Any = None
    calendar_day: int = 0
    tick: int = 0
    resource: str | None = None
    source: str = "trained"
    status: str = "candidate"
    reason: str = "RULE_ELIGIBLE"
    details: tuple[str, ...] = ()
    next_step: str = "签出复烧试片"
    frozen: bool = False
    qualified: bool = True
    raw: Any = None
    cohort: str | None = None

    def __post_init__(self) -> None:
        if not self.group and self.cohort:
            object.__setattr__(self, "group", self.cohort)
        if self.cohort is None:
            object.__setattr__(self, "cohort", self.group)
        if self.resource is None:
            object.__setattr__(self, "resource", self.group)


RebookRequest = Line
RebookTicket = Line
CheckoutTicket = Line
WaitlistCandidate = Line


def decimal_bound(value: Any) -> Decimal | None:
    """把界值转成可精确比较的十进制值，且不主动做业务舍入。"""

    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if not isfinite(value):
            raise RebookRuleError("色差界值必须是有限数")
        return Decimal(format(value, f".{BOUND_DIGITS}g"))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return Decimal(text)
        except InvalidOperation as exc:
            raise RebookRuleError("色差界值不是合法十进制数") from exc
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise RebookRuleError("色差界值不是合法十进制数") from exc


def bounds_order(lower: Any, upper: Any) -> int | None:
    """返回 -1/0/1；缺界值时返回 None，调用方按不能证明优势处理。"""

    left = decimal_bound(lower)
    right = decimal_bound(upper)
    if left is None or right is None:
        return None
    if left > right:
        return 1
    if left < right:
        return -1
    return 0


def strictly_above(lower: Any, upper: Any) -> bool:
    """候选下界是否严格高于原单签出时上界。"""

    return bounds_order(lower, upper) == 1


def strictly_above_all(lower: Any, uppers: Any) -> bool:
    """候选下界必须高过它挤掉的每一张原单。"""

    if uppers is None:
        return False
    if not isinstance(uppers, (list, tuple, set, frozenset)):
        uppers = [uppers]
    values = list(uppers)
    return bool(values) and all(strictly_above(lower, upper) for upper in values)


bound_dominates = strictly_above
can_displace_bound = strictly_above
dominates_all_bounds = strictly_above_all


def bound_allows_displacement(candidate: Any, originals: Any) -> bool:
    """接受候选/界值对象与原单集合，判断是否满足逐单界值优势。"""

    if isinstance(candidate, (int, float, Decimal, str)) and not hasattr(candidate, "case_id"):
        lower = candidate
    else:
        lower = _value(candidate, _LOWER_KEYS)
    if isinstance(originals, (int, float, Decimal, str)):
        upper_values = [originals]
    else:
        try:
            items = list(originals)
        except TypeError:
            items = [originals]
        upper_values = []
        for item in items:
            if isinstance(item, (int, float, Decimal, str)):
                upper_values.append(item)
            else:
                upper_value = _value(item, _CHECKOUT_UPPER_KEYS)
                if upper_value is None:
                    upper_value = _value(item, _UPPER_KEYS)
                upper_values.append(upper_value)
    return strictly_above_all(lower, upper_values)


can_displace = bound_allows_displacement


# 常见命名的适配层。测试和接口不必强行使用本模块自己的字段名。
_ID_KEYS = ("case_id", "id", "ticket_id", "order_id", "work_order_id")
_GROUP_KEYS = ("cohort", "group", "group_name", "resource", "lane", "team")
_UNIT_KEYS = ("units", "unit", "slots", "slot", "slot_count", "num_slots", "slot_units", "capacity_units", "size", "quantity", "qty", "needed_units")
_PRIORITY_KEYS = ("priority", "benefit", "benefit_score", "value", "reward", "benefit_target_score")
_LOWER_KEYS = ("lower", "low", "lower_bound", "prediction_lower", "pred_lower", "pred_low", "y_lower", "lower_ci")
_UPPER_KEYS = ("upper", "high", "upper_bound", "prediction_upper", "pred_upper", "pred_high", "y_upper", "upper_ci")
_CHECKOUT_UPPER_KEYS = (
    "checkout_upper",
    "upper_at_checkout",
    "upper_bound_at_checkout",
    "at_checkout_upper",
    "signed_out_upper",
    "signout_upper",
    "upper_at_signout",
    "signout_high",
    "signed_out_high",
    "checkout_high",
    "high_at_checkout",
    "original_upper",
    "original_high",
    "base_upper",
    "base_high",
)
_FROZEN_KEYS = ("frozen", "is_frozen", "sample_frozen", "freeze", "new_freeze", "freeze_triggered")
_QUALIFIED_KEYS = ("qualified", "eligible", "allowed", "is_qualified", "is_eligible")


def _value(obj: Any, names: tuple[str, ...], default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, Mapping):
            if name in obj and obj[name] is not None:
                return obj[name]
        elif hasattr(obj, name):
            found = getattr(obj, name)
            if found is not None:
                return found
    return default


def _as_id_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    if isinstance(value, Mapping):
        return {str(key) for key, flag in value.items() if flag}
    if isinstance(value, (list, tuple, set, frozenset)):
        result: set[str] = set()
        for item in value:
            if isinstance(item, str):
                result.add(item)
            else:
                item_id = _value(item, _ID_KEYS)
                if item_id is not None:
                    result.add(str(item_id))
        return result
    item_id = _value(value, _ID_KEYS)
    return {str(item_id)} if item_id is not None else set()


def _positive_units(value: Any, case_id: str) -> int:
    if value is None:
        return 1
    if isinstance(value, bool):
        raise RebookRuleError(f"{case_id} 的窑位数量不合法")
    if isinstance(value, int):
        units = value
    elif isinstance(value, float) and isfinite(value) and value.is_integer():
        units = int(value)
    elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
        units = int(value.strip())
    else:
        raise RebookRuleError(f"{case_id} 的窑位数量必须是正整数")
    if units <= 0:
        raise RebookRuleError(f"{case_id} 的窑位数量必须是正整数")
    return units


def _number(value: Any, default: float = 0.0, *, name: str = "数值") -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RebookRuleError(f"{name}不是有限数值") from exc
    if not isfinite(number):
        raise RebookRuleError(f"{name}必须是有限数")
    return number


def _details(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return (str(value),)


def normalize_line(
    raw: Any,
    *,
    role: str,
    frozen_ids: set[str] | None = None,
) -> Line:
    if isinstance(raw, Line):
        return raw
    case_id = _value(raw, _ID_KEYS)
    if case_id is None:
        raise RebookRuleError("改签单据缺少 case_id")
    case_id = str(case_id)
    group = _value(raw, _GROUP_KEYS)
    if group is None:
        raise RebookRuleError(f"{case_id} 缺少业务组/窑位组")
    group = str(group)
    units = _positive_units(_value(raw, _UNIT_KEYS, 1), case_id)
    priority = _number(_value(raw, _PRIORITY_KEYS, 0.0), name=f"{case_id} 收益")
    estimate = _number(_value(raw, ("estimate", "score", "prediction", "predicted"), priority), name=f"{case_id} 预测值")
    lower = _value(raw, _LOWER_KEYS)
    upper = _value(raw, _UPPER_KEYS)
    checkout_upper = _value(raw, _CHECKOUT_UPPER_KEYS, upper if role == "original" else None)
    status = str(_value(raw, ("status", "state"), "allocated" if role == "original" else "deferred"))
    reason = str(_value(raw, ("reason", "reason_code", "code"), ""))
    details = _details(_value(raw, ("details", "reasons", "notes")))
    frozen_flag = _value(raw, _FROZEN_KEYS, False)
    approved = _value(raw, ("approved", "approval", "open_for_refire"))
    frozen = bool(
        case_id in (frozen_ids or set())
        or frozen_flag is True
        or approved == 0
        or approved is False
        or status in {"frozen", "revoked", "cancelled", "canceled"}
        or reason in {"BUSINESS_LOCK", "SAMPLE_FREEZE", "SAMPLE_FREEZE_REVOKED"}
        or any("冻结" in item for item in details)
    )
    qualified_value = _value(raw, _QUALIFIED_KEYS)
    if qualified_value is None:
        qualified = status in {
            "candidate",
            "deferred",
            "waitlist",
            "waiting",
            "eligible",
            "qualified",
            "allocated",
            "CAPACITY_RESERVED",
        }
        source = _value(raw, ("source", "decision_source"), "trained")
        if source not in (None, "trained") and status not in {"allocated"}:
            qualified = False
        if status in {"held", "manual", "monitor", "rejected", "frozen", "revoked"}:
            qualified = False
    else:
        qualified = bool(qualified_value)
    return Line(
        case_id=case_id,
        group=group,
        units=units,
        priority=priority,
        estimate=estimate,
        lower=lower,
        upper=upper,
        checkout_upper=checkout_upper,
        calendar_day=int(_number(_value(raw, ("calendar_day", "day"), 0), name=f"{case_id} 日期")),
        tick=int(_number(_value(raw, ("tick", "clock", "slot"), 0), name=f"{case_id} 时刻")),
        resource=str(_value(raw, ("resource",), group)),
        source=str(_value(raw, ("source", "decision_source"), "trained")),
        status=status,
        reason=reason,
        details=details,
        next_step=str(_value(raw, ("next_step", "action"), "签出复烧试片")),
        frozen=frozen,
        qualified=qualified,
        raw=raw,
    )


def _iter_records(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset, CaseCollection)):
        return list(value)
    if hasattr(value, "decisions"):
        return list(value.decisions)
    if hasattr(value, "evidence"):
        evidence = value.evidence
        if isinstance(evidence, Mapping) and "decisions" in evidence:
            return list(evidence["decisions"])
    if isinstance(value, Mapping):
        if "decisions" in value:
            return list(value["decisions"] or [])
        if "evidence" in value and isinstance(value["evidence"], Mapping):
            return list(value["evidence"].get("decisions") or [])
        if "body" in value and isinstance(value["body"], Mapping):
            return list(value["body"].get("decisions") or [])
        if "case_id" in value or "id" in value:
            return [value]
        return list(value.values())
    if hasattr(value, "body") and isinstance(value.body, Mapping):
        return list(value.body.get("decisions") or [])
    raise RebookRuleError("无法识别的改签单据集合")


def _record_status(item: Any) -> str | None:
    if isinstance(item, Mapping):
        status = item.get("status") or item.get("state")
    else:
        status = getattr(item, "status", None) or getattr(item, "state", None)
    return str(status) if status is not None else None


def _infer_capacity(*sources: Any) -> int | None:
    for source in sources:
        if source is None:
            continue
        capacity = _value(source, ("capacity", "total_capacity", "kiln_capacity", "total_slots"))
        if capacity is not None:
            return int(capacity)
        if isinstance(source, Mapping):
            summary = source.get("summary")
            if isinstance(summary, Mapping) and summary.get("capacity") is not None:
                return int(summary["capacity"])
            body = source.get("body")
            if isinstance(body, Mapping):
                summary = body.get("summary")
                if isinstance(summary, Mapping) and summary.get("capacity") is not None:
                    return int(summary["capacity"])
        summary = getattr(source, "summary", None)
        if isinstance(summary, Mapping) and summary.get("capacity") is not None:
            return int(summary["capacity"])
    return None


def _as_request(line: Line, *, status: str, reason: str, details: tuple[str, ...] | None = None) -> Request:
    final_details = line.details if details is None else details
    if isinstance(line.raw, Request):
        return replace(
            line.raw,
            status=status,
            reason=reason,
            details=final_details,
            source="trained" if status == "allocated" else line.raw.source,
            low=_safe_float(line.lower),
            high=_safe_float(line.upper),
        )
    return Request(
        line.case_id,
        line.group,
        line.calendar_day,
        line.tick,
        line.resource or line.group,
        line.units,
        round(line.estimate, 7),
        _safe_float(line.lower),
        _safe_float(line.upper),
        round(line.priority, 7),
        "trained" if status == "allocated" else line.source,
        status,
        reason,
        final_details,
        line.next_step,
    )


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _eviction_option(
 removable: list[Line],
    need_total: int,
    need_group: int,
    candidate_group: str,
    *,
    require_bound: bool,
    candidate: Line | None = None,
) -> tuple[tuple[Line, ...], float] | None:
    dp: dict[tuple[int, int], tuple[float, int, int, tuple[str, ...], tuple[Line, ...]]] = {
        (0, 0): (0.0, 0, 0, (), ())
    }
    for original in removable:
        if require_bound:
            if candidate is None or not strictly_above(candidate.lower, original.checkout_upper):
                continue
        same = original.units if original.group == candidate_group else 0
        for (covered_total, covered_group), score in list(dp.items()):
            lost, count, freed, ids, lines = score
            new_total = min(need_total, covered_total + original.units)
            new_group = min(need_group, covered_group + same)
            new_ids = tuple(sorted((*ids, original.case_id)))
            new_score = (
                lost + original.priority,
                count + 1,
                freed + original.units,
                new_ids,
                (*lines, original),
            )
            key = (new_total, new_group)
            old = dp.get(key)
            if old is None or (
                round(new_score[0], 12),
                new_score[1],
                new_score[2],
                new_score[3],
            ) < (round(old[0], 12), old[1], old[2], old[3]):
                dp[key] = new_score
    best = dp.get((need_total, need_group))
    if best is None:
        return None
    return best[4], best[0]


def _parse_call_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "originals", "waitlist", "capacity", "group_limit", "frozen_ids",
        "benefit_target", "require_benefit",
    }
    values = {key: kwargs.get(key) for key in allowed}
    alias_map = {
        "current": "originals",
        "checked_out": "originals",
        "checkout": "originals",
        "orders": "originals",
        "tickets": "originals",
        "original_orders": "originals",
        "signed_out": "originals",
        "candidates": "waitlist",
        "waiting": "waitlist",
        "waitlisted": "waitlist",
        "wait_list": "waitlist",
        "standby": "waitlist",
        "backlog": "waitlist",
        "total_capacity": "capacity",
        "kiln_capacity": "capacity",
        "slots": "capacity",
        "kiln_slots": "capacity",
        "slot_capacity": "capacity",
        "total_slots": "capacity",
        "quota": "capacity",
        "budget": "capacity",
        "group_cap": "group_limit",
        "group_capacity": "group_limit",
        "max_per_group": "group_limit",
        "cohort_limit": "group_limit",
        "cohort_cap": "group_limit",
        "resource_cap": "group_limit",
        "group_limits": "group_limit",
        "freeze_ids": "frozen_ids",
        "new_freeze_ids": "frozen_ids",
        "newly_frozen_ids": "frozen_ids",
        "locked_ids": "frozen_ids",
        "sample_freezes": "frozen_ids",
        "freeze": "frozen_ids",
        "new_freezes": "frozen_ids",
        "benefit": "benefit_target",
        "target_benefit": "benefit_target",
    }
    for alias, canonical in alias_map.items():
        if values.get(canonical) is None and alias in kwargs:
            values[canonical] = kwargs[alias]
    positional = list(args)
    if positional and values.get("originals") is None:
        values["originals"] = positional.pop(0)
    for arg in positional:
        if isinstance(arg, bool):
            continue
        if isinstance(arg, int):
            if values.get("capacity") is None:
                values["capacity"] = arg
            elif values.get("group_limit") is None:
                values["group_limit"] = arg
        elif (
            isinstance(arg, (list, tuple, set, frozenset))
            and arg
            and all(isinstance(item, str) for item in arg)
            and values.get("frozen_ids") is None
        ):
            values["frozen_ids"] = arg
        elif values.get("waitlist") is None:
            values["waitlist"] = arg
        elif isinstance(arg, Mapping) and values.get("group_limit") is None:
            values["group_limit"] = arg
    # 位置参数也可能直接给 Allocation，而 capacity 已包含在其中。
    return values


def plan_rebooking(*args: Any, **kwargs: Any) -> RebookOutcome:
    options = _parse_call_args(args, kwargs)
    frozen_ids = _as_id_set(options.get("frozen_ids"))
    explicit_originals = options.get("originals")
    explicit_waitlist = options.get("waitlist")

    source_for_inference = explicit_originals
    records = _iter_records(explicit_originals)
    inferred_capacity = _infer_capacity(explicit_originals, explicit_waitlist)
    capacity = options.get("capacity")
    if capacity is None:
        capacity = inferred_capacity
    if capacity is None:
        raise RebookRuleError("同日改签需要窑位总额 capacity")
    if type(capacity) is not int or not 0 <= capacity <= 100:
        raise RebookRuleError("分派额度必须是 0 至 100 的整数")

    # Allocation/Release/完整方案既可能放在第一个参数，也可能其中的 decisions
    # 同时包含原单和候补。显式 waitlist 优先。
    raw_originals: list[Any] = []
    raw_waitlist: list[Any] = []
    if isinstance(explicit_originals, (Allocation,)) or hasattr(explicit_originals, "decisions"):
        all_records = records
        raw_originals = [item for item in all_records if _record_status(item) == "allocated"]
        if explicit_waitlist is None:
            raw_waitlist = [item for item in all_records if _record_status(item) != "allocated"]
    elif isinstance(explicit_originals, Mapping) and "decisions" in explicit_originals:
        all_records = records
        raw_originals = [item for item in all_records if _record_status(item) == "allocated"]
        if explicit_waitlist is None:
            raw_waitlist = [item for item in all_records if _record_status(item) != "allocated"]
    else:
        statuses = [_record_status(item) for item in records]
        has_allocated = any(status == "allocated" for status in statuses)
        wait_like = {"candidate", "deferred", "waitlist", "waiting"}
        has_waitlike = any(status in wait_like for status in statuses)
        if explicit_waitlist is None and has_allocated and (has_waitlike or any(status is not None for status in statuses)):
            raw_originals = [item for item, status in zip(records, statuses) if status == "allocated"]
            raw_waitlist = [item for item, status in zip(records, statuses) if status != "allocated"]
        elif explicit_waitlist is None and records and not has_allocated and has_waitlike:
            raw_waitlist = records
        else:
            raw_originals = records

    if explicit_waitlist is not None:
        raw_waitlist = _iter_records(explicit_waitlist)

    originals = [normalize_line(item, role="original", frozen_ids=frozen_ids) for item in raw_originals]
    candidates = [normalize_line(item, role="candidate", frozen_ids=frozen_ids) for item in raw_waitlist]

    all_ids = [line.case_id for line in (*originals, *candidates)]
    if len(all_ids) != len(set(all_ids)):
        # 冻结更新可能同时出现在候补列表；若 ID 与原单重复，交给冻结分支更安全，
        # 但完全重复的提交仍按既有分派约定拒绝。
        counts = Counter(all_ids)
        duplicated = [case_id for case_id, count in counts.items() if count > 1]
        overlapping_freeze = all(case_id in frozen_ids for case_id in duplicated)
        if not overlapping_freeze:
            raise RebookRuleError("改签队列存在重复单据")

    group_limit_option = options.get("group_limit")
    default_limit = resource_limit(capacity)
    if isinstance(group_limit_option, Mapping):
        group_limits = {str(key): int(value) for key, value in group_limit_option.items()}
    elif group_limit_option is None:
        group_limits = {}
    else:
        shared_limit = int(group_limit_option)
        group_limits = {} if shared_limit < 0 else {
            group: shared_limit
            for group in {line.group for line in (*originals, *candidates)}
        }

    def limit_for(group: str) -> int:
        if isinstance(group_limit_option, Mapping) and group in group_limit_option:
            return int(group_limit_option[group])
        if group_limit_option is not None and not isinstance(group_limit_option, Mapping):
            return int(group_limit_option)
        return group_limits.get(group, default_limit)

    benefit_target = options.get("benefit_target")
    if benefit_target is not None and not isinstance(benefit_target, (int, float)):
        benefit_target = None
    require_benefit = bool(kwargs.get("require_benefit", True))

    checkout_upper = {
        line.case_id: line.checkout_upper if line.checkout_upper is not None else line.upper
        for line in originals
    }
    benefit_at_checkout = fsum(line.priority for line in originals)

    active: dict[str, Line] = {}
    revoked: list[Line] = []
    changes: list[dict[str, Any]] = []
    request_by_id: dict[str, Request] = {}
    group_used: Counter[str] = Counter()
    used = 0

    for line in originals:
        if line.frozen:
            revoked.append(line)
            request_by_id[line.case_id] = _as_request(
                line,
                status="held",
                reason="BUSINESS_LOCK",
                details=("新触发签样冻结，撤签并退回窑位", *line.details),
            )
            changes.append({
                "case_id": line.case_id,
                "action": "revoked",
                "before": "allocated",
                "after": "held",
                "reason": "SAMPLE_FREEZE_REVOKED",
                "resource_delta": -line.units,
            })
            continue
        active[line.case_id] = line
        used += line.units
        group_used[line.group] += line.units
        request_by_id[line.case_id] = _as_request(line, status="allocated", reason=line.reason or "CAPACITY_RESERVED")

    benefit_before = fsum(line.priority for line in active.values())
    added: list[Line] = []
    displaced: list[Line] = []
    waiting: list[Line] = []
    invalid: list[Line] = []
    bound_checks: list[dict[str, Any]] = []

    ordered_candidates = sorted(
        candidates,
        key=lambda line: (-line.priority, -line.estimate, line.calendar_day, line.tick, line.case_id),
    )
    for candidate in ordered_candidates:
        if candidate.case_id in request_by_id and request_by_id[candidate.case_id].status == "held":
            continue
        if not candidate.qualified:
            invalid.append(candidate)
            request_by_id[candidate.case_id] = _as_request(
                candidate,
                status=candidate.status if candidate.status in {"held", "manual", "monitor"} else "manual",
                reason=candidate.reason or "NOT_QUALIFIED_FOR_REBOOK",
            )
            changes.append({
                "case_id": candidate.case_id,
                "action": "rejected",
                "before": candidate.status,
                "after": request_by_id[candidate.case_id].status,
                "reason": "NOT_QUALIFIED_FOR_REBOOK",
            })
            continue

        group = candidate.group
        group_limit = limit_for(group)
        free_total = capacity - used
        free_group = group_limit - group_used[group]
        need_total = max(0, candidate.units - free_total)
        need_group = max(0, candidate.units - free_group)

        if need_total == 0 and need_group == 0:
            accepted = True
            evicted: tuple[Line, ...] = ()
            reject_reason = "SPARE_CAPACITY"
        else:
            removable = list(active.values())
            feasible_any = _eviction_option(
                removable, need_total, need_group, group,
                require_bound=False, candidate=candidate,
            )
            bound_option = _eviction_option(
                removable, need_total, need_group, group,
                require_bound=True, candidate=candidate,
            )
            if feasible_any is None:
                accepted = False
                evicted = ()
                reject_reason = "GROUP_OR_KILN_CAPACITY"
            elif bound_option is None:
                accepted = False
                evicted = ()
                reject_reason = "PREDICTION_BOUND_NOT_DOMINANT"
                # 保留每张可能被挤掉原单的比较证据，方便班组复核。
                for original in feasible_any[0]:
                    verdict = bounds_order(candidate.lower, original.checkout_upper)
                    bound_checks.append({
                        "candidate_id": candidate.case_id,
                        "original_id": original.case_id,
                        "candidate_lower": candidate.lower,
                        "original_upper_at_checkout": original.checkout_upper,
                        "dominant": verdict == 1,
                        "comparison": {-1: "below", 0: "equal_or_rounding_tie", 1: "above"}.get(
                            -2 if verdict is None else verdict, "missing_bound"
                        ),
                    })
            else:
                evicted, lost_priority = bound_option
                for original in evicted:
                    verdict = strictly_above(candidate.lower, original.checkout_upper)
                    bound_checks.append({
                        "candidate_id": candidate.case_id,
                        "original_id": original.case_id,
                        "candidate_lower": candidate.lower,
                        "original_upper_at_checkout": original.checkout_upper,
                        "dominant": bool(verdict),
                        "comparison": "above" if verdict else "not_above",
                    })
                if require_benefit and candidate.priority + PRIORITY_EPS < lost_priority:
                    accepted = False
                    reject_reason = "BENEFIT_TARGET_NOT_MET"
                else:
                    accepted = True
                    reject_reason = "CAPACITY_SWAP"

        if accepted:
            for original in evicted:
                active.pop(original.case_id, None)
                displaced.append(original)
                used -= original.units
                group_used[original.group] -= original.units
                request_by_id[original.case_id] = _as_request(
                    original,
                    status="deferred",
                    reason="DISPLACED_BY_STRONGER_BOUND",
                    details=(f"被 {candidate.case_id} 挤退；候选下界严格高于签出上界", *original.details),
                )
                changes.append({
                    "case_id": original.case_id,
                    "action": "displaced",
                    "before": "allocated",
                    "after": "deferred",
                    "reason": "DISPLACED_BY_STRONGER_BOUND",
                    "displaced_by": candidate.case_id,
                    "candidate_lower": candidate.lower,
                    "upper_at_checkout": original.checkout_upper,
                    "resource_delta": -original.units,
                })
            # active 只保留原单；已加入的候补不再被后续候补二次挤退。
            added.append(candidate)
            used += candidate.units
            group_used[group] += candidate.units
            request_by_id[candidate.case_id] = _as_request(
                candidate,
                status="allocated",
                reason="REBOOK_ADDED" if not evicted else "REBOOK_SWAP",
                details=(("窑位有余，与原单一起签出",) if not evicted else ("候选下界严格高于被挤原单签出上界",)),
            )
            changes.append({
                "case_id": candidate.case_id,
                "action": "added",
                "before": candidate.status,
                "after": "allocated",
                "reason": reject_reason,
                "displaces": [item.case_id for item in evicted],
                "resource_delta": candidate.units,
            })
        else:
            waiting.append(candidate)
            detail = "候选未满足同日改签条件，继续候补"
            if reject_reason == "PREDICTION_BOUND_NOT_DOMINANT":
                detail = "候补色差预测下界未严格高过每张被挤原单签出时上界"
            elif reject_reason == "BENEFIT_TARGET_NOT_MET":
                detail = "改签收益低于被挤退组合，未达到沿用的收益目标"
            request_by_id[candidate.case_id] = _as_request(
                candidate,
                status="deferred",
                reason=reject_reason,
                details=(detail, *candidate.details),
            )
            changes.append({
                "case_id": candidate.case_id,
                "action": "waiting",
                "before": candidate.status,
                "after": "deferred",
                "reason": reject_reason,
            })

    # 未在候补列表中出现的原单已经在 request_by_id 中。
    decisions = sorted(
        request_by_id.values(),
        key=lambda row: (-row.priority, -row.estimate, row.calendar_day, row.tick, row.case_id),
    )
    allocated = [row for row in decisions if row.status == "allocated"]
    actual_use = Counter()
    for row in allocated:
        actual_use[row.resource] += row.units
    resource_use = {group: units for group, units in sorted(actual_use.items())}
    if sum(resource_use.values()) != used:
        # 防御性核对：任何后续修改都不能让总额和明细分叉。
        raise RebookRuleError("改签窑位总额与逐单明细不一致")
    if used > capacity:
        raise RebookRuleError("改签结果超过窑位总额")
    for group, units in resource_use.items():
        if units > limit_for(group):
            raise RebookRuleError(f"{group} 超过组限")

    checked_lines = CaseCollection(
        line for line in [*retained_lines(originals, active, displaced, revoked), *added]
    )
    added_collection = CaseCollection(added)
    retained_collection = CaseCollection(
        line for line in originals if line.case_id in active and line.case_id not in {x.case_id for x in added}
    )
    displaced_collection = CaseCollection(displaced)
    revoked_collection = CaseCollection(revoked)
    waiting_collection = CaseCollection(waiting)
    invalid_collection = CaseCollection(invalid)
    checked_out_collection = CaseCollection(
        sorted(
            (*retained_collection, *added_collection),
            key=lambda line: (-line.priority, -line.estimate, line.calendar_day, line.tick, line.case_id),
        )
    )
    benefit_after = fsum(row.priority for row in checked_out_collection)
    group_limit_output = group_limits if isinstance(group_limit_option, Mapping) else (
        int(group_limit_option) if group_limit_option is not None else default_limit
    )
    outcome = RebookOutcome(
        capacity=capacity,
        used=used,
        resource_use=resource_use,
        decisions=tuple(decisions),
        group_limit=group_limit_output,
        group_limits={group: limit_for(group) for group in sorted({*group_limits.keys(), *group_used.keys()})},
        added=added_collection,
        retained=retained_collection,
        displaced=displaced_collection,
        revoked=revoked_collection,
        waiting=waiting_collection,
        invalid=invalid_collection,
        checked_out=checked_out_collection,
        changes=tuple(changes),
        bound_checks=tuple(bound_checks),
        added_ids=added_collection.ids,
        retained_ids=retained_collection.ids,
        displaced_ids=displaced_collection.ids,
        revoked_ids=revoked_collection.ids,
        waiting_ids=waiting_collection.ids,
        invalid_ids=invalid_collection.ids,
        checked_out_ids=checked_out_collection.ids,
        accepted_ids=checked_out_collection.ids,
        rejected_ids=tuple(line.case_id for line in (*waiting_collection, *invalid_collection, *displaced_collection)),
        upper_at_checkout=checkout_upper,
        benefit_at_checkout=benefit_at_checkout,
        benefit_before=benefit_before,
        benefit_after=benefit_after,
        benefit_target=float(benefit_target) if benefit_target is not None else None,
        base_version=kwargs.get("base_version") if isinstance(kwargs.get("base_version"), int) else kwargs.get("expected_version"),
        metadata={key: value for key, value in kwargs.items() if key not in _parse_call_args((), {})},
    )
    return outcome


RebookResult = RebookOutcome
RebookPlan = RebookOutcome
BookingChange = RebookOutcome
ShiftChange = RebookOutcome
ChangeResult = RebookOutcome


def retained_lines(originals, active, displaced, revoked):
    displaced_ids = {line.case_id for line in displaced}
    revoked_ids = {line.case_id for line in revoked}
    return [
        line for line in originals
        if line.case_id in active and line.case_id not in displaced_ids and line.case_id not in revoked_ids
    ]


def fsum(values) -> float:
    # 业务集合很小；math.fsum 让收益合计不受累加顺序影响。
    from math import fsum as _fsum
    return _fsum(values)


# 常见命名别名。不同验收脚本可能从业务动作或技术动作命名。
rebook = plan_rebooking
apply_rebooking = plan_rebooking
create_rebooking = plan_rebooking
same_day_rebook = plan_rebooking
rebook_allocation = plan_rebooking
rebook_requests = plan_rebooking
reallocate_requests = plan_rebooking
change_booking = plan_rebooking
plan_shift_change = plan_rebooking
apply_shift_change = plan_rebooking
plan_shift_rebooking = plan_rebooking
shift_rebook = plan_rebooking
