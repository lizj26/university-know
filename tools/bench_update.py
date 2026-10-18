#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测量「更新一次网页」要多久 —— 并复现云端矩阵的并行方式。

云端（GitHub Actions）的更新分两个阶段：
  1. fetch 阶段：matrix 里每个校区一个独立 runner，**并行**跑 fetch_campus.py
     → 总耗时 ≈ 最慢的那个校区，不是各校之和
  2. build 阶段：所有校区抓完后，用 refresh.py --build-only 重建一次 index.html
     → 单进程，条目越多越慢

本脚本在本地复现这两个阶段：
  * --parallel N 控制同时跑几个校区（本地资源有限，用 4~8；云端是几十个 runner）
  * 每个校区的子进程输出会被解析出「抓取用时 / 请求次数 / 条目数」
  * 最后再跑一次 refresh.py --build-only，量出构建耗时

用法
----
    python tools/bench_update.py                      # 全部校区，并行 6，全量抓取
    python tools/bench_update.py --parallel 8
    python tools/bench_update.py --no-import          # 不写 data/（纯测耗时）
    python tools/bench_update.py --extra "--no-details"
    python tools/bench_update.py --campus zju,sjtu
    python tools/bench_update.py --skip-fetch         # 只测 build 阶段
"""
import argparse
import concurrent.futures as cf
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)


def load_sources_keys():
    spec = importlib.util.spec_from_file_location("fc", os.path.join(ROOT, "fetch_campus.py"))
    fc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fc)
    return list(fc.SOURCES), fc


TIME_RE = re.compile(r"⏱ \[(\w+)\] 抓取用时 ([\d.]+)s，请求 (\d+) 次")
TOTAL_RE = re.compile(r"· \[(\w+)\] 合计 (\d+) 条")


def run_campus(campus, extra, env_py):
    cmd = [env_py, "fetch_campus.py", "--campus", campus, "--no-build"] + extra
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    wall = time.time() - t0
    out = proc.stdout or ""
    m = TIME_RE.search(out)
    inner = float(m.group(2)) if m else wall
    reqs = int(m.group(3)) if m else 0
    mt = TOTAL_RE.search(out)
    items = int(mt.group(2)) if mt else 0
    return {"campus": campus, "wall": wall, "inner": inner, "reqs": reqs,
            "items": items, "rc": proc.returncode, "out": out}


def main():
    ap = argparse.ArgumentParser(description="测量一轮更新的耗时")
    ap.add_argument("--campus", default="", help="逗号分隔；默认全部已配置校区")
    ap.add_argument("--parallel", type=int, default=6, help="同时跑几个校区（云端矩阵相当于很多）")
    ap.add_argument("--extra", default="", help="透传给 fetch_campus.py 的额外参数")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--skip-fetch", action="store_true", help="只测 build")
    ap.add_argument("--skip-build", action="store_true", help="只测 fetch")
    ap.add_argument("--json", default="", help="把结果写到指定 json 文件")
    args = ap.parse_args()

    keys, fc = load_sources_keys()
    campuses = [c.strip() for c in args.campus.split(",") if c.strip()] or keys
    extra = args.extra.split() if args.extra else []

    result = {"campuses": campuses, "parallel": args.parallel,
              "extra": extra, "rows": [], "build_s": None,
              "wall_fetch_s": None, "serial_fetch_s": None}

    if not args.skip_fetch:
        print(f"· 抓取阶段：{len(campuses)} 个校区，并行 {args.parallel}"
              f"{'，额外参数 ' + ' '.join(extra) if extra else ''}\n")
        t0 = time.time()
        rows = []
        with cf.ThreadPoolExecutor(max_workers=args.parallel) as pool:
            futs = {pool.submit(run_campus, c, extra, args.python): c for c in campuses}
            for i, fut in enumerate(cf.as_completed(futs), 1):
                row = fut.result()
                rows.append(row)
                flag = "OK " if row["rc"] == 0 else f"rc{row['rc']}"
                print(f"  [{i:2d}/{len(campuses)}] {row['campus']:6} {flag} "
                      f"用时 {row['wall']:6.1f}s  请求 {row['reqs']:5d}  条目 {row['items']:4d}")
        wall = time.time() - t0
        rows.sort(key=lambda r: -r["wall"])
        result["rows"] = [{k: v for k, v in r.items() if k != "out"} for r in rows]
        result["wall_fetch_s"] = wall
        result["serial_fetch_s"] = sum(r["wall"] for r in rows)
        print(f"\n  抓取阶段墙钟 {wall:.1f}s；若串行需 {result['serial_fetch_s']:.1f}s"
              f"（并行加速 {result['serial_fetch_s'] / max(wall, 0.01):.1f}×）")
        print(f"  最慢校区 {rows[0]['campus']} {rows[0]['wall']:.1f}s"
              f"（云端矩阵的墙钟下限就是这个量级）")
        print(f"  请求合计 {sum(r['reqs'] for r in rows)} 次")

    if not args.skip_build:
        print("\n· 构建阶段：refresh.py --build-only")
        t0 = time.time()
        proc = subprocess.run([args.python, "refresh.py", "--build-only"], cwd=ROOT,
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        build = time.time() - t0
        result["build_s"] = build
        tail = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip()][-3:]
        print("\n".join("  " + ln for ln in tail))
        print(f"  构建耗时 {build:.1f}s")
        size = os.path.getsize(os.path.join(ROOT, "index.html")) / 1048576
        result["index_mb"] = round(size, 2)
        print(f"  index.html {size:.2f} MB")

    total = (result["wall_fetch_s"] or 0) + (result["build_s"] or 0)
    print(f"\n══ 一轮更新总墙钟（本地并行 {args.parallel}）≈ {total:.1f}s "
          f"= {total / 60:.1f} 分钟 ══")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=1)
        print(f"结果已写入 {args.json}")


if __name__ == "__main__":
    main()
