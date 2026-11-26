from collections import defaultdict
from math import isfinite

REVIEW_NAME = "复烧热负荷与试片保存"
IMPACT_NAME = "色差面积暴露/ΔE·m²"
HEADROOM_NAME = "可用二次炉位/m²"
OVERRIDE_NOTE = "二次复烧之前必须切小试片"


def operational_check(readings, estimate):
    """同等预测偏色时优先复核受影响面积较大的批次，同时保护二次复烧样片。"""
    refires = readings["refires"]
    sheet_area = readings["sheet_area"]
    impact = estimate * sheet_area
    headroom = 52 - sheet_area - 8 * refires
    override = bool(sheet_area > 55 and refires > 0)
    if not isfinite(impact) or not isfinite(headroom):
        raise ValueError("业务复核的观测量超出计算范围")
    return {
        "name": REVIEW_NAME,
        "impact_label": IMPACT_NAME,
        "headroom_label": HEADROOM_NAME,
        "impact": round(impact, 6),
        "headroom": round(headroom, 6),
        "requires_operator": override,
        "reason": OVERRIDE_NOTE if override else "",
    }


def summarize_workload(actions, evidence):
    distribution = defaultdict(
        lambda: {
            "selected_impact": 0.0,
            "selected": 0,
            "tight_window": 0,
            "needs_operator": 0,
        }
    )
    for action in actions:
        branch = distribution[action.cohort]
        current = evidence[action.case_id]["operational_check"]
        if action.status == "allocated":
            branch["selected"] += 1
            branch["selected_impact"] += current["impact"]
            if current["headroom"] < 0:
                branch["tight_window"] += 1
        if current["requires_operator"]:
            branch["needs_operator"] += 1
    return {
        name: {**data, "selected_impact": round(data["selected_impact"], 4)}
        for name, data in sorted(distribution.items())
    }
