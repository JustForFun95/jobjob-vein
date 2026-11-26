# jobjobvein HTTP 接口参考

服务前缀：`/api/jobjobvein`，默认地址 `http://127.0.0.1:8770`。只允许本机回环访问；写操作必须带 `application/json`。

响应统一为 `{"data": ..., "error": null}`；失败时 `error` 含 `category`（分类）、`error_code`、`message`、`object_id`。agent 应优先按 `category` 判断失败类型，不要只看 `message`。

## 就绪与限制

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/capabilities` | 返回 `channels`（各渠道 `ready`/`login_state`）、`limits`、`default_search`。采集前必须确认 boss 的 `ready=true`。 |

`ready=false` 属于认证/环境问题：服务只认本机 9222 调试浏览器里已登录 BOSS 的会话，不会替用户扫码。

## 任务

| 方法 | 路径 | 请求体 / 参数 | 说明 |
|---|---|---|---|
| POST | `/tasks` | `{"task_id": "...", "task_type": "collect", ...}` | 登记任务，幂等：相同 task_id 但请求不同会报错。 |
| POST | `/tasks/{id}/run` | 空 `{}` 即可 | 启动任务，返回 `202`。后端异步执行，立即返回。 |
| POST | `/tasks/{id}/stop` | 空 `{}` 即可 | 请求停止；等待可中断，不是瞬时生效。 |
| GET | `/tasks/{id}` | — | 查状态：`ready`/`running`/`finished`/`failed`/`stopped`，含进度与统计。 |
| GET | `/tasks/{id}/results` | `page`、`page_size`、`company_source_id`、`source_id`、`acquisition_context` | 分页取结果，`page_size` 上限 100。 |

`task_type` 可选值：

| 值 | 用途 | 关键字段 |
|---|---|---|
| `collect` | 关键词/城市搜索采集 | `channel`、`keyword`、`cities`、`limits.max_candidates` |
| `collect` | 单链接采集 | `source_url`（与上者互斥，不同时有 cities/keyword） |
| `recollect` | 重采单条 | `source_id` |
| `company_collect` | 公司采集 | `company_source_id` |
| `import` | 材料导入 | `materials`（非空），不接受 cities/keyword |

`limits.max_candidates`：1–500（默认 100、上限取 capabilities 的 `limits.max_candidates`）；`limits.timeout_seconds`：10–600。`task_id` 必须非空字符串，建议用 `agent-<uuid>` 避免冲突。

任务状态是轮询语义：发起后每隔 1–2 秒查 `/tasks/{id}`，到 `finished/failed/stopped` 为终态。不要假设 `run` 返回时数据已就绪。

## 来源与公司

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/sources` | 列出来源岗位，支持 `channel`、`city`、`status`、`keyword`、`salary`、`conflict`、`sort` 等查询参数。 |
| GET | `/sources/{id}` | 单条来源详情。 |
| PATCH | `/sources/{id}/status` | `{"status": ..., "expected_revision": N}` 改状态，带版本号防并发覆盖。 |
| GET | `/company-sources` | 列出公司来源。 |
| GET | `/company-sources/{id}` | 单条公司来源。 |

## 冲突裁决

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/conflicts` | 列出冲突，可用 `status=pending` 过滤。只列真有字段差异的冲突。 |
| GET | `/conflicts/{id}` | 冲突详情，含逐字段对比（old/new），裁决前应先读它。 |
| POST | `/conflicts/{id}/decision` | `{"decision": ..., "expected_revision": N, "fields": {...}}` |

`decision` 取值：

- `keep_old` — 保留旧值
- `keep_new` — 采用新值
- `keep_merge` — 字段级合并，必须给 `fields`，例如 `{"title_raw": "new", "salary_raw": "old"}`，键为字段名、值为 `old` 或 `new`

`expected_revision` 必须传当前冲突的版本号，防止两人同时裁决互相覆盖。

## 原件

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/artifacts` | multipart：`file` 字段 + `content_type`（必填）+ 可选 `acquired_at`。 |
| GET | `/artifacts/{id}` | 取回原件内容，用于核对证据原文。 |

## 失败分级（agent 必须遵守）

| 类别 | 现象 | 处理 |
|---|---|---|
| 连接失败 | 客户端报连不上 base | 服务没启动；提示用户运行 `python -m jobjobvein` |
| auth / 未就绪 | `ready=false` 或登录态错误 | 提示用户启动 9222 调试浏览器并扫码登录，不重试采集 |
| schema / 字段缺失 | 有职位但某字段空 | 记录为字段缺失，不要报成「没登录」；详情层会补读薪资 |
| policy / 风控 | message 含「环境异常」「访问频繁」 | 等待后重试，降低条数 |
| 参数错误 | category=参数错误 | 修正请求字段，不要原样重试 |
