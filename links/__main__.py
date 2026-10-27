#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公开链接抓取 —— 命令行入口。

    python -m links harvest --campus zju            # 汇总三个公开渠道，更新台账
    python -m links harvest --all --expand 40       # 额外扩链（慢，低价比）
    python -m links report --all --top 15           # 有多少篇、发布者都有谁
    python -m links report --campus zju --csv out.csv   # 导出到表格

退出码：0 正常；2 台账为空；3 渠道全失败。
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

from . import collect


def _w(text: str, width: int) -> str:
    """按终端显示宽度左对齐（中文算 2 列）。"""
    text = str(text or "")
    used = sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)
    return text + " " * max(0, width - used)


def _campuses(args) -> list[str]:
    cfg = collect.load_config()
    known = list((cfg.get("campuses") or {}).keys())
    albums = collect.read_json(collect.ALBUMS_CONFIG, {}) or {}
    for name in (albums.get("campuses") or {}):
        if name not in known:
            known.append(name)
    if getattr(args, "all", False) or not getattr(args, "campus", ""):
        return known
    out = []
    for name in str(args.campus).replace("，", ",").split(","):
        name = name.strip()
        if not name:
            continue
        if name not in known:
            print(f"✗ 未知校区 {name}，可选：{', '.join(known)}", file=sys.stderr)
            continue
        out.append(name)
    return out


# --------------------------------------------------------------------------- #
def cmd_harvest(args) -> int:
    rc = 0
    for campus in _campuses(args):
        print(f"\n=== {campus} ===")
        lib = collect.harvest(
            campus,
            use_sogou=not args.no_sogou,
            sogou_pages=args.sogou_pages,
            sogou_pace=args.sogou_pace,
            max_hits=args.max_hits,
            verify=args.verify,
            expand=args.expand,
            timeout=args.timeout,
            extra_queries=args.account_queries,
            probe_albums=args.probe_albums,
            seeds=not args.no_seeds,
        )
        summary = collect.summarize(lib)
        collect.save_report(campus, summary)
        print(f"  → 可入库 {summary['linkable']} 篇 / 仅盘点 {summary['metadata_only']} 篇"
              f" / 发布者 {summary['publishers']} 个"
              f" → {os.path.relpath(collect.library_path(campus), collect.ROOT)}")
        if not lib["articles"]:
            rc = max(rc, 2)
    return rc


def cmd_report(args) -> int:
    rc = 0
    grand = {"linkable": 0, "metadata_only": 0, "publishers": 0}
    for campus in _campuses(args):
        lib = collect.load_library(campus)
        if not lib.get("articles"):
            print(f"\n=== {campus} ===")
            print("  （空台账）先跑 `python -m links harvest --campus " + campus + "`")
            rc = max(rc, 2)
            continue
        s = collect.summarize(lib)
        collect.save_report(campus, s)
        print(f"\n=== {s['campus_name'] or campus}（{s['updated_at'][:16] or '-'}）===")
        print(f"  可获得文章 {s['total']} 篇：可入库 {s['linkable']}（有永久链接）"
              f" + 仅盘点 {s['metadata_only']}（只有标题/号/日期）")
        print(f"  发布者 {s['publishers']} 个，其中有过永久链接的 {s['linkable_publishers']} 个")
        src = "、".join(f"{k} {v}" for k, v in sorted(s["by_source"].items(),
                                                      key=lambda x: -x[1]))
        print(f"  来源分布：{src}")

        rows = s["rows"][:args.top] if args.top else s["rows"]
        if rows:
            print(f"  {_w('发布者', 30)}{'可入库':>6}{'仅盘点':>7}{'合计':>6}  时间跨度")
            for r in rows:
                span = f"{r['first']} ~ {r['last']}" if r["first"] else "-"
                print(f"  {_w(r['account'][:28], 30)}{r['linkable']:>6}"
                      f"{r['metadata_only']:>7}{r['total']:>6}  {span}")
            if args.top and len(s["rows"]) > args.top:
                print(f"  … 另有 {len(s['rows']) - args.top} 个号未列出（--top 0 看全部）")

        grand["linkable"] += s["linkable"]
        grand["metadata_only"] += s["metadata_only"]
        grand["publishers"] += s["publishers"]

        if args.csv:
            out = args.csv.replace("{campus}", campus)
            _write_csv(out, s["rows"])
            print(f"  · 已导出 {os.path.relpath(out, collect.ROOT)}")

    if len(_campuses(args)) > 1:
        print(f"\n合计：可获得 {grand['linkable'] + grand['metadata_only']} 篇"
              f"（可入库 {grand['linkable']} + 仅盘点 {grand['metadata_only']}），"
              f"发布者 {grand['publishers']} 个次")
    return rc


def _write_csv(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["发布者", "可入库", "仅盘点", "合计", "最早", "最晚", "合集"])
        for r in rows:
            wr.writerow([r["account"], r["linkable"], r["metadata_only"], r["total"],
                         r["first"], r["last"], " / ".join(r["albums"])])


def cmd_export(args) -> int:
    """把 A 档文章导出成站点 item 结构；加 --merge 才写进 data/。"""
    rc = 0
    for campus in _campuses(args):
        lib = collect.load_library(campus)
        if not lib.get("articles"):
            print(f"✗ {campus} 台账为空，先跑 harvest", file=sys.stderr)
            rc = max(rc, 2)
            continue
        items = collect.to_site_items(lib, confident_only=not args.include_unconfirmed,
                                      retention_days=args.retention_days)
        doc = {"campus": campus, "generated_at": collect.article.now_iso(),
               "module": "links", "source_group": "wechat",
               "retention_days": args.retention_days,
               "count": len(items), "items": items}
        out = args.out or os.path.join(collect.WORK_DIR, f"site-{campus}.json")
        if "{campus}" in out:
            out = out.replace("{campus}", campus)
        collect.write_json(out, doc)
        msg = (f"· {campus}: 导出 {len(items)} 条"
               f"（近 {args.retention_days} 天）→ {os.path.relpath(out, collect.ROOT)}")
        if args.merge:
            res = collect.merge_into_data(campus, items, retention_days=args.retention_days)
            msg += (f"\n  └ 已并入 data/{campus}.json：{res['before']} → {res['total']} 条"
                    f"（新增 {res['added']}，超期剔除 {res['dropped_by_retention']}，"
                    f"{res['accounts']} 个号）")
        print(msg)
    return rc


def cmd_show(args) -> int:
    """看台账里的明细（按号过滤），用于抽查某条到底有没有链接。"""
    lib = collect.load_library(args.campus)
    arts = list((lib.get("articles") or {}).values())
    if args.account:
        arts = [a for a in arts if args.account in (a.get("account") or "")]
    arts.sort(key=lambda a: (a.get("published") or ""), reverse=True)
    print(f"{len(arts)} 条")
    for a in arts[:args.limit]:
        mark = "链接" if a.get("url") else "无链"
        print(f"  [{mark}] {a['published'][:10] or '-'} | {(a.get('account') or '?')[:16]:<18}"
              f" | {(a.get('title') or '(标题未抓)')[:34]:<36} | "
              f"{','.join(a.get('sources') or [])}")
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m links",
        description="公开链接抓取：种子文章页 + 页内关联 + 搜狗搜索，汇总成「可获得的文章」台账。")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def campus_args(p):
        p.add_argument("--campus", help="校区，逗号分隔；不传则全部")
        p.add_argument("--all", action="store_true", help="所有校区")

    p = sub.add_parser("harvest", help="汇总渠道，更新台账")
    campus_args(p)
    p.add_argument("--no-sogou", action="store_true", help="不跑搜狗（只做合集+种子）")
    p.add_argument("--no-seeds", action="store_true",
                   help="不抓种子文章页（每页 3.5MB；云端定时任务只需要合集产物时用它）")
    p.add_argument("--sogou-pages", type=int, default=1, help="每个查询翻几页（默认 1）")
    p.add_argument("--sogou-pace", type=float, default=3.0, help="搜狗请求间隔秒（默认 3）")
    p.add_argument("--max-hits", type=int, default=120, help="每校区最多收多少条搜狗命中")
    p.add_argument("--verify", action="store_true", help="对搜狗命中逐条验证是否仍可公开读取（慢）")
    p.add_argument("--account-queries", type=int, default=8,
                   help="额外给 N 个「还没有永久链接的号」按名字搜（0=关，默认 8）")
    p.add_argument("--probe-albums", type=int, default=0, metavar="N",
                   help="打开 N 篇搜狗命中的文章页，挖合集 id（每篇 3.5MB；挖到就能转成有永久链接）")
    p.add_argument("--expand", type=int, default=0, metavar="N",
                   help="额外抓 N 个页面挖关联链接（每页 3.5MB，平均只挖出约 1 条）")
    p.add_argument("--timeout", type=float, default=25, help="单页超时秒")
    p.set_defaults(func=cmd_harvest)

    p = sub.add_parser("report", help="有多少篇、发布者都有谁")
    campus_args(p)
    p.add_argument("--top", type=int, default=15, help="每个校区列出前 N 个号（0=全部）")
    p.add_argument("--csv", help="导出 CSV（支持 {campus} 占位）")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("export", help="导出站点 item 结构；--merge 才写进 data/")
    campus_args(p)
    p.add_argument("--out", help="输出路径（支持 {campus} 占位）")
    p.add_argument("--merge", action="store_true",
                   help="同时并入 data/<校区>.json（写前自动留 .bak）")
    p.add_argument("--retention-days", type=int, default=90,
                   help="只导出最近 N 天（默认 90，与站点一致；0=不限）")
    p.add_argument("--include-unconfirmed", action="store_true",
                   help="包含「公众号名不像本校」的条目（默认过滤）")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("show", help="看台账明细")
    p.add_argument("--campus", default="zju")
    p.add_argument("--account", help="按号名过滤")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_show)

    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n· 已中断（已写入的部分台账保留）", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
