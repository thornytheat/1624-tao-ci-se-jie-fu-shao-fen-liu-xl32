from kilngrader.shade_tree.estimator import _glaze_node


def test_glaze_tree_uses_observed_separation_without_thin_leaf():
    points = [((x, 0.0, 0.0, 0.0), 3.5 if x >= 20 else 0.75) for x in range(40)]
    tree = _glaze_node(points, 0)
    assert tree["axis"] == 0 and tree["limit"] == 19.5
    assert tree["cold"]["size"] == tree["warm"]["size"] == 20
    assert tree["cold"]["shade"] == 0.75 and tree["warm"]["shade"] == 3.5
