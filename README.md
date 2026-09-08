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

面向"本地专家（GPT）加任务 → 服务端网页标注 → 本地专家拉结果"的闭环，
读写与服务端同一份配置：

```bash
# 加任务（与 import_batch 同一套 samples.jsonl 契约，fail-closed）
python3 tools/gpt_tasks.py add --batch mybatch --file samples.jsonl \
  --heads target_mode,stance --config config.json

# 拉结果（jsonl 默认打印 stdout；--out 写文件；csv 同 export 列）
python3 tools/gpt_tasks.py pull --config config.json
python3 tools/gpt_tasks.py pull --final-only --batch mybatch --format csv --out gold.csv --config config.json
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

## 部署到服务端（无镜像、无构建）

项目是纯 Python 源码 + 原生 JS，**部署 = 拷文件**：没有编译产物、不需要 Docker 镜像，
本机 arm64 与 x86_64 服务器架构无关（PyMySQL 为纯 Python 驱动，同样无架构问题）。
服务器唯一要求：`python3 ≥3.8`（mysql 模式另装 `pip3 install --user pymysql`）。

```bash
./deploy.sh --host user@server --path /srv/myresearcher-labeler \
            --push-config --install-deps     # 首次部署；日常更新去掉这两个参数
```

脚本行为：rsync 同步代码（**排除 data/、config.json、日志**）→ 远端无 config.json 才上传
（绝不覆盖远端已有配置）→ 校验远端 python3/config.json/pymysql → 打印启动方式。
开机自启用 `deploy/labeler.service`（systemd 模板，改 WorkingDirectory/User/端口）。

配置文件两端共用同一份即可，注意 `mysql.host` 要两端各自可达：
推荐 MySQL 容器发布在服务器 `13306` 端口、两端配置都写服务器公网 IP（安全组放行 13306）；
若服务器侧只想走内网，可把该端配置的 host 改为 `127.0.0.1`，仅这一项允许不同。

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
- [ ] disposition：跳过/无法判断/缺少上下文 → 跳下一条；稍后再看 → 留在本条
- [ ] 刷新页面 → 恢复原 batch/head/sample 与已填答案
- [ ] 断网（关电脑服务）点击 →"本机已保存·待同步 N 条"；恢复服务 → 约 5–10s 自动同步为"已入库"
- [ ] 换 head（⇄）→ 返回选择页，重进后进度保留
- [ ] 导出 jsonl/csv 与界面数据一致，`split_provenance` 原样保留

## 已知限制

- 无鉴权，信任局域网环境；请勿暴露公网。
- 服务不可达时整页冷启动不可用（固定文件结构无 Service Worker）；页面保持打开时离线点击不丢。
- 本机重试队列对同一 assignment 只保留最新一次点击的完整快照。
