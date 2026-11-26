# jobjobvein · 职位来源与采集

把取得的职位资料，处理成**有原件依据、可查询、可维护**的来源记录。这是求职系统里的模块 02，其他模块直接读它的 SQLite 数据。

- 一份资料 → 一份不可覆盖的原件（页面 HTML / 纯文本）
- 一条来源 → 当前采用字段 + 原件引用 + 取得时间 + 问题说明
- 原文 raw 与少量原生结构化字段并存；每条记录可回溯到原件
- 同一渠道同一引用重复出现 → 只更新核对时间，不重复建记录
- 业务含义变了、字段级判得准 → 逐字段合并；判不准 → 存冲突，等人工裁决

## 它不做什么

- 不做职位机会、匹配打分、优先级、投递（属于其他模块）
- 不判断“BOSS 上的岗位和官网是同一个真实岗位”
- 不调大模型、不做语义提取、不做 CSV 导出、不做永久删除
- 不实现登录/角色/令牌（首版本机单管理员）；BOSS 登录态由你自己的浏览器持有，程序只读取、不代登录、不存 Cookie
- 截图不做 OCR（首版只存原件）

## 启动

需要 Python 3.11+（依赖里 DrissionPage 可用 3.11/3.12）。在 `project/` 目录执行：

```bash
pip install -r requirements.txt
python -m jobjobvein                 # 默认 http://127.0.0.1:8770
```

管理员页面：<http://127.0.0.1:8770/>（单文件前端，无框架、无 CDN、无构建）。

一键准备好采集环境（专用浏览器 + 服务 + 页面）：

```bash
python scripts/start_all.py
python scripts/start_all.py --check  # 只看状态，不启动任何东西
```

第一次要在弹出的独立浏览器里扫码登录 BOSS；登录态留在 `data/chrome-profile`，不进仓库。Chrome 136 起禁止在默认用户目录开调试端口，所以必须用独立目录——你日常双击的 Chrome 连不上，这不是配置问题。

## 目录

```text
jobjobvein/
├── __main__.py       启动入口（python -m jobjobvein）
├── api.py            HTTP 路由、回环与跨站限制、响应封装
├── service.py        任务登记/执行/停止、逐条提交、冲突裁决
├── sources.py        同来源判定、原文字段比对、字段级合并
├── normalization.py  身份键、薪资/地点/文本规范化、内容标识
├── config.py         配置加载
├── errors.py         失败分级（transport/schema/auth/policy）
├── adapters/         boss（DrissionPage 连本机调试浏览器）、manual（已有资料）
├── storage/          SQLite（WAL、外键）、原件文件、仓储
├── web/index.html    管理员页面
├── skill/            给 agent 用的 skill（SKILL.md + client.py + api.md）
├── dist/             skill 打包产物
└── README.md
```

## 怎么用

页面四个页签：来源、公司、待裁决、任务。采集参数折叠在「来源」页顶部，进度与停止按钮在列表头——不用切页。

- **采集**：填关键词、城市、条数上限，点「开始采集」。留空走默认值（关键词 `ai`、城市 `成都`、上限 100）。可异步启动、看实时进度、随时停止。
- **进度**：列表头显示运行中/用时/已读条数；本次新建的条目标「新」并置顶。
- **筛选排序**：渠道、城市、状态、薪资、冲突筛选；按更新时间/新增/薪资/公司名排序；每条显示更新时间。
- **待裁决**：只列真正的字段级冲突，展示逐字段对比和理由，支持逐字段合并，不是整份二选一。
- **单链接采集**：贴一条 BOSS 职位链接，走人工入口落库。

## 配置

配置文件 `project/config/jobjobvein.toml`，改完重启生效。**不放任何明文凭据**——渠道登录态留在你自己的浏览器里。

关键项：服务只监听 `127.0.0.1`；采集总量走 `default_candidates`（默认 100）/ `max_candidates`（上限 500），翻页封顶 30；默认搜索 `ai` / `成都`；BOSS 调试端口 `127.0.0.1:9222`；详情页、翻页各有随机延迟（调小会更快，但风控风险由调的人承担）。

## 数据怎么被其他模块用

数据在 `project/data/jobjobvein/`：`source.sqlite3`（六张业务表 + `module_meta`，`PRAGMA user_version=110`）和 `artifacts/`（原件文件）。

下游模块两条接法：

1. **直接读 SQLite**（只读）：核心表 `source_records`（当前采用字段 + raw）、`company_source_records`、`source_conflicts`、`raw_artifacts`、`collection_tasks`、`collection_results`。
2. **调仓储层**：`from jobjobvein.storage.repositories import Repo`，避免自己拼 SQL。

HTTP 接口是给外部与 agent 用的；模块之间在同一目录树下直接读数据即可，不必绕接口。

## HTTP 接口

前缀 `/api/jobjobvein`，共 16 个路由：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/capabilities` | 渠道就绪、登录态、预算上限 |
| GET | `/sources` | 来源分页、筛选、排序 |
| GET | `/sources/{id}` | 单条来源详情 |
| PATCH | `/sources/{id}/status` | 停用/恢复 |
| GET | `/company-sources` | 公司维度来源列表 |
| GET | `/company-sources/{id}` | 单条公司来源 |
| GET | `/conflicts` | 待裁决冲突列表 |
| GET | `/conflicts/{id}` | 冲突详情与字段对比 |
| POST | `/conflicts/{id}/decision` | 逐字段裁决（合并/采用/保留） |
| POST | `/tasks` | 登记任务（collect / company_collect / 单链接） |
| POST | `/tasks/{id}/run` | 执行 |
| POST | `/tasks/{id}/stop` | 协作停止 |
| GET | `/tasks/{id}` | 进度 |
| GET | `/tasks/{id}/results` | 逐条结果 |
| POST | `/artifacts` | 上传原件 |
| GET | `/artifacts/{id}` | 读原件（按纯文本返回，不执行） |

参数与返回见 `skill/references/api.md`。

## 失败分级

失败按四类记，不混为一谈：

| 类别 | 含义 |
|---|---|
| `transport` | 网络/浏览器连不上、超时 |
| `schema` | 页面结构变了、字段取不到。BOSS 列表接口本就不返回薪资（`salaryDesc` 为空是正常的），薪资去详情页读，不能判成未登录 |
| `auth` | 未登录、掉登录、需验证码 |
| `policy` | 站点明确拒绝、风控拦截 |

字段缺失归 `schema`，不报成没登录。「已发送」不等于「对方收到」，投递回执不由本模块负责。

## 数据与隐私

- 服务只监听 `127.0.0.1`，拒绝非回环来源和跨站写入
- `data/`、`*.sqlite3`、本地配置已在 `.gitignore`；**不要提交**原件（含 HR 姓名、联系方式）
- 升级或迁移前先停服务、checkpoint、整目录备份

## 测试

```bash
cd project && python -m pytest tests/ -q       # 48 项全过
```

测试用模拟数据，证明数据规则与接口契约；**不证明真实 BOSS 渠道已接通**——那需要本机已登录的调试浏览器。

设计依据见 `../modules/02-职位来源与采集.md` 与 `../contracts/`。
