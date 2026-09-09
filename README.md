# MyResearcher-Labeling-app

单人手机标注工具。默认后端零依赖（Python 标准库 `http.server` + `sqlite3`），可选 MySQL 存储；前端零构建零依赖（原生 JS + PWA manifest），`git` 管理按 phase 提交。

标签体系（6 个单标签 head + 15 标签多选 `reasoning_tags`）抄录自
`MyResearcher-ModelTraining/schema/semantic-schema-calibrated-v0.2.1.json`（只读，class_order 未改动）。
中文释义、问题句、正例、易混淆说明在 `schema/annotation-schema.v1.json`，owner 可直接编辑，服务重启后生效。

> 完整交付文档见 **[使用说明与接口说明.md](使用说明与接口说明.md)**（界面操作、HTTP API、CLI、数据契约、localStorage 约定）。

## 存储模式与配置文件

存储后端由配置文件决定（默认读取项目根 `config.json`，所有 CLI 可用 `--config` 指定其他路径）。
配置含数据库凭据，**已 gitignore**；模板见 `config.example.json`，复制改名即可：

```json
{
  "storage": "sqlite",                    // 或 "mysql"
  "jsonl_path": "data/annotations.jsonl", // 审计流水，两种模式都会写
  "sqlite": { "db_path": "data/labeler.db" },
  "mysql": { "host": "...", "port": 3306, "user": "...", "password": "...", "database": "myresearcher_labeler" }
}
```

- 无 `config.json` 时默认 sqlite（历史用法完全不变）。
- 相对路径按**配置文件所在目录**解析；`server.py`、`tools/gpt_tasks.py` 等读同一份配置，把 `config.json` 复制到哪台机器就能对哪个库操作。
- 校验 fail-closed：未知字段、缺字段直接报错退出 2，不静默降级。
- mysql 模式需要驱动：`pip install pymysql`（纯 Python，仅此一个依赖；sqlite 模式仍然零安装）。MySQL 端 `batch_id ≤64 字符、sample_id ≤255 字符`，超限导入会整批回滚。

## 启动

```bash
cd MyResearcher-Labeling-app
python3 tools/seed_demo.py        # 可选：生成 20 条虚构文本的 demo batch
python3 server.py --port 8787     # 绑定 0.0.0.0，自动打印局域网 URL
python3 server.py --config config.json --port 8787   # mysql 模式同理
```

sqlite 模式下数据写 `data/labeler.db` 与 `data/annotations.jsonl`（append 流水）；
mysql 模式下表建在配置指定的库中（utf8mb4），jsonl 流水仍写本地。

## GPT 加任务 / 拉结果（gpt_tasks）

面向"本机 GPT 加任务 → 服务端网页标注 → 本机拉结果"的闭环。两份配置指向**同一个 MySQL**：
服务端 `config.json` 用内网 host，本机 `data/config-gpt.json` 用公网 host（已配置好、已实测通）。

```bash
# 加任务（本机执行；与 import_batch 同一套 samples.jsonl 契约，fail-closed）
python3 tools/gpt_tasks.py add --batch mybatch --file samples.jsonl \
  --heads target_mode,stance --config data/config-gpt.json

# 稀疏指定：每行的 heads 独立决定该 sample 生成哪些任务
python3 tools/gpt_tasks.py add --batch sparse --assignment-file assignments.jsonl --config data/config-gpt.json
# assignments.jsonl 示例：{"sample_id":"a-001","text":"正文","heads":["stance"]}

# 查完成度（判断批次是否填完；total=任务数 done=final或终态 finals=is_final=1）
python3 tools/gpt_tasks.py status --batch mybatch --config data/config-gpt.json

# 拉结果（jsonl 默认打印 stdout；--out 写文件；csv 同 export 列）
python3 tools/gpt_tasks.py pull --config data/config-gpt.json
python3 tools/gpt_tasks.py pull --final-only --batch mybatch --format csv --out gold.csv --config data/config-gpt.json
```

退出码：0 成功；1 数据/校验失败（不写库）；2 配置错误。

## 导入自己的数据

`samples.jsonl` 每行一个对象：

```json
{"sample_id":"a-001","text":"正文必填","title":"可选",
 "metadata":{"date":"2026-08-01","stock_code":"DM0001","stock_name":"虚构甲","source":"来源","split_provenance":"dev/xxx_v1"}}
```

- 必填 `sample_id`、`text`；可选 `title`、`metadata`（原样落库）。
- 未知顶层字段、重复 `sample_id`、缺必填字段 → fail-closed 退出码 1，不写库。

```bash
python3 tools/import_batch.py --batch mybatch --file samples.jsonl \
  --heads target_mode,stance,emotion_primary,emotion_target,action_tendency,context_dependency,reasoning_tags
```

## 手机访问（同一 Wi-Fi）

1. 电脑启动 `python3 server.py --port 8787`，记下控制台打印的 `局域网: http://192.168.x.x:8787`。
2. 手机连同一 Wi-Fi，浏览器打开该地址。
3. **添加到主屏幕**：
   - iOS Safari：分享 → 添加到主屏幕；
   - Android Chrome：菜单 → 添加到主屏幕 / 安装应用。
4. 标注时全程使用会话锁定的 head；断网点击会进本机队列，网络恢复自动同步（页面需保持打开）。

## 部署到服务端

> 当前状态（2026-09-08）：已上线 `https://testapi.zuzurent.com.cn/labeler/`（nginx 子路径
> 反代实测通）；更新方式=服务端构建，见下。

### 方案 A：docker compose + 已有 MySQL（当前实际部署，推荐）

环境：阿里云服务器，公网 `8.148.251.114`、内网 `172.21.153.219`。MySQL 用服务端
**已有实例**的 `fzz-config` 库与同名账号——无需建库建号，labeler 首次启动自动建
4 张表（batches/samples/assignments/annotations）。

镜像策略：**应用镜像在服务端构建，不推私有仓库**（push 有流量费用）。基础镜像
`python:3.12-slim`（amd64）已一次性推入私有仓库，服务器构建时经 VPC 端点拉取一次后
常驻本地缓存；此后每次构建只重建应用层，秒级、零仓库流量。

服务器上首次部署（照抄，`<MySQL密码>` 换成 fzz-config 的真实密码）：

```bash
# ① 一次性：登录私有镜像仓库（构建时拉基础镜像用）
docker login --username='xiazhidao@1726161629823217' \
  fangzuzu-docker-registry-vpc.cn-guangzhou.cr.aliyuncs.com

# ② clone + 一键生成 config.json（storage=mysql；已存在时拒绝覆盖，先删旧的）
git clone <你的仓库地址> MyResearcher-Labeling-app
cd MyResearcher-Labeling-app
python3 tools/setup_deploy.py \
  --mysql-host 172.21.153.219 --mysql-user fzz-config --mysql-db fzz-config \
  --mysql-password '<MySQL密码>'

# ③ 构建并启动（首次会拉基础镜像，之后秒级；只绑 127.0.0.1:8787 交给 nginx，见下节）
docker compose up -d --build
curl http://127.0.0.1:8787/api/batches   # 应能看到 "selftest-gpt-link"（链路自检批次）
```

- **服务端 `mysql.host` 必须填内网 `172.21.153.219`**（容器到宿主机内网 IP 可路由），
  不要用默认的 `host.docker.internal`——该 MySQL 不一定监听 docker 网关地址。
- 本机侧（Mac 上跑 `gpt_tasks.py` 加任务/拉结果）的配置就是 `data/config-gpt.json`
  （gitignored），已指向公网 `8.148.251.114:3306` 并实测双向通，与服务器部署无关，
  用法见「GPT 加任务 / 拉结果」。
- 通用口径（换其他 MySQL 时）：`--mysql-host` 按容器可达地址填；MySQL 只监听
  127.0.0.1 时容器连不上，需改监听地址或填可达内网 IP。
- 审计流水落在宿主机 `./data/annotations.jsonl`；标注数据在 MySQL 里。
- **更新代码**（改了代码之后）：本机推 GitHub → 服务器
  `git pull && docker compose up -d --build`。基础镜像已常驻缓存，构建秒级；
  **不要推应用镜像到私有仓库**（push 有流量费，基础镜像只在变更 Python 版本时才重新推）。

### 方案 B：无 Docker（python3 直接跑）

git clone 后**默认零配置可用**（无 config.json 即 sqlite，零依赖）：

```bash
git clone <你的仓库地址> MyResearcher-Labeling-app
cd MyResearcher-Labeling-app
python3 server.py --port 8787
```

要 mysql 模式则自备 MySQL 并手写 `config.json`（模板 `config.example.json`，
`pip3 install --user pymysql`）；开机自启用 `deploy/labeler.service`（systemd 模板，
改 WorkingDirectory/User/端口）。

## 挂到已有 nginx（子路径）

前端资源与 API 请求已**全部相对路径化**，挂任意子路径零改动，nginx 只加两个 location：

```nginx
location = /labeler { return 301 /labeler/; }   # 无斜杠入口重定向（必须）
location /labeler/ {
    proxy_pass http://127.0.0.1:8787/;          # 末尾 / 会剥掉 /labeler 前缀
    proxy_set_header Host $host;
}
```

- 直接访问 `https://你的域名/labeler/` 即可；前端无绝对路径，http/https、有无反代都无感。
- PWA manifest/图标同为相对路径，添加主屏幕在子路径下照常可用。
- 本地直连 `http://192.168.x.x:8787/`（手机局域网场景）不受影响，同一份代码两种挂法通用。

## 导出

```bash
python3 tools/export_annotations.py --format jsonl > out.jsonl
python3 tools/export_annotations.py --format jsonl --final-only > gold.jsonl
python3 tools/export_annotations.py --format csv > out.csv
```

导出含 `batch, sample_id, head, answer, disposition, is_final, schema_version, updated_at, metadata`；
默认包含未标注行（`answer=null`），`--final-only` 只输出 `is_final=1`。

## 测试

```bash
python3 -m unittest discover tests
```

## 手动测试清单（真机验收）

- [ ] 360×800 竖屏无横向滚动；长文本在卡片内滚动，页面四段不动
- [ ] 底部标签区独立滚动，按钮高度 ≥44px；单选/多选（推理依据）行为正确
- [ ] head 旁"?"、标签旁"ⓘ"、"更多信息"三个 bottom sheet 打开/关闭，不跳页不丢已选
- [ ] 多选每次 toggle 即存草稿；"完成本条"置 final 并跳下一条未完成
- [ ] disposition：跳过/无法判断/缺少上下文 → 跳下一条；稍后再看 → 留在本条；**再点一次已选中的 → 取消**（被终态跳走的条目用"上一条"返回后同样可取消）
- [ ] 刷新页面 → 恢复原 batch/head/sample 与已填答案
- [ ] 断网（关电脑服务）点击 →"本机已保存·待同步 N 条"；恢复服务 → 约 5–10s 自动同步为"已入库"
- [ ] 换 head（⇄）→ 返回选择页，重进后进度保留；选择页各 head 显示"剩 N / M"且随标注刷新
- [ ] 导出 jsonl/csv 与界面数据一致，`split_provenance` 原样保留

## 已知限制

- 无鉴权，信任局域网环境；请勿暴露公网。
- 服务不可达时整页冷启动不可用（固定文件结构无 Service Worker）；页面保持打开时离线点击不丢。
- 本机重试队列对同一 assignment 只保留最新一次点击的完整快照。
