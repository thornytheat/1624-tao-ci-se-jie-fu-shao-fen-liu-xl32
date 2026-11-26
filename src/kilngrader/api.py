from dataclasses import asdict
from functools import lru_cache
import json
import os
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt


from .shade_disposition.service import compare_records, evidence, export_table
from .kiln_dispatch.contracts import RevisionConflict, UnknownRelease, UnknownRebook
from .kiln_dispatch.history import release_changes
from .kiln_dispatch.service import list_releases, read_release, register
from .kiln_dispatch.rebooking import (
    list_rebookings,
    plan_rebooking,
    read_rebooking,
    register_rebooking,
    outcome_delivery,
    rebook_delivery,
)
from .engine import ROOT, plan
from .settings import DEFAULT_BUDGET, DEFAULT_DAY, MODEL_LABEL, PROJECT_NAME

app = FastAPI(title=PROJECT_NAME, version="1.0.0")


SECTIONS = {
    "kiln_lots": "ingestion",
    "glaze_factors": "features",
    "shade_tree": "model",
    "cone_validation": "replay",
    "recipe_drift": "shift",
    "refire_budget": "resources",
}
Capacity = Annotated[int, Query(ge=0, le=100)]
Snapshot = Annotated[int, Query(ge=70, le=36_500)]


class Feed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observations: list = Field(max_length=2500)
    outcomes: list = Field(max_length=2500)


class PlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capacity: StrictInt = Field(default=DEFAULT_BUDGET, ge=0, le=100)
    as_of: StrictInt = Field(default=DEFAULT_DAY, ge=70, le=36_500)
    packet: Feed | None = None


class RegisterInput(PlanInput):
    expected_generation: StrictInt = Field(ge=0)
    run_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class RebookInput(BaseModel):
    model_config = ConfigDict(extra="allow")

    expected_generation: StrictInt = Field(default=0, ge=0)
    capacity: StrictInt | None = Field(default=None, ge=0, le=100)
    group_limit: int | dict[str, int] | None = None
    originals: list[dict[str, Any]] | None = None
    waitlist: list[dict[str, Any]] | None = None
    candidates: list[dict[str, Any]] | None = None
    frozen_ids: list[str] | None = None
    rebook_id: str | None = None
    run_id: str | None = None
    base_generation: StrictInt | None = Field(default=None, ge=0)
    release_generation: StrictInt | None = Field(default=None, ge=1)
    register_as_release: bool = False


def store():
    return Path(os.environ.get("WORKSHOP_DB", ROOT / "var" / "register.sqlite3"))


@app.middleware("http")
async def constrain_payload(request: Request, call_next):
    if request.method == "POST":
        declared = request.headers.get("content-length", "")
        if declared.isdecimal() and int(declared) > 2_000_000:
            return JSONResponse({"detail": "单次批次上限 2 MB"}, status_code=413)
    return await call_next(request)


@lru_cache(maxsize=24)
def sample_plan(capacity, as_of):
    return plan(as_of=as_of, capacity=capacity)


def compute(command: PlanInput):
    try:
        if command.packet is None:
            return sample_plan(command.capacity, command.as_of)
        return plan(
            command.packet.model_dump(), as_of=command.as_of, capacity=command.capacity
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/healthz")
def health():
    body = {
        "status": "ready",
        "service": PROJECT_NAME,
        "model": MODEL_LABEL,
        "default_snapshot": DEFAULT_DAY,
        "run_endpoint": "/api/plan",
        "summary_endpoint": "/api/summary",
    }
    return Response(
        json.dumps(body, ensure_ascii=False, indent=2), media_type="application/json"
    )


@app.get("/api/plan")
def trial(capacity: Capacity = DEFAULT_BUDGET, as_of: Snapshot = DEFAULT_DAY):
    return sample_plan(capacity, as_of).body


@app.post("/api/plan")
def custom_trial(command: PlanInput):
    return compute(command).body


@app.get("/api/summary")
def summary(capacity: Capacity = DEFAULT_BUDGET):
    report = sample_plan(capacity, DEFAULT_DAY).body
    return {
        "project": report["project"],
        "action": report["action"],
        "intake": report["intake"]["incoming"],
        "exceptions": report["intake"]["reasons"],
        "estimator": report["model"]["algorithm"],
        "metric": report["evaluation"]["metric"],
        "holdout": report["evaluation"]["holdout_cases"],
        "report": report["summary"],
        "first_allocations": [
            row for row in report["decisions"] if row["status"] == "allocated"
        ][:5],
    }


@app.get("/api/modules/{module}")
def module_evidence(module: str, capacity: Capacity = DEFAULT_BUDGET):
    if module not in SECTIONS:
        raise HTTPException(404, "模块不存在")
    report = sample_plan(capacity, DEFAULT_DAY).body
    return {
        "module": module,
        "run_id": report["run_id"],
        "evidence": evidence(report, SECTIONS[module]),
    }


@app.get("/api/decisions/{case_id}")
def single(case_id: str, capacity: Capacity = DEFAULT_BUDGET):
    report = sample_plan(capacity, DEFAULT_DAY).body
    entry = report["row_evidence"].get(case_id)
    if entry is None:
        raise HTTPException(404, "候选单据不存在")
    return {
        "decision": next(
            item for item in report["decisions"] if item["case_id"] == case_id
        ),
        "input_evidence": entry,
    }


@app.get("/api/compare")
def compare(capacity: Capacity = DEFAULT_BUDGET, against: Capacity = 0):
    result = sample_plan(capacity, DEFAULT_DAY).body
    reference = sample_plan(against, DEFAULT_DAY).body
    return {
        "current": result["summary"],
        "reference": reference["summary"],
        "changes": compare_records(result, reference),
    }


@app.get("/api/decisions.csv")
def download(capacity: Capacity = DEFAULT_BUDGET):
    body = sample_plan(capacity, DEFAULT_DAY).body
    return Response(
        "\ufeff" + export_table(body),
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=kilngrader-decisions.csv"
        },
    )


@app.get("/api/releases")
def releases(
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    before: Annotated[int | None, Query(gt=0)] = None,
):
    return [asdict(value) for value in list_releases(store(), limit, before)]


@app.get("/api/releases/{generation}")
def archived(generation: int):
    try:
        return asdict(read_release(store(), generation))
    except UnknownRelease as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/rebooks")
@app.get("/api/rebook")
@app.get("/api/rebookings")
@app.get("/api/rebooking")
def rebook_history(
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
):
    try:
        return [asdict(item) for item in list_rebookings(store(), limit)]
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/rebooks/{generation}")
@app.get("/api/rebook/{generation}")
@app.get("/api/rebookings/{generation}")
@app.get("/api/rebooking/{generation}")
def archived_rebook(generation: int):
    try:
        return asdict(read_rebooking(store(), generation))
    except UnknownRebook as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/rebooks", status_code=201)
@app.post("/api/rebook", status_code=201)
@app.post("/api/rebookings", status_code=201)
@app.post("/api/rebooking", status_code=201)
@app.post("/api/shift-rebook", status_code=201)
@app.post("/api/shift-rebooking", status_code=201)
def create_rebook(command: RebookInput):
    data = command.model_dump(exclude_none=True)
    expected = data.pop("expected_generation", 0)
    release_generation = data.pop("release_generation", None)
    as_release = data.pop("register_as_release", False)
    rebook_id = data.pop("rebook_id", None)
    run_id = data.pop("run_id", None)
    base_generation = data.pop("base_generation", None)
    try:
        if release_generation is not None:
            base = read_release(store(), release_generation)
            delivery = rebook_delivery(base, **data)
            if command.run_id and delivery.digest != command.run_id:
                raise HTTPException(409, "改签证据已经变化，请按新方案重新核对")
            receipt = register(store(), delivery, expected)
            return {
                **asdict(receipt),
                "rebook_evidence": delivery.body["same_day_rebook"],
            }
        outcome = plan_rebooking(**data)
        if as_release:
            receipt = register(store(), outcome_delivery(outcome), expected)
        else:
            receipt = register_rebooking(
                store(),
                outcome,
                expected,
                rebook_id=rebook_id or run_id,
                base_generation=base_generation,
            )
        return asdict(receipt)
    except UnknownRelease as exc:
        raise HTTPException(404, str(exc)) from exc
    except RevisionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/release-differences")
def release_diff(
    before: Annotated[int, Query(gt=0)], after: Annotated[int, Query(gt=0)]
):
    try:
        return release_changes(store(), before, after)
    except UnknownRelease as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/releases", status_code=201)
def publish(command: RegisterInput):
    delivery = compute(command)
    if not delivery.quantity:
        raise HTTPException(422, "没有可登记的候选单据")
    if delivery.digest != command.run_id:
        raise HTTPException(409, "待登记证据已经变化，请按新方案重新核对")
    try:
        return asdict(register(store(), delivery, command.expected_generation))
    except RevisionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
