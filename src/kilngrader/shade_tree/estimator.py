from math import fsum
from statistics import mean


MAX_DEPTH = 5
LEAF_SUPPORT = 10


def _glaze_node(points, depth):
    labels = [v for _, v in points]
    center = mean(labels)
    parent_error = fsum((y - center) ** 2 for y in labels)
    node = {"shade": center, "size": len(points), "squared_error": parent_error}
    if depth >= MAX_DEPTH or len(points) < LEAF_SUPPORT * 2 or parent_error < 1e-9:
        return node
    best = None
    for factor in range(len(points[0][0])):
        ranked = sorted(points, key=lambda row: row[0][factor])
        left_sum = left_sq = 0.0
        total = fsum(y for _, y in ranked)
        square = fsum(y * y for _, y in ranked)
        for idx, (profile, shade) in enumerate(ranked[:-1], 1):
            left_sum += shade
            left_sq += shade * shade
            tail = len(ranked) - idx
            if (
                min(idx, tail) < LEAF_SUPPORT
                or profile[factor] == ranked[idx][0][factor]
            ):
                continue
            impurity = max(0.0, left_sq - left_sum**2 / idx) + max(
                0.0, square - left_sq - (total - left_sum) ** 2 / tail
            )
            cut = (profile[factor] + ranked[idx][0][factor]) / 2
            key = (impurity, factor, cut)
            if best is None or key < best[0]:
                best = (key, ranked[:idx], ranked[idx:])
    if best is None or parent_error - best[0][0] < 0.07:
        return node
    (_, factor, cut), lower, upper = best
    node.update(
        axis=factor,
        limit=cut,
        cold=_glaze_node(lower, depth + 1),
        warm=_glaze_node(upper, depth + 1),
    )
    return node


def train(rows):
    return _glaze_node([(row.raw, row.y) for row in rows], 0)


def infer(state, signal):
    branch = state
    while "axis" in branch:
        branch = (
            branch["cold"]
            if signal.vector[branch["axis"]] <= branch["limit"]
            else branch["warm"]
        )
    return branch["shade"]


def explain(state):
    def leaves(node):
        return leaves(node["cold"]) + leaves(node["warm"]) if "axis" in node else 1

    return {
        "leaves": leaves(state),
        "root_feature": state.get("axis"),
        "root_cutoff": state.get("limit"),
        "baseline_error": state["squared_error"],
        "min_leaf_support": LEAF_SUPPORT,
    }
