#!/usr/bin/env python3
"""生成 20 条虚构文本的 demo batch，覆盖全部七个 head。"""

import argparse
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))
from import_batch import fail, import_samples  # noqa: E402

PROJECT_ROOT = TOOLS_DIR.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "labeler.db"
SCHEMA_PATH = PROJECT_ROOT / "schema" / "annotation-schema.v1.json"

STOCKS = [
    ("DM0001", "云帆科技"),
    ("DM0002", "蓝鲸生物"),
    ("DM0003", "星河电力"),
    ("DM0004", "远山矿业"),
    ("DM0005", "青梧传媒"),
]

# 20 条虚构文本：仅用于演示标注工具，不对应任何真实股票或真实言论。
DEMO_TEXTS = [
    ("这票业绩出来我直接傻眼，营收涨三成利润反而亏更多，管理层到底在干什么", "营收涨利润反亏"),
    ("希望下周能站稳20日线，站稳我就补一点，现在的位置其实不贵", "盼站稳20日线"),
    ("今天量能温和放大，板块轮动到算力了，手里的先拿着不动", "轮动到算力先持有"),
    ("隔壁那只都翻倍了，论基本面它还不如我们这票，服了", "隔壁翻倍了自己没动"),
    ("听群里说有大资金要进来，真假不知道，先观察几天再说", "群传大资金先观望"),
    ("被套两年了，解套我就走，再也不碰这行业", "套两年解套就走"),
    ("大盘这量能别谈什么个股了，覆巢之下无完卵", "大盘弱别谈个股"),
    ("公告说扩产两倍，环评图纸都公示了，这波基本面是真好", "扩产公告真利好"),
    ("不割肉，拿三年，亏了就当支持实体经济了", "拿三年不割肉"),
    ("小作文满天飞，什么重组什么借壳，一个字都不能信", "重组小作文不信"),
    ("刚加了一成仓，再跌再加，长线看好这个赛道", "加一成仓长线看好"),
    ("PE才15倍，同行都40倍，这估值是不是明显偏低了？", "PE15倍是否低估"),
    ("T了一手，成本降了两毛，今天操作挺顺", "做T降两毛很顺"),
    ("求求别再跌了，孩子开学学费都要没了", "求别跌学费没了"),
    ("出利好第二天就高开低走，这剧本我都看了十年了", "利好高开低走剧本"),
    ("机构和游资一起往外跑，散户接盘，典型出货形态", "资金出逃出货形态"),
    ("翻了财报，现金流是真好，可股价就是不动，一边安心一边憋屈", "现金流好股价不动"),
    ("别人恐惧我贪婪，满市场恐慌的时候正好捡点便宜筹码", "恐慌时捡筹码"),
    ("这名字谐音不吉利，反正我是从来不碰", "名字谐音不吉利"),
    ("政策一出整个板块都涨停了，就它没封住板，真没用的东西", "板块涨停它没封住"),
]


def build_samples():
    samples = []
    for i, (text, title) in enumerate(DEMO_TEXTS, 1):
        code, name = STOCKS[(i - 1) % len(STOCKS)]
        samples.append(
            {
                "sample_id": f"demo-{i:03d}",
                "title": f"[虚构demo] {title}",
                "text": text,
                "metadata": {
                    "date": f"2026-08-{28 + (i - 1) % 3:02d}",
                    "stock_code": code,
                    "stock_name": name,
                    "source": "虚构股吧（demo）",
                    "split_provenance": "demo/seed_v1",
                },
            }
        )
    return samples


def main():
    ap = argparse.ArgumentParser(description="生成 20 条虚构文本的 demo batch")
    ap.add_argument("--batch", default="demo")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--heads", default="")
    args = ap.parse_args()

    import json

    glossary = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    heads = args.heads or ",".join(glossary["head_order"])
    head_list = [h.strip() for h in heads.split(",") if h.strip()]
    if not head_list:
        fail("--heads 为空且 schema head_order 不可用")
    samples = build_samples()
    stats = import_samples(args.db, args.batch, samples, head_list, glossary.get("schema_version"))
    print(
        f"[seed_demo] OK batch={args.batch} samples={stats['samples']} "
        f"heads={len(head_list)} assignments={stats['assignments']} db={args.db}"
    )


if __name__ == "__main__":
    main()
