#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查 _work/items-*.json 的质量,决定是否入库。"""
import json
import os
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORK = os.path.join(ROOT, "_work")

for campus in ("zju", "sjtu", "xjtu"):
    path = os.path.join(WORK, f"items-{campus}.json")
    data = json.load(open(path, encoding="utf-8"))
    items = data["items"]
    print("=" * 96)
    print("%s  共 %d 条" % (campus, len(items)))
    print("  来源分组 :", dict(Counter((it.get("dt") or {}).get("sg") for it in items)))
    print("  来源数   :", len({it["s"] for it in items}))
    print("  分类分布 :", dict(Counter(it["c"] for it in items).most_common()))
    print("  intent   :", dict(Counter(it["g"] for it in items)))
    print("  有正文   : %d/%d   有活动时间: %d   有地点: %d   有 ev: %d" % (
        sum(1 for it in items if (it.get("dt") or {}).get("tx")),
        len(items),
        sum(1 for it in items if (it.get("dt") or {}).get("tm")),
        sum(1 for it in items if (it.get("dt") or {}).get("lo")),
        sum(1 for it in items if (it.get("dt") or {}).get("ev")),
    ))
    print("  无日期   :", sum(1 for it in items if not it.get("d")))
    print("  重复标题 :", [t for t, n in Counter(it["t"] for it in items).items() if n > 1][:3])
    print("  重复链接 :", [u for u, n in Counter(it["i"] for it in items).items() if n > 1][:3])
    dates = sorted(it["d"][:10] for it in items if it.get("d"))
    print("  日期范围 :", (dates[0] if dates else "-"), "→", (dates[-1] if dates else "-"))
    print("  来源明细 :")
    for name, n in Counter(it["s"] for it in items).most_common():
        group = {(it.get("dt") or {}).get("sg") for it in items if it["s"] == name}
        print("     %-34s %3d  %s" % (name[:34], n, "/".join(sorted(g for g in group if g))))
    print("  样例:")
    for it in items[:5]:
        dt = it.get("dt") or {}
        print("     [%s] %s | %s | %s" % (it["c"], (it["d"] or "无日期")[:16], it["s"][:16], it["t"][:40]))
        if dt.get("ev"):
            print("           ev=%s" % dt["ev"])
        if dt.get("tm"):
            print("           tm=%s" % dt["tm"])
