from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest

from kilngrader.kiln_dispatch.contracts import RevisionConflict
from kilngrader.kiln_dispatch.rebooking import (
    list_rebookings,
    read_rebooking,
    register_rebooking,
)
from kilngrader.refire_budget.rebooking import plan_rebooking, strictly_above


@dataclass(frozen=True)
class Ticket:
    case_id: str
    cohort: str = "g"
    units: int = 1
    priority: float = 1.0
    lower: float | None = None
    upper: float | None = 2.0
    upper_at_checkout: float | None = None
    status: str = "allocated"


def ids(collection):
    return list(collection.ids if hasattr(collection, "ids") else collection)


def test_spare_kiln_and_group_limit_adds_waitlist_with_originals():
    result = plan_rebooking(
        [Ticket("O", units=1, upper=2.0)],
        [Ticket("W", status="deferred", lower=1.5, upper=2.8, priority=2.0)],
        capacity=3,
        group_limit=2,
    )

    assert result.checked_out_ids == ["W", "O"] or set(result.checked_out_ids) == {"O", "W"}
    assert result.added_ids == ("W",)
    assert result.displaced_ids == ()
    assert result.used == 2
    assert result.remaining == 1


def test_conflict_requires_dominance_over_every_displaced_original():
    originals = [
        Ticket("O1", upper=3.0, upper_at_checkout=3.0),
        Ticket("O2", upper=3.05, upper_at_checkout=3.05),
    ]
    candidate = Ticket(
        "W", units=2, status="deferred", priority=100.0,
        lower=3.1, upper=4.0,
    )

    accepted = plan_rebooking(originals, [candidate], capacity=2, group_limit=2)
    assert accepted.checked_out_ids == ("W",)
    assert set(accepted.displaced_ids) == {"O1", "O2"}

    blocked_candidate = Ticket(
        "W2", units=2, status="deferred", priority=100.0,
        lower=3.02, upper=4.0,
    )
    blocked = plan_rebooking(originals, [blocked_candidate], capacity=2, group_limit=2)
    assert set(blocked.checked_out_ids) == {"O1", "O2"}
    assert blocked.displaced_ids == ()
    assert blocked.waiting_ids == ("W2",)
    assert blocked.changes[-1]["reason"] == "PREDICTION_BOUND_NOT_DOMINANT"


def test_rounding_artifacts_do_not_win_bound_verdict():
    assert strictly_above(2.000000001, 2.0)
    assert not strictly_above(0.1 + 0.2, 0.3)
    assert not strictly_above(2.0, 2.0)

    result = plan_rebooking(
        [Ticket("O", upper=0.3, upper_at_checkout=0.3)],
        [Ticket("W", status="deferred", lower=0.1 + 0.2, priority=2.0)],
        capacity=1,
        group_limit=1,
    )
    assert result.checked_out_ids == ("O",)
    assert result.waiting_ids == ("W",)


def test_new_freeze_revokes_even_without_waitlist_and_returns_slots():
    result = plan_rebooking(
        [Ticket("O1", units=2), Ticket("O2", units=1, cohort="other")],
        [],
        capacity=3,
        frozen_ids=["O1"],
    )

    assert result.revoked_ids == ("O1",)
    assert result.checked_out_ids == ("O2",)
    assert result.used == 1
    assert result.resource_use == {"other": 1}
    row = next(row for row in result.decisions if row.case_id == "O1")
    assert row.status == "held" and row.reason == "BUSINESS_LOCK"


def test_group_limit_is_still_enforced_when_total_kiln_has_room():
    result = plan_rebooking(
        [Ticket("O")],
        [Ticket("W", status="deferred", lower=2.1, priority=100.0)],
        capacity=4,
        group_limit=1,
    )

    assert result.checked_out_ids == ("W",)
    assert result.displaced_ids == ("O",)
    assert result.group_limits["g"] == 1
    assert result.resource_use == {"g": 1}


def test_benefit_goal_blocks_low_value_swap():
    result = plan_rebooking(
        [Ticket("O", priority=10.0, upper=2.0)],
        [Ticket("W", priority=1.0, status="deferred", lower=2.1)],
        capacity=1,
        group_limit=1,
    )

    assert result.checked_out_ids == ("O",)
    assert result.waiting_ids == ("W",)
    assert result.changes[-1]["reason"] == "BENEFIT_TARGET_NOT_MET"


def test_concurrent_different_rebooks_only_first_wins(tmp_path):
    store = tmp_path / "rebooks.db"
    first = plan_rebooking(
        [{"case_id": "O", "cohort": "g", "upper": 2.0}],
        [{"case_id": "A", "cohort": "g", "lower": 2.1, "priority": 2.0, "status": "deferred"}],
        capacity=1,
        group_limit=1,
    )
    second = plan_rebooking(
        [{"case_id": "O", "cohort": "g", "upper": 2.0}],
        [{"case_id": "B", "cohort": "g", "lower": 2.2, "priority": 3.0, "status": "deferred"}],
        capacity=1,
        group_limit=1,
    )

    def submit(payload):
        try:
            return register_rebooking(store, payload, 0)
        except RevisionConflict as exc:
            return exc

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(submit, [first, second]))

    receipts = [item for item in outcomes if not isinstance(item, RevisionConflict)]
    conflicts = [item for item in outcomes if isinstance(item, RevisionConflict)]
    assert len(receipts) == 1 and len(conflicts) == 1
    winner = receipts[0]
    loser = first if "B" in winner.payload["checked_out"] else second

    with pytest.raises(RevisionConflict, match="版本"):
        register_rebooking(store, loser, 0)
    saved = read_rebooking(store, 1)
    assert saved.rebook_id == winner.rebook_id
    assert saved.payload["checked_out"] == winner.payload["checked_out"]
    assert len(saved.payload["checked_out"]) == 1
    assert list_rebookings(store) and len(list_rebookings(store)) == 1


def test_outcome_can_use_existing_release_optimistic_lock(tmp_path):
    from kilngrader.kiln_dispatch.service import register

    store = tmp_path / "releases.db"
    outcome = plan_rebooking(
        [{"case_id": "O", "cohort": "g", "upper": 2.0}],
        [{"case_id": "W", "cohort": "g", "lower": 2.1, "priority": 2.0, "status": "deferred"}],
        capacity=1,
        group_limit=1,
    )
    competing = plan_rebooking(
        [{"case_id": "O", "cohort": "g", "upper": 2.0}],
        [{"case_id": "W2", "cohort": "g", "lower": 2.2, "priority": 3.0, "status": "deferred"}],
        capacity=1,
        group_limit=1,
    )
    first = register(store, outcome, 0)
    retry = register(store, outcome, 0)
    assert first.generation == retry.generation == 1 and retry.replayed
    with pytest.raises(RevisionConflict):
        register(store, competing, 0)


def test_rebook_api_registers_and_rejects_stale_shift(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WORKSHOP_DB", str(tmp_path / "http-rebook.db"))
    from kilngrader.api import app

    body = {
        "expected_generation": 0,
        "capacity": 1,
        "group_limit": 1,
        "originals": [
            {"case_id": "O", "cohort": "g", "units": 1, "priority": 1.0, "upper": 2.0}
        ],
        "waitlist": [
            {
                "case_id": "W",
                "cohort": "g",
                "units": 1,
                "priority": 2.0,
                "lower": 2.1,
                "status": "deferred",
            }
        ],
    }
    with TestClient(app) as client:
        first = client.post("/api/rebooks", json=body)
        assert first.status_code == 201
        stale_body = {
            **body,
            "rebook_id": "other-shift",
            "waitlist": [
                {
                    "case_id": "W2",
                    "cohort": "g",
                    "units": 1,
                    "priority": 3.0,
                    "lower": 2.2,
                    "status": "deferred",
                }
            ],
        }
        stale = client.post("/api/rebooks", json=stale_body)
        assert stale.status_code == 409
        assert client.get(f"/api/rebooks/{first.json()['generation']}").status_code == 200
