#!/usr/bin/env python3
"""服务器部署一次性设置：生成 .env（compose 凭据）与 config.json（服务端视角）。

git clone 后在服务器上跑一次，随后 docker compose up -d --build 即可：

    python3 tools/setup_deploy.py [--db myresearcher_labeler] [--gpt-host 服务器IP]

两个文件已 gitignore；已存在时拒绝覆盖（EXIT=2），确认重新生成请先手动删除。
MySQL 密码用 secrets 随机生成，两份文件保持一致，并打印一份给本地 GPT 机器的配置。
"""
import argparse
import json
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser(description="生成 compose 部署所需配置（.env + config.json）")
    ap.add_argument("--db", default="myresearcher_labeler", help="MySQL 库名（默认 myresearcher_labeler）")
    ap.add_argument("--user", default="labeler", help="MySQL 用户名（默认 labeler）")
    ap.add_argument("--gpt-host", default="<服务器IP>",
                    help="打印给本地 GPT 配置用的 host（填服务器公网 IP）")
    args = ap.parse_args()

    env_path = ROOT / ".env"
    cfg_path = ROOT / "config.json"
    for p in (env_path, cfg_path):
        if p.exists():
            print(f"[setup] {p.name} 已存在，不覆盖；确认要重新生成请先删除它", file=sys.stderr)
            return 2

    app_pw = secrets.token_urlsafe(12)
    root_pw = secrets.token_urlsafe(12)

    env_path.write_text(
        f"MYSQL_ROOT_PASSWORD={root_pw}\n"
        f"MYSQL_DATABASE={args.db}\n"
        f"MYSQL_USER={args.user}\n"
        f"MYSQL_PASSWORD={app_pw}\n",
        encoding="utf-8",
    )
    os.chmod(env_path, 0o600)

    cfg_path.write_text(
        json.dumps({
            "storage": "mysql",
            "jsonl_path": "data/annotations.jsonl",
            "mysql": {"host": "mysql", "port": 3306, "user": args.user,
                      "password": app_pw, "database": args.db},
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(cfg_path, 0o600)

    print(f"[setup] 已生成 {env_path.name}（compose 凭据，随机密码）与 {cfg_path.name}（服务端视角 host=mysql）")
    print("[setup] 下一步：docker compose up -d --build")
    print("[setup] 本地 GPT 机器的配置（存为 config-gpt.json，host 改成服务器公网 IP；"
          "安全组放行 13306）：")
    print(json.dumps({
        "storage": "mysql",
        "mysql": {"host": args.gpt_host, "port": 13306, "user": args.user,
                  "password": app_pw, "database": args.db},
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
