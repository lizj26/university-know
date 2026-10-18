#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""就地清洗 _work/items-*.json 里的标题(去掉「_交大要闻」这类栏目尾巴)。

配合 fetch_campus.py 里修好的 pick_title 使用;只改标题,不动其它字段。
用法: python build/_check/fix_titles.py [--apply]
"""
import importlib.util
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
WORK = os.path.join(ROOT, "_work")
APPLY = "--apply" in sys.argv

spec = importlib.util.spec_from_file_location("fc", os.path.join(ROOT, "fetch_campus.py"))
fc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fc)

changed_total = 0
for campus in ("zju", "sjtu", "xjtu"):
    path = os.path.join(WORK, f"items-{campus}.json")
    if not os.path.exists(path):
        continue
    data = json.load(open(path, encoding="utf-8"))
    changed = []
    for item in data["items"]:
        old = item["t"]
        new = fc.pick_title(old, "")
        if new and new != old and len(new) >= 6 and len(new) < len(old):
            item["t"] = new
            changed.append((old, new))
    print("%s: %d/%d 条标题被清洗" % (campus, len(changed), len(data["items"])))
    for old, new in changed[:6]:
        print("   %s  →  %s" % (old[:44], new[:44]))
    changed_total += len(changed)
    if APPLY and changed:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)

print("\n合计清洗 %d 条%s" % (changed_total, "(已写回)" if APPLY else "(仅预览,加 --apply 写回)"))
