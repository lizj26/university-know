#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公开合集抓取 —— 命令行入口。

    python -m albums fetch --campus zju              # 抓（没有合集时自动先发现）
    python -m albums fetch --all --new-only          # 三校增量
    python -m albums discover --campus sjtu          # 只发现合集，不抓文章
    python -m albums list                            # 看已知合集与最近状态
    python -m albums check --all                     # 自检：接口通不通、字段还在不在
    python -m albums export --campus zju --out _work/albums/site-zju.json
    python -m albums add --campus zju --ref <合集URL 或 biz:album_id> --title 招生合集

退出码：0 正常；2 没有可用合集；3 全部失败（有网络/接口故障）。
"""

from __future__ import annotations

import argparse
import os
import sys

from . import client, collect, store

MAX_PAGES = client.PAGE_HARD_CAP


def _campuses(args) -> list[str]:
    cfg = store.load_config()
    known = store.campuses(cfg)
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


def _pages(args) -> int:
    if getattr(args, "all_pages", False):
        return MAX_PAGES
    return max(1, min(int(getattr(args, "pages", 3)), MAX_PAGES))


# --------------------------------------------------------------------------- #
def cmd_fetch(args) -> int:
    cfg = store.load_config()
    rc = 0
    for campus in _campuses(args):
        print(f"\n=== {campus} ===")
        payload = collect.collect(
            campus, cfg,
            pages=_pages(args),
            new_only=not args.no_incremental,
            fresh=args.fresh,
            only=args.album,
            auto_discover=not args.no_discover,
            seed_limit=args.seed_limit,
            seed_per_account=args.seed_per_account,
            workers=args.workers,
            timeout=args.timeout,
            retries=args.retries,
            pacing=0.0 if args.fast else args.pacing,
            max_age_days=args.max_age_days,
        )
        stats = payload["stats"]
        out = store.output_path(campus)
        age = f"，跳过 {stats['stale_albums']} 个过期合集" if stats.get("stale_albums") else ""
        hist = (f"，清掉历史旧条目 {stats['dropped_by_age']}"
                if stats.get("dropped_by_age") else "")
        print(f"· {stats['albums']} 个合集{age} / 累计 {stats['articles']} 篇"
              f"（本轮翻到 {stats['fetched']}，新 {stats['new_articles']}）"
              f" / {stats['accounts']} 个号{hist} → {os.path.relpath(out, store.ROOT)}")
        if not payload["items"]:
            rc = max(rc, 2)
        elif all(a["status"] == "error" for a in payload["albums"]):
            rc = max(rc, 3)
    return rc


def cmd_discover(args) -> int:
    cfg = store.load_config()
    rc = 0
    for campus in _campuses(args):
        info = collect.discover(campus, cfg, limit=args.seed_limit,
                                per_account=args.seed_per_account,
                                workers=args.workers, timeout=args.timeout)
        print(f"\n=== {campus} === 种子 {info['seeds']} 篇 → 新发现 {info['found']} 个合集")
        for rec in info["records"]:
            print(f"  + {rec['account'] or '(未知号)':<16} {rec['album_id']}  {rec['biz']}")
        for err in info["errors"][:5]:
            print(f"  ! {err}")
        if info["errors"] and not info["records"]:
            rc = max(rc, 3)
        if not info["seeds"]:
            rc = max(rc, 2)
    return rc


def _w(text: str, width: int) -> str:
    """按终端显示宽度左对齐（中文算 2 列，否则表格会错位）。"""
    text = text or ""
    used = sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)
    return text + " " * max(0, width - used)


def cmd_list(args) -> int:
    cfg = store.load_config()
    state = store.load_state()
    for campus in _campuses(args):
        albums = collect.resolve_albums(cfg, campus)
        print(f"\n=== {campus}（{len(albums)} 个合集）===")
        if not albums:
            print("  （空）先跑 `python -m albums discover`，或 `add` 手工登记")
            continue
        print(f"  {_w('合集', 22)}{_w('公众号', 16)}{_w('来源', 11)}"
              f"{'总数':>5}  {'最新文章':<12}{'状态':<9}最后抓取")
        for rec in sorted(albums, key=lambda r: (r.get("account") or "", r.get("title") or "")):
            st = store.album_state(state, f"{rec['biz']}:{rec['album_id']}")
            title = (st.get("title") or rec.get("title")
                     or rec.get("note") or f"id:{rec['album_id']}")
            status = st.get("status") or "-"
            if st.get("stale") and status != "stale":
                status = "stale"
            print(f"  {_w(title[:20], 22)}{_w((rec.get('account') or '-')[:14], 16)}"
                  f"{_w(rec.get('source') or '-', 11)}{st.get('article_count', 0):>5}  "
                  f"{_w(st.get('latest') or '-', 12)}{_w(status, 9)}"
                  f"{(st.get('last_run') or '-')[:16]}")
    return 0


def cmd_check(args) -> int:
    cfg = store.load_config()
    bad = 0
    for campus in _campuses(args):
        rows = collect.probe(campus, cfg, timeout=args.timeout)
        print(f"\n=== {campus}（{len(rows)} 个合集）===")
        if not rows:
            print("  （没有合集可检）")
            continue
        print(f"  {_w('合集', 22)}{_w('公众号', 16)}{'ret':>5}{'条':>4}{'总数':>6}  字段")
        for r in rows:
            fields = "/".join(k for k in ("has_url", "has_time", "has_key") if r[k])
            flag = "OK " if r["ok"] else "FAIL"
            print(f"  {_w((r['title'] or r['album_id'])[:20], 22)}"
                  f"{_w((r['account'] or '-')[:14], 16)}"
                  f"{r['ret']:>5}{r['rows']:>4}{r['article_count']:>6}  {flag} {fields}"
                  + (f"  ⚠ {r['error']}" if r["error"] else ""))
            if not (r["ok"] and r["has_url"] and r["has_time"] and r["has_key"]):
                bad += 1
    return 0 if bad == 0 else 3


def cmd_export(args) -> int:
    cfg = store.load_config()
    rc = 0
    for campus in _campuses(args):
        path = store.output_path(campus)
        payload = store.read_json(path, None)
        if not payload:
            print(f"✗ 没有 {os.path.relpath(path, store.ROOT)}，先跑 fetch", file=sys.stderr)
            rc = 2
            continue
        items = collect.to_site_items(payload,
                                     confident_only=not args.include_unconfirmed,
                                     retention_days=args.retention_days)
        doc = {
            "campus": campus,
            "generated_at": client.now_iso(),
            "module": "albums",
            "source_group": "wechat",
            "filtered_by_campus": not args.include_unconfirmed,
            "count": len(items),
            "items": items,
        }
        out = args.out or os.path.join(store.WORK_DIR, f"site-{campus}.json")
        if "{campus}" in out:
            out = out.replace("{campus}", campus)
        store.write_json(out, doc)
        print(f"· {campus}: 导出 {len(items)} 条 → {os.path.relpath(out, store.ROOT)}")
    return rc


def cmd_add(args) -> int:
    ref = client.parse_ref(args.ref)
    if not ref:
        print("✗ 认不出这个合集引用：需要完整合集 URL 或 biz:album_id", file=sys.stderr)
        return 2
    cfg = store.load_config()
    campus = args.campus or ""
    if not campus:
        print("✗ 必须用 --campus 指一个校区", file=sys.stderr)
        return 2
    added = store.add_album(cfg, campus, ref.biz, ref.album_id,
                            title=args.title or "", account=args.account or "",
                            note=args.note or "")
    print(f"{'✓ 已加入' if added else '· 已存在（已补空字段）'} {campus} / {ref.key}")
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m albums",
        description="公开合集（appmsgalbum）抓取：零凭据、无第三方依赖、不受公众号限流影响。")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def campus_args(p, default_help="校区，逗号分隔；不传则全部"):
        p.add_argument("--campus", help=default_help)
        p.add_argument("--all", action="store_true", help="所有已配置校区")

    def net_args(p):
        p.add_argument("--timeout", type=float, default=15, help="单请求超时秒（默认 15）")
        p.add_argument("--retries", type=int, default=2, help="失败重试次数（默认 2）")

    p = sub.add_parser("fetch", help="抓合集文章（没有合集时自动先发现）")
    campus_args(p)
    p.add_argument("--pages", type=int, default=3, help="每个合集最多翻几页（默认 3，每页约 20 篇）")
    p.add_argument("--all-pages", action="store_true", help="翻到合集末尾（首次补历史用）")
    p.add_argument("--no-incremental", action="store_true",
                   help="忽略水位，从头抓（默认是增量：翻到上次位置就停）")
    p.add_argument("--fresh", action="store_true", help="同时忽略已见集合（等于重新开始）")
    p.add_argument("--album", action="append", metavar="ID",
                   help="只抓指定合集（album_id / biz / biz:album_id，可重复）")
    p.add_argument("--no-discover", action="store_true", help="没有合集时不要自动发现")
    p.add_argument("--seed-limit", type=int, default=24, help="发现时最多抓多少篇种子文章页")
    p.add_argument("--seed-per-account", type=int, default=2,
                   help="每个号抽几篇种子（默认 2；同号文章指向同一合集，不必多抓）")
    p.add_argument("--workers", type=int, default=4, help="发现阶段的并发数")
    p.add_argument("--pacing", type=float, default=0.8, help="合集之间/翻页之间的间隔秒")
    p.add_argument("--fast", action="store_true", help="不留间隔（自测用，别长期这么跑）")
    p.add_argument("--max-age-days", type=int, default=None,
                   help="只看最近 N 天内有更新的合集（默认取 albums.json 的 60；0=不限）")
    net_args(p)
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("discover", help="从种子文章页里发现合集")
    campus_args(p)
    p.add_argument("--seed-limit", type=int, default=24, help="最多抓多少篇种子文章页")
    p.add_argument("--seed-per-account", type=int, default=2, help="每个号抽几篇")
    p.add_argument("--workers", type=int, default=4, help="并发数")
    net_args(p)
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("list", help="列出已知合集与最近一次抓取状态")
    campus_args(p)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("check", help="自检：接口可达性 + 返回字段完整性")
    campus_args(p)
    net_args(p)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("export", help="导出成站点 item 结构（供下游合并，本包不写 data/）")
    campus_args(p)
    p.add_argument("--out", help="输出路径（支持 {campus} 占位）")
    p.add_argument("--include-unconfirmed", action="store_true",
                   help="包含「公众号名不像本校」的条目（默认过滤掉，防跨校污染）")
    p.add_argument("--retention-days", type=int, default=store.RETENTION_DAYS,
                   help=f"只导出最近 N 天（默认 {store.RETENTION_DAYS}，与站点一致；0 = 不限）")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("add", help="手工登记一个合集")
    campus_args(p)
    p.add_argument("--ref", required=True, help="合集 URL 或 biz:album_id")
    p.add_argument("--title", help="合集名（不填则抓取时自动补）")
    p.add_argument("--account", help="公众号名（不填则抓取时自动补）")
    p.add_argument("--note", help="备注")
    p.set_defaults(func=cmd_add)

    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n· 已中断（已抓到的部分不会落盘）", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
