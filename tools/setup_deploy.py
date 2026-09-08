#!/usr/bin/env python3
"""服务器部署一次性设置：生成 config.json（指向已有 MySQL 实例）。

git clone 后在服务器上跑一次，随后 docker compose up -d 即可：

    python3 tools/setup_deploy.py --mysql-password '已有MySQL的密码' \
        [--mysql-host host.docker.internal] [--mysql-port 3306] \
        [--mysql-user labeler] [--mysql-db myresearcher_labeler] [--gpt-host 服务器公网IP]

- MySQL 由使用方自备（宿主机实例/容器/其他主机均可），需先建库建号，命令见运行时提示。
- mysql.host 填「labeler 容器能到达」的地址：宿主机实例用默认 host.docker.internal
  （compose 已配 host-gateway）；容器或其他主机填内网 IP。
- config.json 已 gitignore；已存在时拒绝覆盖（EXIT=2）。结束时打印本地 GPT 机器用的配置。
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser(description="生成 config.json（指向已有 MySQL）")
    ap.add_argument("--mysql-host", default="host.docker.internal")
    ap.add_argument("--mysql-port", type=int, default=3306)
    ap.add_argument("--mysql-user", default="labeler")
    ap.add_argument("--mysql-password", required=True)
    ap.add_argument("--mysql-db", default="myresearcher_labeler")
    ap.add_argument("--gpt-host", default="<服务器IP>",
                    help="打印给本地 GPT 配置用的 host（填服务器公网 IP）")
    args = ap.parse_args()

    cfg_path = ROOT / "config.json"
    if cfg_path.exists():
        print("[setup] config.json 已存在，不覆盖；确认要重新生成请先删除它", file=sys.stderr)
        return 2

    cfg = {
        "storage": "mysql",
        "jsonl_path": "data/annotations.jsonl",
        "mysql": {"host": args.mysql_host, "port": args.mysql_port,
                  "user": args.mysql_user, "password": args.mysql_password,
                  "database": args.mysql_db},
    }
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(cfg_path, 0o600)

    sys.path.insert(0, str(ROOT))
    from storage import load_config
    load_config(str(cfg_path))

    print(f"[setup] 已生成 config.json（{args.mysql_host}:{args.mysql_port}/{args.mysql_db}，通过 load_config 校验）")
    print("[setup] 下一步：docker compose up -d")
    print("[setup] 前提（在已有 MySQL 上执行一次）：")
    print(f"    CREATE DATABASE IF NOT EXISTS {args.mysql_db} DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
    print(f"    CREATE USER IF NOT EXISTS '{args.mysql_user}'@'%' IDENTIFIED BY '<密码>';")
    print(f"    GRANT ALL PRIVILEGES ON {args.mysql_db}.* TO '{args.mysql_user}'@'%';")
    print("[setup] 本地 GPT 机器的配置（存为 config-gpt.json；安全组放行该 MySQL 端口）：")
    print(json.dumps({
        "storage": "mysql",
        "mysql": {"host": args.gpt_host, "port": args.mysql_port,
                  "user": args.mysql_user, "password": args.mysql_password,
                  "database": args.mysql_db},
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
