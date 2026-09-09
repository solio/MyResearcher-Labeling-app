# HANDOFF — MyResearcher-Labeling-app

供后续集成审阅（含 GPT 集成审阅）。交付日期：2026-09-07；phase6（MySQL 适配）2026-09-08。

## 1. 项目边界（铁律执行情况）

- 未读写 `MyResearcher-ModelTraining` 内任何文件；唯一例外是**只读**其
  `schema/semantic-schema-calibrated-v0.2.1.json`，用于抄录 7 个 head 的
  `class_order` 与标签定义（顺序未改动，见 `schema/annotation-schema.v1.json` 的
  `note` 字段）。该仓库无任何修改。
- 默认后端仅 Python 标准库（http.server / sqlite3 / json / argparse / csv / zlib /
  struct），零 pip 依赖；**mysql 模式为 owner 后续要求（phase6），需
  `pip install pymysql`（纯 Python 驱动，唯一非标准库依赖，仅 storage=mysql 时 import），
  sqlite 模式保持零安装**。前端原生 JS，零构建、零 CDN、零框架。
- 无任何模型预测/预填答案展示（UI 与 API 均不含预测字段）。

## 2. 实际运行的验证命令与退出码

| Phase | 命令 | 结果 |
|---|---|---|
| P1 | `python3 tools/seed_demo.py` | EXIT=0，batch=demo 20 samples × 7 heads = 140 assignments |
| P1 | `python3 tools/import_batch.py --batch rt --file /tmp/…missing.jsonl --heads stance`（缺 text） | EXIT=1，FAIL-CLOSED，库内 0 行 |
| P1 | 同上（文件内重复 sample_id） | EXIT=1，FAIL-CLOSED，库内 0 行 |
| P1 | 同上（`--heads not_a_head`） | EXIT=1，FAIL-CLOSED |
| P1 | `python3 tools/import_batch.py --batch rt --file rt.jsonl --heads target_mode,reasoning_tags` | EXIT=0，4 assignments |
| P1 | `python3 tools/export_annotations.py --format jsonl --db data/labeler.db` | EXIT=0，144 行；断言 metadata/split_provenance/scheme_version 原样一致 |
| P1 | `… --format csv` / `… --final-only` | EXIT=0，CSV 9 列；final-only 过滤生效 |
| P1 | curl `/api/batches` `/api/resume` `/api/assignments` `/api/assignment` | 全部 200，结构符合第 4 节 |
| P1 | POST `/api/annotations` 同 id 两次 | revision 1→2，库内 1 行（幂等 upsert）；非法标签 HTTP 400；未知 assignment HTTP 404 |
| P1 | `curl --path-asis /../server.py` | HTTP 403（路径穿越防护） |
| P2 | `node --check static/app.js` | OK |
| P2 | 360×800 视口（同源 iframe 模拟，innerWidth/innerHeight=360/800） | `scrollWidth=360` 无横向滚动；四段布局 topbar 0–59 / 卡片 69–199 / 问题句 209–257 / 标签区 257–739 / 底栏 739–800；标签按钮高 54px；长文卡内滚动（scrollHeight 2874 vs clientHeight 336）；截图存档 |
| P2 | 点击标签 → POST → toast"已入库"；更多信息 sheet 7 行元数据 | 通过，sheet 关闭后选中态保留 |
| P3 | head"?"sheet / 标签"ⓘ"sheet / ⇄ 换 head 流程 | 打开关闭不丢状态；换到 stance（5 标签）、reasoning_tags（15 标签）正常 |
| P3 | 多选 toggle ×2 →"完成本条" | 每次 toggle 即存 draft（revision 递增）；final 后 DB `is_final=1`、status=completed、自动跳下一条 |
| P3 | disposition 跳过 | `disposition=跳过` 落库，status=completed，跳下一条；稍后再看停留本条 |
| P4 | 服务停止时点击 | localStorage 队列 1 条 + 横幅"本机已保存 · 待同步 1 条"+ toast；UI 选中态即时更新 |
| P4 | 服务恢复（约 5s 重试定时器） | 自动重试 → 队列 0、横幅消失、DB revision+1、jsonl 追加、toast"已入库" |
| P4 | 刷新/关页重开（服务在线） | 经 `/api/resume` + localStorage 恢复原 batch/head/sample 与已选答案（demo-003 五标签恢复） |
| P5 | `python3 -m unittest discover tests` | **Ran 15 tests … OK**，EXIT=0（幂等 upsert、round-trip jsonl+csv、fail-closed ×5、resume 生命周期、静态/参数校验/glossary） |
| P6 | `python3 -m unittest discover tests` | **Ran 28 tests … OK (skipped=1)**，EXIT=0（原 15 个不改一行仍绿；新增 12 配置/工具用例 + 1 opt-in MySQL） |
| P6 | `MR_LABELER_TEST_MYSQL='{…13306…}' python3 -m unittest tests.test_config_and_tools.TestMysqlStore` | **Ran 2 tests … OK**，EXIT=0（docker mysql:8.0.46：utf8mb4 中文往返、幂等 upsert、fail-closed 回滚、resume 生命周期、export 过滤） |
| P6 | `python3 tools/gpt_tasks.py add --batch e2e --file data/e2e_samples.jsonl --heads stance,target_mode --config data/config.test.json` | EXIT=0，storage=mysql，4 assignments |
| P6 | curl（8788，mysql 模式）`/api/batches` `/api/resume` POST `/api/annotations` | 200；BULL final revision=1；非法标签 400；resume 随 final/草稿正确移动 |
| P6 | `python3 tools/gpt_tasks.py pull --config …`（jsonl / `--final-only --format csv`） | EXIT=0；jsonl 4 行（中文 metadata 经 utf8mb4 无损）；csv 仅 final 行 |
| P6 | `python3 server.py --port 8789`（无 config.json，sqlite 默认） | EXIT=0，`/api/batches` 返回 demo batch——历史用法零变化 |
| P6 | `git check-ignore config.json data/config.test.json` | 均命中 .gitignore（凭据不入库） |
| P6b | `python3 -m unittest discover tests`（含新增 tests/test_subpath.py） | **Ran 30 tests … OK (skipped=1)**，EXIT=0 |
| P6b | 浏览器实测 `/labeler/` 前缀挂载（prefix-mounter 模拟 nginx 剥前缀） | resume→多选 toggle draft→完成本条 final(revision=2)→自动跳下一条→⇄返回选择屏全通过；网络面板确认全部请求命中 `/labeler/api/...`；服务端 `done=1`、is_final/answer 正确；静态/manifest 走相对路径 |
| P6c | `docker compose up -d --build`（本机 Docker Desktop，mysql:8.0 + python:3.12-slim） | 双容器 Up，mysql healthcheck 通过后 labeler 才启动；`docker compose logs` 可见 `storage=mysql / mysql://labeler@mysql:3306/...`（Dockerfile 已加 PYTHONUNBUFFERED=1） |
| P6c | compose 全链路：宿主机 `gpt_tasks.py add`（127.0.0.1:13306）→ 容器 `GET /api/batches` → POST 标注（is_final=1）→ 宿主机 `gpt_tasks.py pull --final-only` | 全通：GPT 写入的 batch 网页立即可见；拉回 `answer=BULL` 与 metadata（utf8mb4 无损）；审计行落宿主机 `./data/annotations.jsonl`（容器内路径 /app/data/，volume 挂载生效） |
| P6c | `python3 -m unittest discover tests`（回归） | **Ran 30 tests … OK (skipped=1)**，EXIT=0 |
| P6c | `docker compose down -v` | 容器/网络/volume 全部移除；临时 config.json/.env 删除（gitignore 命中） |
| P6d | `python3 tools/setup_deploy.py --gpt-host 203.0.113.10` | EXIT=0；生成 .env + config.json（随机密码两份一致、chmod 600）并打印 GPT 端配置；再跑一次 EXIT=2（拒绝覆盖） |
| P6d | 生成物 `load_config('config.json')` + `docker compose up -d --build` + `curl /api/batches` + `down -v` | 严格校验通过；双容器 healthy、API 返回 `[]`；随后清理（镜像缓存保留） |
| P6d | `git rm deploy.sh`（rsync 同步方案废除：代码经 git clone 分发） | deploy.sh/.dockerignore/README/HANDOFF 同步更新 |
| P6e | 用户拍板 MySQL 用服务端已有实例：compose 精简为仅 labeler，删 mysql 服务/13306/.env；`setup_deploy.py` 重写为生成指向已有 MySQL 的 config.json | E2E 拓扑=本机 mysql 容器当"宿主机 MySQL"（13306），labeler 容器经 `host.docker.internal` 连接 |
| P6e | labeler 镜像 `docker buildx build --platform linux/amd64` → 推 `fangzuzu-…cr.aliyuncs.com/fangzuzu/labeler:v1` | registry 端 imagetools inspect 确认 **linux/amd64**（附带 unknown/unknown attestation，拉取自动忽略）；服务器不 build、不依赖 Docker Hub |
| P6e | E2E 发现真 bug：GPT `add` 提交后容器侧 POST 立即 404"assignment 不存在"，片刻后又可见 | 根因：MysqlStore `autocommit=False` + 单条长连接，纯读请求不结束事务，REPEATABLE READ 快照冻结在首次读之前；此前 phase6 E2E 顺序侥幸未触发 |
| P6e | 修复：连接改 `autocommit=True`（读永远新快照），`import_samples`/`upsert_annotation` 显式 `START TRANSACTION` 保 fail-closed 原子性 | **Ran 32 tests … OK**（含 MySQL opt-in ×2，EXIT=0）；E2E 重验「容器先读→GPT add→立即 POST→pull」：POST ok=True revision=1，pull 拉回 final 行 |
| P7 | 上线（2026-09-08）：服务器 git clone + setup_deploy.py（`--mysql-host 172.21.153.219`，fzz-config 库）+ compose up | `https://testapi.zuzurent.com.cn/labeler/` 全链路通（nginx 子路径反代→容器→fzz-config MySQL）；本机 gpt_tasks 经公网 3306 add/pull 实测通；中间曾因服务端 config `storage=sqlite` 出空批次（mysql 段被总开关忽略），改回 mysql 后正常 |
| P7 | `setup_deploy.py --gpt-host` 改可选（默认不打印本机配置片段；本机配置=`data/config-gpt.json` 独立维护） | 用户反馈参数名误导；默认部署命令不再含该参数 |
| P7 | disposition 可取消（用户反馈误触无回头路）：前端再点已选 chip = 清除；后端 null/null 且非 final 放行为清除 | 新增 `test_disposition_cancel_and_reset`；**Ran 31 tests … OK (skipped=1)**；MySQL opt-in ×2 OK（本机测试容器，首轮失败为上个会话遗留脏数据撞 fail-closed，干净重跑稳定过）；镜像重建推 v1 |
| P7 | 用户定规：**私有仓库 push 有流量费，只允许一次性推基础镜像，应用镜像一律服务端构建** | `python:3.12-slim`（amd64，`56fd2ca9…`）已推入 `…/fangzuzu/`（`docker tag` 直推曾退回单平台旧内容，改 buildx `FROM python:3.12-slim --platform linux/amd64 --push` 确保 amd64）；Dockerfile 改 `ARG BASE_IMAGE`（默认 VPC 端点），compose 改 `build: .`；本机冒烟（挂临时 sqlite config）API `[]` 通过；更新流程=`git pull && docker compose up -d --build`，不再 `compose pull`；labeler:v1 遗留仓库（无害，可 ACR 控制台删） |
| P7 | 选择页各 head 显示剩余量（用户需求：换 head 回选择页要知道还剩多少没填） | `renderStart` head 按钮加"剩 N / M"副文本；`refreshStartData` 并行拉 `/api/batches` + 7 个 head 的 `/api/assignments`（离线回退 `cachedList`），返回选择页/切 batch 时刷新，进入标注屏后不回刷；浏览器实测：标注前 stance 剩 20/20 → 标 1 条 final → ⇄ 返回剩 19/20、batch 行"完成 3/140"同步刷新 |
| P7 | 批次完成时刻（用户需求：填完了要有自动触发的信号）：① 标完 head 末条或进入已完成 batch → 查 `/api/batches`，done==total 弹"本批次已全部填完"sheet（每 batch 每会话一次；触发点在 flushQueue 成功路径，避免与 POST 竞态；离线补传场景同样触发）② batch 行"已完成 ✓ N / N"③ `gpt_tasks.py status --batch X` 输出 total/done/finals/complete（批次不存在 exit 1） | 32 tests OK（含新增 `test_status_reports_completion`）；浏览器实测：mini 批 2 条标完 → sheet 弹出 → 返回选择页见"已完成 ✓ 2 / 2"且 head 剩 0/2 → 重进该批次 sheet 再弹；真实库 status：demo {140,3,2,false} |
| git | 每 phase commit | `131d5ee` phase1, `10ae9f1` phase2, `f34770e` phase3, `71003a6` phase4, `7ad7518` phase5, `00d61c3` docs, `7bc6978` phase6, `4071b78` deploy.sh, `d7f778f` subpath, `2978808` compose, `0e62024` setup_deploy, `7ac08b8` external-mysql, `b05f29c` docs, `ea7a117` gpt-host optional, `4791793` disposition-cancel, `b92df4b` server-side-build, `2c9d795` head-counts |

## 3. 文件清单

```
server.py                     唯一后端入口（API、静态服务；存储委托 storage.py，--config）
storage.py                    存储层：load_config（fail-closed 校验）+ SqliteStore/MysqlStore + create_store
config.example.json           配置模板（提交）；config.json 为真实凭据（gitignore）
static/index.html             单页骨架（开始屏 / 主标注屏 / sheet / toast / 横幅）
static/style.css              100dvh 四段布局、bottom sheet、按钮 ≥44px
static/app.js                 会话、标注流、保存链路（队列+重试）、resume、sheet
static/manifest.json          PWA（可添加到主屏幕）
static/icon-192.png           stdlib（zlib/struct）生成的占位 PNG
static/icon-512.png           同上
schema/annotation-schema.v1.json  owner 可编辑释义：head 问题句/定义/该选不该选/正例/易混淆 + 标签中文名/释义 + 不变量
tools/import_batch.py         导入 samples.jsonl（fail-closed；委托 SqliteStore，保留 --db 旧用法）
tools/export_annotations.py   导出 jsonl/csv（--final-only；委托 SqliteStore，保留 --db 旧用法）
tools/gpt_tasks.py            GPT 专用：add 加任务 / pull 拉结果（--config，sqlite/mysql 通用）
tools/setup_deploy.py         服务器部署一次性设置：生成 config.json（指向自备 MySQL，--mysql-password 必填、chmod 600、拒绝覆盖、过 load_config 校验），打印建库 SQL 与本地 GPT 端配置
deploy/labeler.service        systemd 单元模板（服务器开机自启/崩溃重启；无 Docker 场景）
Dockerfile                    ARG BASE_IMAGE 默认私有仓库 python:3.12-slim（amd64，已一次性推入，本机验证用 --build-arg 覆盖公网端点）+ pymysql，PYTHONUNBUFFERED=1；服务端构建应用层
requirements.txt              仅 pymysql>=1.1（镜像构建用）
.dockerignore                 构建上下文排除 .git/data/config.json/.env/tests/文档
docker-compose.yml            仅 labeler 服务：build: .（服务端构建，不推应用镜像）、回环 8787 给 nginx、extra_hosts host-gateway（容器连宿主机 MySQL）、挂 config.json/data；MySQL 由使用方自备实例
tools/seed_demo.py            20 条虚构文本 demo
tests/test_server.py          stdlib unittest ×15（HTTP/存储行为，后端无关）
tests/test_config_and_tools.py  配置校验 ×9 + gpt_tasks 子进程往返 ×4 + MySQL opt-in ×2
tests/test_subpath.py        子路径挂载 ×2（prefix 剥离 API/静态 + 前端无根绝对路径守卫）
README.md                     使用说明 + 手动测试清单
data/labeler.db               运行时创建（gitignore）
data/annotations.jsonl        每次保存 append 一行（gitignore）
```

## 4. 数据契约

### 导入 samples.jsonl（`--batch`、`--heads` 必填）

| 字段 | 必填 | 说明 |
|---|---|---|
| `sample_id` | ✓ | 非空字符串；batch 内唯一，与库中撞库即 fail-closed（EXIT=1 不写库） |
| `text` | ✓ | 非空字符串，落 `samples.content` |
| `title` |  | 字符串或 null |
| `metadata` |  | 对象，**原样落库**（`json.dumps(..., sort_keys=True)`）；未知顶层字段 fail-closed |

每个 sample × 每个 head 生成一条 assignment：`assignment_id = {batch_id}:{sample_id}:{head}`，
`position` 按 head 内文件顺序 0 起递增（追加导入续接 max+1）。

### API

- `GET /api/resume` → `{batch_id, assignment_id}`；未完成 = 无标注 或（`is_final=0` 且
  disposition 不属于终态 {跳过, 无法判断, 缺少上下文}）；排序：最近活动优先，从未触碰按 position。
- `GET /api/batches` → 含 `total` / `done`（done = is_final=1 或终态 disposition）。
- `GET /api/assignments?batch_id=&head=` → 按 position 升序，含 `answer/disposition/is_final` 快照。
- `GET /api/assignment?id=` → assignment + sample（content/metadata）+ head 释义（glossary）+
  全局不变量（invariants）+ disposition 词表。
- `POST /api/annotations` → `{assignment_id, answer, disposition, is_final}`；answer 为
  字符串（单选）或字符串数组（reasoning_tags，去重保序）；按 assignment_id 幂等 upsert，
  `revision` 每次 +1，并 append 一行到 `data/annotations.jsonl`。标签不在 schema 词表 → 400。
- disposition 存储中文枚举：`无法判断 / 缺少上下文 / 跳过 / 稍后再看`。
- **取消**：`answer=null, disposition=null, is_final=false` = 清除标注（P7 起），revision+1、
  status 回 in_progress、resume 重新可见；`is_final=true` 时空标注仍 400。前端 = 再点一次
  已选中的 disposition chip（保留已有 answer 与 final 态）。

### 导出（stdout 重定向即文件）

- JSONL 行：`{batch, sample_id, head, answer, disposition, is_final, schema_version,
  updated_at, metadata}`；默认含未标注行（answer=null, updated_at=null）；`--final-only` 只输出 is_final=1。
- CSV：同字段 9 列；数组 answer 与 metadata 序列化为 JSON 字符串；`is_final` 为 1/0。
- `data/annotations.jsonl`：服务端每次保存的完整流水（含 revision），与 DB 最终态互补。

## 5. provenance 字段说明

- `metadata.split_provenance` 由导入方提供，原样落库、原样导出，本工具**不解析、不修改、
  不参与任何切分逻辑**；用于下游（ModelTraining 侧 split 追溯）。
- `schema_version`：batch 建立时从 `schema/annotation-schema.v1.json` 的
  `schema_version`（= `semantic-schema-calibrated-v0.2.1`）写入 batches 表并随每条导出携带；
  标签词表校验以该文件 `head_order`/`labels` 为唯一来源。
- `source`、`date`、`stock_code`、`stock_name` 均为透传元数据，仅 UI"更多信息"展示。
- demo 数据（seed_demo）全部为虚构文本，`split_provenance=demo/seed_v1`，source 标注"虚构股吧（demo）"。

## 6. 不变量（已写入 annotation-schema.v1.json，UI sheet 可见）

`UNKNOWN≠NEUTRAL`、`NONE_EXPLICIT≠CALM`、`NO_ACTION_SIGNAL≠WATCH`、`看空≠恐惧`、
`愿望/条件动作/他人动作≠作者已执行动作`、`"不割肉"不是 SELL、"不追高"不是 BUY`、弃权不是普通标签。

## 7. 已知限制

1. **无鉴权/无 HTTPS**：设计为信任局域网单人使用，勿暴露公网。
2. **完全离线冷启动不可用**：固定文件结构无 Service Worker，服务不可达时刷新无法加载页面
   壳；页面保持打开时离线点击不丢（队列 + 自动重试）。
3. 重试队列同一 assignment 只保留最新完整快照（中间态不逐条回放；服务端幂等 upsert 语义一致）。
4. `/api/resume` 跨 batch/head 时以服务端最近活动为准，会覆盖本地会话选择（跨设备续标语义）。
5. CSV 中数组/对象为 JSON 字符串，Excel 内不便直接筛选。
6. sqlite `journal_mode=WAL`，并发写依赖进程内锁；假设单写者（单人标注）。
7. HEAD 释义编辑（annotation-schema.v1.json）需重启 server.py 生效；已导入 assignment 的
   head/标签词表不会自动迁移（换 schema 版本需显式新建 batch）。
8. 本机 localStorage 队列详情缓存上限：详情 200 条（超出淘汰最早），列表按 batch+head 一份。
9. mysql 模式：单连接 + 进程内锁串行（与 sqlite 同一并发假设：单人标注）；长连接经
   `ping(reconnect=True)` 自愈；多进程同时写依赖 MySQL 事务，无跨进程优先级仲裁。
10. `config.json` 相对路径按**配置文件所在目录**解析；复制配置到其他机器时需保持目录结构
    （或改绝对路径）。凭据明文存于 config.json——已 gitignore，但不得随仓库/截图外传。
11. compose 版仅 labeler 服务（镜像经私有仓库分发，服务器不 build）：MySQL 由使用方自备实例，
    `config.json` 指向。若 MySQL 只监听 127.0.0.1，容器经 `host.docker.internal`（host-gateway）
    连不上——需 MySQL 监听内网地址，或 config 填可达内网 IP。本机 GPT（gpt_tasks）直连 MySQL 对外端口。

## 8. 开放集成问题（待 owner 拍板）

1. **batch 样本来源**：当前靠 import_batch 手工导入。是否对接 MyResearcher-DataClean 的
   canonical 输出（按 `sample_id` 严格对齐）作为唯一来源？导入端是否需要增加对 canonical
   字段（如 published_at）的校验？
2. **Gold 晋级协议**：本工具产出的是 reviewed/human 标注（`is_final=1`），不自动成为 Gold。
   晋级需在 ModelTraining 侧定义（如双标一致、owner 抽检、disposition 排除规则）。导出是否
   需要附加 `label_confidence`（schema 中已有 audit-only 词表 HIGH/MEDIUM/LOW）字段？
3. **assignment 生成策略**：当前为 sample × head 全积（20 样本 ×7 head=140 条）。大库下是否
   改为按 head 分批、或按难度/分歧采样子集？position 目前等于文件顺序，是否需要随机化以
   避免顺序效应？
4. 标注语义依赖 `board_context`（目标股票）——当前由 metadata.stock_code/name 隐式承载，
   UI 未在正文中注入 board_context；是否需要将 board_context 与正文同卡片展示？
