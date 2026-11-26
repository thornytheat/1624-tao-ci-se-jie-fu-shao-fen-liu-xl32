# 模块与边界验证

所属领域 AI/ML，子方向 传统ML。

## 功能模块

| 序号 | 模块 | 职责 | 实现 | 测试 |
| --- | --- | --- | --- | --- |
| M1 | kiln_lots | 单据接入与时点裁决 | `src/kilngrader/kiln_lots/service.py` | `tests/test_kiln_lots.py` |
| M2 | glaze_factors | 业务特征与时点档案 | `src/kilngrader/glaze_factors/service.py` | `tests/test_glaze_factors.py` |
| M3 | shade_tree | 训练、估计与基线 | `src/kilngrader/shade_tree/service.py` | `tests/test_shade_tree.py` |
| M4 | cone_validation | 时间回放与核验 | `src/kilngrader/cone_validation/service.py` | `tests/test_cone_validation.py` |
| M5 | recipe_drift | 群组偏移与可信分派 | `src/kilngrader/recipe_drift/service.py` | `tests/test_recipe_drift.py` |
| M6 | refire_budget | 业务锁与资源分派 | `src/kilngrader/refire_budget/service.py` | `tests/test_refire_budget.py` |
| M7 | shade_disposition | 逐单证据与资源守恒 | `src/kilngrader/shade_disposition/service.py` | `tests/test_shade_disposition.py` |
| M8 | kiln_dispatch | 版本登记与追溯 | `src/kilngrader/kiln_dispatch/service.py` | `tests/test_kiln_dispatch.py` |

## 易错边界

| 用例 | 业务后果 | 处理位置 | 验证位置 |
| --- | --- | --- | --- |
| TRAP-14 | 同版本异值隔离，较旧修订不能复活 | `src/kilngrader/kiln_lots/service.py` | `tests/test_kiln_lots.py::test_conflicting_revision_is_quarantined` |
| TRAP-19 | 业务锁优先，未获准的单据不消耗资源 | `src/kilngrader/refire_budget/service.py` | `tests/test_refire_budget.py::test_prohibited_entries_never_spend` |
| TRAP-26 | 使用过期版本登记被拒，保留前次结果 | `src/kilngrader/kiln_dispatch/service.py` | `tests/test_kiln_dispatch.py::test_stale_generation_keeps_prior` |
| TRAP-32 | 零方差业务特征仍有有界尺度 | `src/kilngrader/glaze_factors/service.py` | `tests/test_glaze_factors.py::test_zero_variance_uses_unit_scale` |
| TRAP-42 | 训练冻结后到达的单据或回执不会回填 | `src/kilngrader/glaze_factors/service.py` | `tests/test_glaze_factors.py::test_future_and_late_measurements_do_not_fit` |
| TRAP-43 | 训练与独立回放的单据按时段隔离 | `src/kilngrader/cone_validation/service.py` | `tests/test_cone_validation.py::test_holdout_separates_ordered_periods` |
| TRAP-44 | 稀疏或常量参考出现越界值时拦住自动分派 | `src/kilngrader/recipe_drift/service.py` | `tests/test_recipe_drift.py::test_constant_distribution_detects_outside` |
| TRAP-53 | 同批重排仍得到相同证据和决策 | `src/kilngrader/shade_disposition/service.py` | `tests/test_shade_disposition.py::test_report_is_permutation_invariant` |
| TRAP-54 | 单笔推理与合并调用的结果一致 | `src/kilngrader/shade_tree/service.py` | `tests/test_shade_tree.py::test_score_is_batch_invariant` |
