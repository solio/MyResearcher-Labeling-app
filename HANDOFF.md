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
| git | 每 phase commit | `131d5ee` phase1, `10ae9f1` phase2, `f34770e` phase3, `71003a6` phase4, `7ad7518` phase5, `00d61c3` docs, phase6=本提交 |

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
deploy.sh                     部署到服务端：rsync 纯文件同步（排除凭据/数据），无镜像无构建
deploy/labeler.service        systemd 单元模板（服务器开机自启/崩溃重启）
tools/seed_demo.py            20 条虚构文本 demo
tests/test_server.py          stdlib unittest ×15（HTTP/存储行为，后端无关）
tests/test_config_and_tools.py  配置校验 ×9 + gpt_tasks 子进程往返 ×4 + MySQL opt-in ×2
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
