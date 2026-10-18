#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线测量「构建阶段」耗时：把所有校区的数据合进 index.html 要多久。

为什么要单独量构建
------------------
云端一轮更新分两段：抓取（matrix 并行，墙钟≈最慢校区）和构建（**单进程**，
refresh.py --build-only，把 data/*.json 全量内联进 index.html）。抓取可以靠加
机器并行，构建不行 —— 校区的数据越多、条目越多，这一段就越长，所以它才是
扩到几十所学校后首先要确认的那一段。

做法
----
在一个临时目录里复制 refresh.py + template.html，按指定规模合成 data/*.json
（已有校区的真实数据直接复制，其余校区克隆模板条目并改写 id/url/标题），
真跑一次 `refresh.py --build-only`，量出耗时与产物大小。

用法
----
    python tools/bench_build.py                    # 默认 40/80/160 条/校区三档
    python tools/bench_build.py --per-campus 120   # 只跑一档
    python tools/bench_build.py --keep             # 保留临时目录以便检查
"""
import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from campus_roster import CAMPUSES  # noqa: E402

REAL = ("zju", "sjtu", "xjtu")


def data_campuses():
    """真正会写进 index.html 的校区（排除外链校区，如清华）。"""
    return [r for r in CAMPUSES if not (len(r) > 5 and r[5])]


def synth_campus(key, zh, per_campus, tpl):
    """按模板条目合成一个校区的数据（字段结构与真实数据完全一致）。"""
    items = []
    for n in range(per_campus):
        base = dict(tpl[n % len(tpl)])
        base["i"] = f"https://{key}.example.edu.cn/news/{n:04d}.htm"
        base["u"] = base["i"]
        base["t"] = f"【{zh}】{base.get('t', '通知')[:40]} 第{n}期"
        base["d"] = base.get("d") or "2026-10-01T09:00:00+08:00"
        items.append(base)
    items.sort(key=lambda x: (x.get("d") or ""), reverse=True)
    return {
        "campus": key,
        "campus_name": zh,
        "items": items,
        "sources": [{"key": "syn", "name": f"{zh}·新闻网", "group": "official",
                     "count": per_campus, "status": "ok"}],
        "source_registry": {"syn": {"name": f"{zh}·新闻网", "group": "official",
                                    "kind": "list", "url": "https://example.edu.cn/",
                                    "status": "ok", "count": per_campus, "new": 3,
                                    "last_checked": "2026-10-11T21:00:00+08:00",
                                    "first_seen": "2026-10-01T09:00:00+08:00",
                                    "last_new": "2026-10-11T21:00:00+08:00"}},
        "fetched": per_campus,
        "groups": {"official": 1},
    }


def run_once(per_campus, keep):
    tmp = tempfile.mkdtemp(prefix="bench_build_")
    try:
        for name in ("refresh.py", "template.html"):
            shutil.copy2(os.path.join(ROOT, name), os.path.join(tmp, name))
        data_dir = os.path.join(tmp, "data")
        os.makedirs(data_dir, exist_ok=True)

        tpl_path = os.path.join(ROOT, "data", "sjtu.json")
        with open(tpl_path, encoding="utf-8") as fh:
            tpl = json.load(fh)["items"]

        total_items = 0
        for row in data_campuses():
            key, zh = row[0], row[1]
            src = os.path.join(ROOT, "data", f"{key}.json")
            dst = os.path.join(data_dir, f"{key}.json")
            if key in REAL and os.path.exists(src):
                shutil.copy2(src, dst)
                with open(src, encoding="utf-8") as fh:
                    total_items += len(json.load(fh).get("items") or [])
            else:
                payload = synth_campus(key, zh, per_campus, tpl)
                with open(dst, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False)
                total_items += per_campus

        t0 = time.time()
        proc = subprocess.run([sys.executable, "refresh.py", "--build-only"], cwd=tmp,
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        build = time.time() - t0
        index = os.path.join(tmp, "index.html")
        size = os.path.getsize(index) / 1048576 if os.path.exists(index) else 0
        tail = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip()][-2:]
        return {"per_campus": per_campus, "campuses": len(data_campuses()),
                "items": total_items, "build_s": build, "index_mb": size,
                "rc": proc.returncode, "tail": tail, "tmp": tmp if keep else None}
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="离线测量构建阶段耗时")
    ap.add_argument("--per-campus", type=int, default=0,
                    help="新增校区每校合成多少条（默认跑 40/80/160 三档）")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    args = ap.parse_args()

    levels = [args.per_campus] if args.per_campus else [40, 80, 160]
    print(f"· 校区数 {len(data_campuses())}（其中 {len(REAL)} 所用真实数据，其余按档位合成）\n")
    rows = []
    for lv in levels:
        r = run_once(lv, args.keep)
        rows.append(r)
        print(f"  每校 {lv:4d} 条 → 合计 {r['items']:5d} 条 | "
              f"构建 {r['build_s']:5.2f}s | index.html {r['index_mb']:5.2f} MB"
              + (f" | rc={r['rc']}" if r["rc"] else ""))
        for ln in r["tail"]:
            print(f"      {ln.strip()}")

    if len(rows) >= 2:
        lo, hi = rows[0], rows[-1]
        di = max(1, hi["items"] - lo["items"])
        slope = (hi["build_s"] - lo["build_s"]) / di
        print(f"\n· 规模斜率 ≈ {slope * 1000:.2f} ms/条"
              f"（{lo['items']} → {hi['items']} 条，{lo['build_s']:.2f}s → {hi['build_s']:.2f}s）")
        print(f"· 每 1000 条新增构建成本 ≈ {slope * 1000 * 1000 / 1000:.1f}s")


if __name__ == "__main__":
    main()
