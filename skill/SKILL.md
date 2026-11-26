---
name: jobjobvein
description: 通过本机 jobjobvein 采集服务抓取并管理 BOSS 直聘岗位原文：发起采集、查进度与结果、列来源与公司、处理字段级冲突裁决、核对原件证据。当用户要求采集/重采职位、查询已采集岗位、处理待裁决冲突、导出岗位原文，或提到 jobjobvein、岗位台账、BOSS 采集服务时使用。服务需先在本机启动且 9222 调试浏览器已登录 BOSS；不负责替用户扫码登录。
agent_created: true
---

# jobjobvein 采集服务

## 用途

jobjobvein 是本机运行的岗位采集与证据台账服务：从 BOSS 直聘抓岗位原文，存本地 SQLite，提供 Web 页面和 HTTP 接口。本 skill 让 agent 调用这套接口完成采集和管理，不依赖网页点击。

核心特点：只存原文与少量原生结构化字段，零 token、不调大模型；每条岗位可回溯到原件。

## 使用前检查（不可跳过）

1. 确认服务已启动。客户端脚本连不上时，提示用户在库目录执行 `python -m jobjobvein`，不要自行尝试其他端口。
2. 运行 `python scripts/client.py ready`，确认输出 `can_collect: true`。
3. 若 `can_collect: false`，属于认证/环境问题：提示用户用库的 `scripts/start_all.py` 启动专用调试浏览器并扫码登录 BOSS，然后重跑 `ready`。**不要**反复发起采集，也不要声称登录已完成。

所有命令都在本 skill 目录下执行，脚本只用 Python 标准库，不需要安装额外包。默认服务地址 `http://127.0.0.1:8770`；若用户改过端口，用 `--base` 指明。

## 工作流

### 采集岗位

1. `python scripts/client.py collect --keyword "AI" --cities "成都" --max-candidates 100`
   - 不用填关键词/城市时直接省略，服务用配置默认值（`ai` / `成都`）。
   - 采集单条职位详情用 `--source-url <BOSS详情URL>`，此时不能带 `--keyword` / `--cities`。
   - 脚本会自动生成唯一 `task_id` 并启动任务，立即返回。
2. 等待完成：`python scripts/client.py wait <task_id>`；或采集时直接加 `--wait`。
3. 取结果：`python scripts/client.py results <task_id> --page 1 --page-size 20`。
4. 需要中途停止：`python scripts/client.py stop <task_id>`，再用 `task` 查是否真正停止。

任务状态是轮询语义：发起后每隔 1–2 秒查 `task`，到 `finished/failed/stopped` 才算终态。不要假设启动返回时数据已就绪。

### 查看数据

- 列来源：`python scripts/client.py sources [--channel boss] [--city 成都] [--status new] [--sort recent]`
- 单条详情：`python scripts/client.py source <source_id>`
- 列公司：`python scripts/client.py companies`
- 查原件：接口 `GET /artifacts/{id}`，用浏览器或 curl 打开核对原文。

### 处理冲突

冲突只列真有字段差异的项，不要向用户堆砌「看起来一样」的项。

1. `python scripts/client.py conflicts --status pending`
2. `python scripts/client.py conflict <conflict_id>` 读字段对比，向用户展示 old/new 差异摘要。
3. 用户拍板后裁决：
   - 保留旧值：`decide <id> --decision keep_old --revision <当前revision>`
   - 采用新值：`decide <id> --decision keep_new --revision <当前revision>`
   - 逐字段合并：`decide <id> --decision keep_merge --revision <当前revision> --fields '{"title_raw":"new","salary_raw":"old"}'`

`revision` 必须从第 2 步的详情里取，防止并发覆盖。没有用户明确指示时，不要自行裁决。

## 失败分级（必须遵守）

按返回的 `error.category` 判断，不要只看 message：

| 情况 | 处理 |
|---|---|
| 连不上服务 | 提示用户启动 `python -m jobjobvein` |
| `ready=false` / 登录态错误 | 提示用户扫码登录 9222 浏览器，不重试采集 |
| 有职位但薪资等字段空 | 记为字段缺失（schema 级），不要报成未登录；详情层会补读薪资 |
| message 含「环境异常」「访问频繁」 | 判为风险控，等待并降低条数后重试 |
| category=参数错误 | 修正请求字段，不要原样重试 |

「已发起采集」不等于「数据已入库」；只有 `results` 里查到记录才算采到。

## 资源

- `scripts/client.py` — 全部 HTTP 接口的命令行封装，标准库实现，输出 JSON。
- `references/api.md` — 16 个接口的参数、返回结构、任务类型、失败分类。遇到脚本未覆盖的接口或需要核对字段时读取。
