from math import fsum
from statistics import median


def _maturity(record):
    v = record.record.reported
    if None in (v["soak_temp"], v["cool_rate"], v["glaze_sg"]):
        return None
    return (v["soak_temp"] - 1000) / max(1, v["cool_rate"] + 3) * v["glaze_sg"]


def context(current, neighbors, old, score):
    here = _maturity(current)
    readings = current.record.reported
    threshold = readings["sheet_area"]
    comparable = []
    for row in old:
        sampled = _maturity(row)
        if here is not None and sampled is not None and 0 <= abs(sampled - here) <= 8:
            comparable.append(sampled)
    second_pass = fsum(
        row.record.reported.get("sheet_area") or 0.0
        for row in neighbors
        if row.record.reported.get("refires", 0)
        and row.record.clock // 12 == current.record.clock // 12
    )
    hold = (
        here is not None
        and second_pass > 120
        and readings.get("refires") == 1
        and (threshold or 0) > 42
    )
    return {
        "decision": "同坯类二次烧成负载超出炉位限制" if hold else "",
        "support": len(comparable),
        "heat_index": here,
        "peer_median": median(comparable) if comparable else None,
        "second_fire_area": round(second_pass, 3),
        "severity_area": score * (threshold or 0),
    }
