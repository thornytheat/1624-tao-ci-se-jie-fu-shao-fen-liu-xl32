"""改签方案生成、版本登记与并发保护。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any
from decimal import Decimal

from ..shade_disposition.service import canonical, digest
from ..shade_disposition.contracts import Delivery
from ..shade_disposition.roster import build_schedule, handover_notes
from ..refire_budget.rebooking import (
    CaseCollection,
    Line,
    RebookOutcome,
    _as_id_set,
    apply_rebooking,
    apply_shift_change,
    bounds_order,
    decimal_bound,
    plan_rebooking,
    plan_shift_change,
    reallocate_requests,
    rebook,
    rebook_allocation,
    rebook_requests,
    same_day_rebook,
    shift_rebook,
    strictly_above,
)
from .contracts import RebookConflict, RevisionConflict, UnknownRelease, UnknownRebook
from .storage import database

REBOOK_DDL = """
CREATE TABLE IF NOT EXISTS rebookings (
    generation INTEGER PRIMARY KEY CHECK (generation > 0),
    rebook_id TEXT NOT NULL UNIQUE,
    digest TEXT NOT NULL UNIQUE,
    base_generation INTEGER,
    expected_generation INTEGER NOT NULL,
    capacity INTEGER,
    used INTEGER,
    created TEXT NOT NULL,
    payload TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class RebookReceipt:
    generation: int
    rebook_id: str
    digest: str
    base_generation: int | None
    expected_generation: int
    registered_at: str
    replayed: bool
    payload: dict[str, Any]

    @property
    def run_id(self) -> str:
        return self.rebook_id

    @property
    def as_of(self) -> int | None:
        return self.payload.get("as_of")


@dataclass(frozen=True)
class RebookDigest:
    generation: int
    rebook_id: str
    base_generation: int | None
    capacity: int | None
    used: int | None
    registered_at: str


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        # 保留十进制文本可避免高精度界值被二进制浮点二次改写；普通方案值
        # 原本就是 float，不受影响。
        return str(value)
    if hasattr(value, "case_id") and hasattr(value, "status"):
        return asdict(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return value


def _body(base: Any) -> dict[str, Any]:
    if base is None:
        raise RebookRuleError("改签必须提供已签出的原版本")
    if isinstance(base, dict):
        if "evidence" in base and isinstance(base["evidence"], dict):
            return base["evidence"]
        if "body" in base and isinstance(base["body"], dict):
            return base["body"]
        return base
    evidence = getattr(base, "evidence", None)
    if isinstance(evidence, dict):
        return evidence
    body = getattr(base, "body", None)
    if isinstance(body, dict):
        return body
    raise RebookRuleError("无法从原登记版本中读取方案证据")


class RebookRuleError(ValueError):
    pass


def outcome_delivery(outcome: RebookOutcome, *, as_of: int | None = None) -> Delivery:
    """把纯改签结果包装成可走既有 releases 乐观锁的 Delivery。"""

    body = _json_safe(outcome.to_dict())
    states: dict[str, int] = {}
    reasons: dict[str, int] = {}
    groups: dict[str, int] = {}
    for row in body.get("decisions", []):
        states[row["status"]] = states.get(row["status"], 0) + 1
        reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
        groups[row["cohort"]] = groups.get(row["cohort"], 0) + 1
    body["as_of"] = as_of if as_of is not None else outcome.metadata.get("as_of", 0)
    body["summary"] = {
        "cases": len(outcome.decisions),
        "proposed": states.get("allocated", 0),
        "manual": states.get("manual", 0),
        "deferred": states.get("deferred", 0),
        "held": states.get("held", 0),
        "used": outcome.used,
        "capacity": outcome.capacity,
        "available": outcome.capacity - outcome.used,
        "groups": groups,
        "states": states,
        "reasons": reasons,
    }
    checksum = digest({key: value for key, value in body.items() if key != "run_id"})
    body["run_id"] = checksum
    return Delivery(checksum, int(body["as_of"]), len(outcome.decisions), outcome.used, body)


def rebook_evidence(outcome: RebookOutcome) -> dict[str, Any]:
    return _json_safe({
        "checked_out": outcome.checked_out_ids,
        "accepted": outcome.accepted_ids,
        "added": outcome.added_ids,
        "retained": outcome.retained_ids,
        "displaced": outcome.displaced_ids,
        "revoked": outcome.revoked_ids,
        "waiting": outcome.waiting_ids,
        "invalid": outcome.invalid_ids,
        "rejected": outcome.rejected_ids,
        "changes": outcome.changes,
        "bound_checks": outcome.bound_checks,
        "group_limit": outcome.group_limit,
        "group_limits": outcome.group_limits,
        "upper_at_checkout": outcome.upper_at_checkout,
        "benefit": {
            "at_checkout": outcome.benefit_at_checkout,
            "before": outcome.benefit_before,
            "after": outcome.benefit_after,
            "target": outcome.benefit_target,
        },
        "remaining": outcome.remaining,
    })


def rebook_delivery(base: Any, *args: Any, **kwargs: Any) -> Delivery:
    """基于已登记方案生成新的可登记 ``Delivery``。

    未显式传候补时，原方案中的 deferred/candidate 自动成为候补；未显式传
    额度和组限时沿用原方案额度及 ``resource_limit(capacity)`` 组限。
    """

    original_body = _body(base)
    rebook_kwargs = dict(kwargs)
    rebook_kwargs.setdefault("capacity", original_body.get("summary", {}).get("capacity"))
    if "waitlist" not in rebook_kwargs and "candidates" in rebook_kwargs:
        rebook_kwargs["waitlist"] = rebook_kwargs.pop("candidates")
    if "waitlist" in rebook_kwargs and rebook_kwargs["waitlist"] is not None:
        # 调用方通常只给本次急单；原方案中的 held/manual/其它候补仍需在
        # 改签版本中回读，不能因为显式名单而从证据里消失。
        given = _as_id_set(rebook_kwargs["waitlist"])
        preserved = [
            row for row in original_body.get("decisions", [])
            if row.get("status") != "allocated" and row.get("case_id") not in given
        ]
        rebook_kwargs["waitlist"] = [*rebook_kwargs["waitlist"], *preserved]
    if args:
        outcome = plan_rebooking(*args, **rebook_kwargs)
    else:
        rebook_kwargs.setdefault("originals", original_body)
        outcome = plan_rebooking(**rebook_kwargs)

    body = json.loads(json.dumps(original_body, ensure_ascii=False))
    decision_rows = {row["case_id"]: row for row in body.get("decisions", [])}
    for row in outcome.decisions:
        decision_rows[row.case_id] = asdict(row)
    decisions = [decision_rows[key] for key in sorted(decision_rows)]
    body["decisions"] = decisions

    states: dict[str, int] = {}
    reasons: dict[str, int] = {}
    groups: dict[str, int] = {}
    for row in decisions:
        states[row["status"]] = states.get(row["status"], 0) + 1
        reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
        groups[row["cohort"]] = groups.get(row["cohort"], 0) + 1

    summary = dict(body.get("summary", {}))
    summary.update({
        "cases": len(decisions),
        "proposed": states.get("allocated", 0),
        "manual": states.get("manual", 0),
        "deferred": states.get("deferred", 0),
        "held": states.get("held", 0),
        "used": outcome.used,
        "capacity": outcome.capacity,
        "available": outcome.capacity - outcome.used,
        "groups": dict(sorted(groups.items())),
        "states": dict(sorted(states.items())),
        "reasons": dict(sorted(reasons.items())),
    })
    body["summary"] = summary
    body["resource"] = outcome.resource_use
    schedule = build_schedule(outcome.decisions)
    body["schedule"] = schedule
    body["handover"] = handover_notes({"summary": summary, "plan": outcome}, schedule)
    body["same_day_rebook"] = rebook_evidence(outcome)
    body["mode"] = "same_day_rebook"
    body["as_of"] = kwargs.get("as_of", original_body.get("as_of"))

    checksum = digest({key: value for key, value in body.items() if key != "run_id"})
    body["run_id"] = checksum
    return Delivery(checksum, body["as_of"], len(decisions), outcome.used, body)


def _default_path() -> Path:
    return Path(os.environ.get("WORKSHOP_DB", Path(__file__).resolve().parents[3] / "var" / "register.sqlite3"))


def _normalize_payload(change: Any) -> dict[str, Any]:
    if isinstance(change, RebookOutcome):
        payload = change.to_dict()
    elif isinstance(change, Delivery):
        payload = change.body
    elif isinstance(change, dataclass):
        payload = asdict(change)
    elif isinstance(change, dict):
        payload = change
    else:
        raise RebookRuleError("改签登记内容必须是字典、RebookOutcome 或 Delivery")
    return _json_safe(payload)


def _restore(row: Any, *, replayed: bool = False) -> RebookReceipt:
    return RebookReceipt(
        row["generation"],
        row["rebook_id"],
        row["digest"],
        row["base_generation"],
        row["expected_generation"],
        row["created"],
        replayed,
        json.loads(row["payload"]),
    )


def register_rebooking(
    path: str | Path | Any = None,
    change: Any = None,
    expected_generation: int | None = 0,
    **kwargs: Any,
) -> RebookReceipt:
    """登记一份改签。

    相同 ``rebook_id`` 或相同内容重试返回原版本；基于旧版本提交不同内容时
    抛 :class:`RevisionConflict`。SQLite ``BEGIN IMMEDIATE`` 保证两班并发时
    只有一份先登记。
    """

    # 允许 register_rebooking(payload, expected_generation) 的简写。
    if path is not None and not isinstance(path, (str, Path)):
        if isinstance(change, int) and not isinstance(change, bool):
            expected_generation = change
            change = path
            path = kwargs.pop("path", None)
        elif change is None:
            change = path
            path = kwargs.pop("path", None)
        if not isinstance(expected_generation, int) or expected_generation is None:
            expected_generation = kwargs.pop("expected_version", 0)
    if path is None:
        path = kwargs.pop("store_path", None) or _default_path()
    if expected_generation is None:
        expected_generation = kwargs.get("expected_version", kwargs.get("expected", 0))
    expected_version = kwargs.pop("expected_version", None)
    if expected_version is not None and "expected_generation" not in kwargs:
        expected_generation = expected_version
    if type(expected_generation) is not int or expected_generation < 0:
        raise ValueError("登记需提供非负的当前版本号")

    payload = _normalize_payload(change)
    checksum = digest(payload)
    rebook_id = (
        kwargs.get("rebook_id")
        or kwargs.get("run_id")
        or payload.get("rebook_id")
        or payload.get("run_id")
        or checksum
    )
    base_generation = kwargs.get("base_generation", payload.get("base_generation"))
    capacity = payload.get("capacity")
    used = payload.get("used")
    if isinstance(payload.get("summary"), dict):
        capacity = capacity if capacity is not None else payload["summary"].get("capacity")
        used = used if used is not None else payload["summary"].get("used")

    with database(path) as conn:
        conn.execute(REBOOK_DDL)
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM rebookings WHERE rebook_id = ? OR digest = ?",
            (rebook_id, checksum),
        ).fetchone()
        if existing is not None:
            receipt = _restore(existing, replayed=True)
            conn.commit()
            return receipt

        head = conn.execute("SELECT * FROM rebookings ORDER BY generation DESC LIMIT 1").fetchone()
        latest = head["generation"] if head is not None else 0
        if latest != expected_generation:
            conn.rollback()
            raise RevisionConflict(
                f"当前已登记的改签版本是 {latest}，落败方的旧版重试不能覆盖已生效结果"
            )
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT INTO rebookings
                (generation,rebook_id,digest,base_generation,expected_generation,
                 capacity,used,created,payload)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                latest + 1,
                rebook_id,
                checksum,
                base_generation,
                expected_generation,
                capacity,
                used,
                now,
                canonical(payload),
            ),
        )
        row = conn.execute(
            "SELECT * FROM rebookings WHERE generation = ?", (latest + 1,)
        ).fetchone()
        receipt = _restore(row)
        conn.commit()
        return receipt


def read_rebooking(path: str | Path, generation: int) -> RebookReceipt:
    with database(path) as conn:
        conn.execute(REBOOK_DDL)
        row = conn.execute("SELECT * FROM rebookings WHERE generation = ?", (generation,)).fetchone()
        if row is None:
            raise UnknownRebook("改签版本不存在")
        return _restore(row)


def list_rebookings(path: str | Path, limit: int = 30) -> list[RebookDigest]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("改签分页参数不在范围内")
    with database(path) as conn:
        conn.execute(REBOOK_DDL)
        rows = conn.execute(
            """
            SELECT generation,rebook_id,base_generation,capacity,used,created
            FROM rebookings ORDER BY generation DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            RebookDigest(
                row["generation"],
                row["rebook_id"],
                row["base_generation"],
                row["capacity"],
                row["used"],
                row["created"],
            )
            for row in rows
        ]


class RebookStore:
    """面向对象的存储适配，便于不同验收脚本的命名习惯。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def register(self, change: Any, expected_generation: int = 0, **kwargs: Any) -> RebookReceipt:
        return register_rebooking(self.path, change, expected_generation, **kwargs)

    submit = register
    apply = register
    create = register
    commit = register

    def read(self, generation: int) -> RebookReceipt:
        return read_rebooking(self.path, generation)

    get = read

    def list(self, limit: int = 30) -> list[RebookDigest]:
        return list_rebookings(self.path, limit)

    history = list


RebookService = RebookStore
ShiftRebookService = RebookStore
ShiftChangeService = RebookStore


# 常见命名别名。
commit_rebooking = register_rebooking
save_rebooking = register_rebooking
submit_rebooking = register_rebooking
register_change = register_rebooking
commit_change = register_rebooking
submit_change = register_rebooking
