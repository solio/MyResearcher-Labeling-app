#!/usr/bin/env bash
# 部署到服务端：rsync over ssh 纯文件同步。无镜像、无构建、无架构问题
# （纯 Python + 原生 JS，PyMySQL 为纯 Python 驱动；服务器只需 python3 ≥3.8）。
#
# 用法：
#   ./deploy.sh --host user@server --path /srv/myresearcher-labeler
#   ./deploy.sh --host user@server --path /srv/myresearcher-labeler \
#               --push-config --install-deps          # 首次部署：传配置+装驱动
#   --dry-run   只预览将同步的文件
#   --port N    输出提示里的端口（默认 8787）
#
# 原则：代码与凭据分开同步；远端已有 config.json 时绝不覆盖（用 --push-config 显式上传）。

set -euo pipefail
cd "$(dirname "$0")"

HOST="" ; DST="" ; PUSH_CONFIG=0 ; INSTALL_DEPS=0 ; PORT=8787 ; DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --path) DST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --push-config) PUSH_CONFIG=1; shift ;;
    --install-deps) INSTALL_DEPS=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "[deploy] 未知参数: $1" >&2; exit 2 ;;
  esac
done

[ -n "$HOST" ] || { echo "[deploy] 需要 --host user@server" >&2; exit 2; }
[ -n "$DST" ] || { echo "[deploy] 需要 --path /srv/..." >&2; exit 2; }
command -v rsync >/dev/null || { echo "[deploy] 本机缺少 rsync" >&2; exit 2; }

RSYNC_FLAGS=(-av
  --exclude .git/ --exclude data/ --exclude config.json --exclude .env
  --exclude __pycache__/ --exclude '*.pyc' --exclude .DS_Store
  --exclude BLOCKED.md --exclude '*.log')
[ "$DRY_RUN" = 1 ] && RSYNC_FLAGS+=(-n)

echo "==> 1/4 同步代码到 $HOST:$DST （不含 data/、config.json、日志）"
ssh "$HOST" "mkdir -p '$DST'"
rsync "${RSYNC_FLAGS[@]}" ./ "$HOST:$DST/"

echo "==> 2/4 检查远端 config.json"
if ssh "$HOST" "test -f '$DST/config.json'"; then
  echo "    远端已有 config.json，保持不动"
else
  if [ "$PUSH_CONFIG" = 1 ]; then
    [ -f config.json ] || { echo "[deploy] 本地没有 config.json，无法 --push-config" >&2; exit 1; }
    echo "    上传本地 config.json（scp 加密通道）"
    scp -q config.json "$HOST:$DST/config.json"
  else
    echo "[deploy] 远端缺少 config.json：手工放置，或重跑加 --push-config" >&2
    echo "         这是『服务端视角』配置：compose 模式 host=mysql port=3306；" >&2
    echo "         非 compose 模式 host=127.0.0.1。本地 GPT 另用一份（host=服务器IP:13306），见 README 部署节" >&2
    exit 1
  fi
fi

echo "==> 3/4 远端环境检查"
ssh "$HOST" "command -v python3 >/dev/null || { echo '[deploy] 远端缺少 python3（需 ≥3.8）' >&2; exit 1; }"
ssh "$HOST" "python3 --version"
ssh "$HOST" "cd '$DST' && python3 -c 'import json,sys; json.load(open(sys.argv[1]))' config.json && echo 'config.json JSON 合法'"
if ssh "$HOST" "python3 -c 'import pymysql' >/dev/null 2>&1"; then
  echo "    pymysql 已安装"
else
  if [ "$INSTALL_DEPS" = 1 ]; then
    echo "    远端安装 pymysql（pip3 install --user pymysql，纯 Python 无编译）"
    ssh "$HOST" "python3 -m pip install --user --quiet pymysql"
  else
    echo "[deploy] 提醒：远端缺 pymysql；storage=mysql 时必需。"
    echo "         登录执行 pip3 install --user pymysql，或重跑加 --install-deps"
  fi
fi

echo "==> 4/4 启动（二选一）"
echo "  A) docker compose 推荐（自带 MySQL：healthcheck/自动重启/数据卷，镜像在服务器构建）："
echo "     ssh $HOST 'cd $DST && cp .env.example .env && nano .env   # 设置两个密码，MYSQL_PASSWORD 需与 config.json 一致'"
echo "     ssh $HOST 'cd $DST && docker compose up -d --build'"
echo "     （要求服务端 config.json 为 storage=mysql, host=mysql, port=3306）"
echo "  B) 无 Docker："
echo "     systemd：scp deploy/labeler.service $HOST:/tmp/ && ssh $HOST 'sudo mv /tmp/labeler.service /etc/systemd/system/'"
echo "     （改 unit 的 WorkingDirectory/User/端口）ssh $HOST 'sudo systemctl daemon-reload && sudo systemctl enable --now myresearcher-labeler'"
echo "     手动：ssh $HOST 'cd $DST && nohup python3 server.py --config config.json --port $PORT >> labeler.log 2>&1 &'"
echo "  A 访问: http://<服务器IP>:8787 （compose 版只绑本机回环，需配 nginx 反代）；本地 GPT 经 13306 连 MySQL"
echo "  B 访问: http://<服务器IP>:$PORT  （放行安全组/防火墙；mysql 记得也放行给本地 GPT 机器）"
