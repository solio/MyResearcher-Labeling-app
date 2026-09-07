# MyResearcher-Labeling-app

单人手机标注工具。后端仅 Python 标准库（`http.server` + `sqlite3`），前端零构建零依赖（原生 JS + PWA manifest），`git` 管理按 phase 提交。

标签体系（6 个单标签 head + 15 标签多选 `reasoning_tags`）抄录自
`MyResearcher-ModelTraining/schema/semantic-schema-calibrated-v0.2.1.json`（只读，class_order 未改动）。
中文释义、问题句、正例、易混淆说明在 `schema/annotation-schema.v1.json`，owner 可直接编辑，服务重启后生效。

## 启动

```bash
cd MyResearcher-Labeling-app
python3 tools/seed_demo.py        # 可选：生成 20 条虚构文本的 demo batch
python3 server.py --port 8787     # 绑定 0.0.0.0，自动打印局域网 URL
```

启动后同时写 `data/labeler.db`（SQLite）与 `data/annotations.jsonl`（append 流水）。

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
