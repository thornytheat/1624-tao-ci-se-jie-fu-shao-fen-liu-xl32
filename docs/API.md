# 服务接口

`GET /api/plan?capacity=20` 返回默认快照下的完整方案。`POST /api/plan` 支持提交本地记录，`packet` 内仅含 `observations` 和 `outcomes` 列表。

`GET /api/summary` 返回简读版。`GET /api/decisions/<case_id>` 回读对应测量与标准化特征。`GET /api/compare?capacity=20&against=0` 显示两档额度的逐单状态变更。

以下模块证据可通过 `/api/modules/` 下的相同名称访问。

- `kiln_lots`
- `glaze_factors`
- `shade_tree`
- `cone_validation`
- `recipe_drift`
- `refire_budget`

`GET /api/decisions.csv` 导出本次候选。`POST /api/releases` 接受规划请求的原参数及 `expected_generation`、`run_id`。重试同一个运行标识返回原版本，试图用过期版本登记其他方案返回 409。

同日改签使用 `POST /api/rebooks`（别名 `/api/rebookings`）。请求可直接给 `originals`、`waitlist`、`capacity`、`group_limit`、`frozen_ids` 和 `expected_generation`；也可传 `release_generation` 从已登记方案读取原单与候补并生成新的 release。`GET /api/rebooks` 与 `GET /api/rebooks/{generation}` 回读改签历史。两班基于同一版本提交不同内容时，先登记者生效，过期版本的落败重试返回 409。
