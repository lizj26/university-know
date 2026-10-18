#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
「XX知道」校园信息聚合站 —— 一条命令完成「抓取 + 生成单文件站点」。

数据来源：各高校官网公开通知（由 fetch_campus.py 抓取），或 pkuknow.cn 公开接口
（仅当某校在 CAMPUS_PREFIX 里登记了上游前缀）。内容版权归原作者所有。

用法示例
--------
    python refresh.py --build-only           # 不联网，用 data/*.json 重新生成 index.html
    python refresh.py --limit 300            # 上游模式下只抓最近 300 条，快很多
    python refresh.py --campus zju           # 指定校区(暂无上游接口的会跳过)
    python refresh.py --no-details           # 只抓列表，不带正文详情(体积小 10 倍)
    python refresh.py --allow-partial        # 抓取不完整时也写入(默认保留旧数据)
    python refresh.py --campus zju --import items.json   # 导入自建信息源(按 id 合并)
    python refresh.py --campus zju --import items.json --replace   # 整校区替换

校区
----
    浙大(zju) / 上交(sjtu) / 西交(xjtu) 的内容由 fetch_campus.py 直接抓各校官网写入
    data/<campus>.json；本文件负责把它们合进 index.html。清华知道(thu) 已改为外链，
    本站不再抓取它的内容。上游(pkuknow.cn)接口能力保留：若某校以后恢复上游接口，
    在 CAMPUS_PREFIX 里登记路径即可用 --campus 直接抓。

产物
----
    data/<campus>.json    抓取到的原始数据(可复用于其它前端)
    index.html            双击即可打开的单文件站点
"""

import argparse
import concurrent.futures as cf
import glob
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
import http.cookiejar
from datetime import datetime, timedelta, timezone

# 统一用北京时间：不能用 time.strftime / datetime.now() —— 它们取运行机器的本地时区，
# GitHub Actions 运行器是 UTC，会把 UTC 时间贴上 +08:00 标签（页面显示差 8 小时）。
CST = timezone(timedelta(hours=8))          # 中国无夏令时，固定 +08:00


def now_iso_cst():
    """当前北京时间，形如 2026-10-11T12:34:56+08:00。"""
    return datetime.now(CST).isoformat(timespec="seconds")


BASE = "https://pkuknow.cn"
PAGE_SIZE = 30            # 上游服务端硬上限
RETENTION_DAYS = 90       # 保留期:发布超过 90 天的条目自动清除(0 表示永久保留)
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
TEMPLATE = os.path.join(HERE, "template.html")
OUTPUT = os.path.join(HERE, "index.html")
# 打开 index.html 时默认落在哪个校区（顶栏第一项 thu 是外链，故默认给西交）
DEFAULT_CAMPUS = "xjtu"

# 校区 -> 上游路径前缀;None 表示不经上游(改由 fetch_campus.py 直接抓官网，或该校区
# 为外链不抓取)。当前四校都为 None：三校由 fetch_campus.py 抓官网，清华是外链。
CAMPUS_PREFIX = {
    "thu": None,
    "zju": None,
    "sjtu": None,
    "xjtu": None,
}
CAMPUS_LABEL = {
    "thu": "清华知道",
    "zju": "浙大知道",
    "sjtu": "上交知道",
    "xjtu": "西交知道",
}
CAMPUS_EN = {
    "thu": "THU Know",
    "zju": "ZJU Know",
    "sjtu": "SJTU Know",
    "xjtu": "XJTU Know",
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

CONTENT_EXCERPT = 520     # 正文摘录上限(字符)
SUMMARY_LIMIT = 240       # AI 摘要上限(字符)

IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
HEAD_RE = re.compile(r"^#{1,6}\s*", re.M)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
# 上游 pkuknow.cn 现在要求「先拿会话 cookie 再读数据」:
#   第一次请求返回 403 {"code":"READ_SESSION_REQUIRED","scope":"missing_session"}
#   同时 Set-Cookie: __Host-pku_read_guest=<uuid>.<ts>.<hmac>
#   带上这个 cookie 重发同一请求才会返回 200。
# 所以这里挂一个 cookiejar,并在会话失效时自动重新握手。
_COOKIE_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_COOKIE_JAR))


def _do_get(url, timeout=30):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/json"})
    with _OPENER.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def prime_session():
    """先访问站点首页,把 __Host-pku_read_guest 会话 cookie 装进 cookiejar。

    冷启动时直接打 API 会 403;只要先命中一次任意页面,后续请求就都带上 cookie 了。
    """
    try:
        _do_get(BASE + "/thu", timeout=20)
    except Exception:  # noqa: BLE001   握手失败不致命,真正的请求还会再试
        pass


def get_json(path, retries=4):
    url = path if path.startswith("http") else BASE + path
    last = None
    for attempt in range(retries):
        try:
            text = _do_get(url)
            # 会话失效时上游返回 200/403 但 body 是 READ_SESSION_REQUIRED
            if "READ_SESSION_REQUIRED" in text[:400]:
                raise RuntimeError("会话已过期")
            return json.loads(text)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries - 1:      # 最后一次失败后不再空等
                prime_session()            # 重新握手,换一个新 cookie 再试
                time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"请求失败 {url}: {last}")


def api(campus, path):
    prefix = CAMPUS_PREFIX[campus]
    if prefix is None:
        raise RuntimeError(f"{CAMPUS_LABEL.get(campus, campus)} 暂无上游接口")
    return f"{prefix}{path}"


def fetch_list(campus, page, extra="&intent=all"):
    return get_json(api(campus, f"/api/notices?page={page}&page_size={PAGE_SIZE}{extra}"))


def fetch_detail(campus, item_id):
    """取单条详情，压缩成前端够用的精简结构。失败返回 None。"""
    try:
        raw = get_json(api(campus, f"/api/notices/{item_id}"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(raw, dict):        # 上游偶尔返回 [] 之类的非对象
        return None

    en = raw.get("english") or {}
    body = raw.get("content_markdown") or raw.get("content") or ""
    names = []
    for att in (raw.get("attachments") or []):
        if isinstance(att, dict):
            names.append(att.get("name") or att.get("title") or "")
        elif isinstance(att, str):
            names.append(att)
    names = [n for n in names if n][:8]

    return {
        "su": _clean(raw.get("ai_summary") or en.get("ai_summary"), SUMMARY_LIMIT),
        "tx": _clean_md(body, CONTENT_EXCERPT),
        "tm": _clean(raw.get("time_text"), 160),
        "lo": _clean(raw.get("location"), 160),
        "og": _clean(raw.get("organizer"), 100),
        "dl": _clean(raw.get("deadline_text"), 140),
        "ev": [raw.get("event_start"), raw.get("event_end")],
        "at": names,
        "sg": raw.get("source_group"),
    }


# --------------------------------------------------------------------------
# 文本清洗
# --------------------------------------------------------------------------
def _clean(text, limit):
    if not text:
        return None
    text = " ".join(str(text).split())
    return (text[:limit].rstrip() + "…") if len(text) > limit else text


def _clean_md(text, limit):
    """把 markdown 正文压成纯文本摘录。"""
    if not text:
        return None
    text = IMG_RE.sub(" ", str(text))
    text = LINK_RE.sub(r"\1", text)
    text = HEAD_RE.sub("", text)
    text = text.replace("**", "").replace("`", "")
    text = " ".join(text.split())
    return ((text[:limit].rstrip() + "…") if len(text) > limit else text) or None


def slim_item(it):
    en = it.get("english") or {}
    return {
        "i": it.get("id"),
        "t": it.get("title") or "",
        "s": it.get("source_name") or "",
        "si": it.get("source_id") or "",
        "d": it.get("published_at") or "",
        "u": it.get("url") or "",
        "c": it.get("category") or "",
        "g": it.get("intent_group") or "information",
        "en": en.get("title") or "",
        "ens": en.get("source_name") or "",
    }


# --------------------------------------------------------------------------
# 写入 / 统计工具
# --------------------------------------------------------------------------
def force_utf8_stdout():
    """Windows 控制台默认 gbk，直接打印 ✓/✗ 会抛 UnicodeEncodeError 让进程以退出码 1 结束。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def write_text(path, text):
    """原子写入：先写临时文件再替换；替换不被允许时退回直接写。"""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)


def backup_file(path):
    if os.path.exists(path):
        try:
            shutil.copy2(path, path + ".bak")
        except OSError as exc:
            print(f"  ! 备份 {path} 失败：{exc}", file=sys.stderr)


def load_previous_items(path):
    """把上一次的 data/<campus>.json 读成 {id: item}，用于详情抓取失败时的兜底。"""
    try:
        with open(path, encoding="utf-8") as fh:
            items = (json.load(fh) or {}).get("items") or []
    except Exception:  # noqa: BLE001
        return {}
    return {it.get("i"): it for it in items if isinstance(it, dict) and it.get("i")}


def pub_ts(item):
    """条目的发布时间(Unix 秒)。先看 d,再退回 dt.ev[0],与前端 _ts 的取法一致;
    两个都解析不了返回 None —— 时间未知的条目不参与保留期清除。"""
    candidates = [item.get("d")]
    dt = item.get("dt")
    if isinstance(dt, dict):
        ev = dt.get("ev")
        if isinstance(ev, list) and ev:
            candidates.append(ev[0])
    for value in candidates:
        if not value or not isinstance(value, str):
            continue
        text = value.strip()
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
            try:
                return time.mktime(time.strptime(text[:19], fmt))
            except ValueError:
                continue
    return None


def prune_old_items(items, days=None):
    """保留期过滤:发布时间距今超过 days 天的条目删除,时间未知的不清除。
    返回 (保留的条目, 清除的条数)。"""
    if days is None:
        days = RETENTION_DAYS
    if not days or days <= 0:
        return items, 0
    cutoff = time.time() - days * 86400
    kept, removed = [], 0
    for it in items:
        ts = pub_ts(it)
        if ts is not None and ts < cutoff:
            removed += 1
            continue
        kept.append(it)
    return kept, removed


def recompute_stats(items, declared_sources=None, registry=None):
    """来源/分组/分类统计一律按本次抓到的 items 现算。

    上游只在第 1 页给出 sources/groups 等汇总，它们描述的是全站而不是本次抓到的
    子集，直接用会让页面上的「N 个信息来源」「官网 · M 条」与正文数据对不上。

    registry 是抓取器的来源注册表(key -> 状态/链接/最近新增)，会合并进 sources 条目，
    这样页面能显示「最近更新 / 无新内容 / 采集失败 / 待接入」这类状态徽标。

    来源按 **key**(=条目的 si:官网是栏目 id,公众号是 wx:账号名)聚合,不按显示名 ——
    「浙江大学图书馆」这种官网栏目和同名公众号是两个来源,不能混成一个。
    """
    declared = {}
    for src in (declared_sources or []):
        if isinstance(src, dict) and (src.get("key") or src.get("name")):
            declared[src.get("key") or src["name"]] = src

    counts, name_of, group_of = {}, {}, {}
    for it in items:
        key = it.get("si") or ("src:" + (it.get("s") or ""))
        counts[key] = counts.get(key, 0) + 1
        name_of.setdefault(key, it.get("s") or key)
        gid = (it.get("dt") or {}).get("sg")
        if not gid:      # 与前端 _group 的推断保持一致
            gid = "wechat" if str(it.get("si") or "").startswith("weixin") else "official"
        group_of.setdefault(key, gid)
    for key, src in declared.items():
        if key in counts and src.get("source_group"):
            group_of[key] = src["source_group"]
        if src.get("name"):
            name_of.setdefault(key, src["name"])

    extra = {}
    for key, meta in (registry or {}).items():
        if not isinstance(meta, dict):
            continue
        name_of.setdefault(key, meta.get("name") or key)
        if meta.get("group"):
            group_of[key] = meta["group"]
        extra[key] = {k: meta[k] for k in ("status", "last_checked", "last_new", "first_seen",
                                           "url", "source_id", "kind", "new")
                      if meta.get(k) not in (None, "")}

    sources = []
    for key in set(counts) | set(extra):
        entry = {"key": key, "name": name_of.get(key) or key,
                 "source_group": group_of.get(key, "other"), "count": counts.get(key, 0)}
        entry.update(extra.get(key, {}))
        sources.append(entry)
    sources.sort(key=lambda s: (-s["count"], s["name"]))

    groups = []
    for gid in ("wechat", "official", "other"):
        rows = [s for s in sources if s["source_group"] == gid and s["count"] > 0]
        if rows:
            groups.append({"id": gid, "sources": len(rows), "count": sum(r["count"] for r in rows)})

    cats, intents = {}, {}
    for it in items:
        cats[it.get("c") or ""] = cats.get(it.get("c") or "", 0) + 1
        intents[it.get("g") or ""] = intents.get(it.get("g") or "", 0) + 1

    return {
        "sources": sources,
        "groups": groups,
        "category_totals": [{"name": k, "count": v}
                            for k, v in sorted(cats.items(), key=lambda kv: -kv[1])],
        "intent_totals": [{"intent_group": k, "count": v}
                          for k, v in sorted(intents.items(), key=lambda kv: -kv[1])],
    }


# --------------------------------------------------------------------------
# 导入自建信息源
# --------------------------------------------------------------------------
ITEM_TEXT_FIELDS = ("t", "s", "si", "d", "u", "c", "g", "en", "ens")
DEFAULT_CATEGORY = "校园动态"
DEFAULT_INTENT = "information"


def normalize_item(raw):
    """把外部导入的一条通知补全成页面认识的紧凑结构(缺字段给默认值)。"""
    if not isinstance(raw, dict):
        raise RuntimeError("items 里的每一项都必须是对象")
    key = raw.get("i") or raw.get("id")
    if not key:
        raise RuntimeError("每条通知都必须有 i(id) 字段")
    item = {"i": str(key)}
    for name in ITEM_TEXT_FIELDS:
        value = raw.get(name)
        item[name] = value if isinstance(value, str) else ("" if value is None else str(value))
    if not item["c"]:
        item["c"] = DEFAULT_CATEGORY
    if not item["g"]:
        item["g"] = DEFAULT_INTENT
    if isinstance(raw.get("dt"), dict):
        item["dt"] = raw["dt"]
    return item


def import_items(campus, path, replace=False):
    """把一份 JSON 里的通知并入(或整校区替换) data/<campus>.json。

    导入文件的两种写法都认:直接是通知数组,或 {"items": [...]} 对象。
    合并时按 i(id) 覆盖同名条目,原有条目保留;--replace 则整校区换成这份。
    """
    if campus not in CAMPUS_LABEL:
        raise RuntimeError(f"未知校区 {campus}，可选：{', '.join(CAMPUS_LABEL)}")
    if not os.path.exists(path):
        raise RuntimeError(f"找不到导入文件 {path}")
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    incoming = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(incoming, list):
        raise RuntimeError("导入文件要么是通知数组，要么是含 items 数组的对象")

    items, defaulted = [], 0
    for entry in incoming:
        item = normalize_item(entry)
        if not (isinstance(entry, dict) and entry.get("c")):
            defaulted += 1
        items.append(item)

    data_path = os.path.join(DATA_DIR, f"{campus}.json")
    old = {}
    if os.path.exists(data_path):
        try:
            with open(data_path, encoding="utf-8") as fh:
                old = json.load(fh) or {}
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"现有 {data_path} 读不出来：{exc}")

    merged = {} if replace else {it["i"]: it for it in (old.get("items") or []) if it.get("i")}
    added = updated = 0
    for item in items:
        if item["i"] in merged:
            updated += 1
        else:
            added += 1
        merged[item["i"]] = item
    final = sorted(merged.values(), key=lambda x: (x.get("d") or "", x.get("i") or ""), reverse=True)
    final, pruned = prune_old_items(final)
    if pruned:
        print(f"  · 清除发布超过 {RETENTION_DAYS} 天的历史条目 {pruned} 条，剩余 {len(final)} 条")

    warnings = []
    if defaulted:
        warnings.append(f"{defaulted} 条没有 category，已归入「{DEFAULT_CATEGORY}」")
    warnings.append(
        f"由 {os.path.basename(path)} 导入：新增 {added} 条 / 更新 {updated} 条 / 现有 {len(final)} 条"
        + ("（整校区替换）" if replace else ""))

    now = now_iso_cst()
    accounts = raw.get("wechat_accounts") if isinstance(raw, dict) else None
    incoming = raw.get("source_registry") if isinstance(raw, dict) else None

    # 来源注册表也只增不减:上一轮的状态先兜底,再用本轮/文件里的覆盖。
    # 否则「本轮没抓到的来源」会从来源列表里消失(待接入的公众号就会丢)。
    # 来源注册表:抓取器给出的是一份**累积的完整注册表**(它自己会读上一轮),以它为准;
    # 旧注册表里没被提到的条目,只有在「仍然有内容」或「已登记待接入」时才保留,
    # 这样既能清掉历史遗留的错误条目,也不会因为导入文件只带一部分就丢掉来源。
    live_keys = {it.get("si") or ("src:" + (it.get("s") or "")) for it in final}
    if incoming:
        registry = dict(incoming)
        for key, meta in (old.get("source_registry") or {}).items():
            if key in registry or not isinstance(meta, dict):
                continue
            if key in live_keys or meta.get("status") == "pending":
                registry[key] = meta
    else:
        registry = {}
        for src in (old.get("sources") or []):
            key = src.get("key") if isinstance(src, dict) else None
            if key:
                registry[key] = {k: v for k, v in src.items()
                                 if k not in ("key", "name", "count", "new")}
        for key, meta in (old.get("source_registry") or {}).items():
            if isinstance(meta, dict):
                registry[key] = meta

    payload = {
        "campus": campus,
        "campus_name": old.get("campus_name") or CAMPUS_LABEL.get(campus, campus),
        "scraped_at": now,
        # 这些校区没有单一上游,页脚「数据同步时间」记最近一次成功抓到内容的时间:
        # 本轮抓到条目就前进;一条都没抓到(来源全挂等)沿用旧值,不假装同步过
        "site_last_sync": (now if items else (old.get("site_last_sync") or now)),
        "interval_minutes": old.get("interval_minutes"),
        "total_all": len(final),
        "fetched": len(final),
        "truncated": False,
        "import_source": os.path.basename(path),
        # 只增不减:合并时保留已有条目,这里显式记下来
        "append_only": True,
        "last_import": {"at": now, "added": added, "updated": updated, "items": len(final),
                        "source": os.path.basename(path),
                        "crawl_new": sum(int(v.get("new") or 0) for v in (incoming or {}).values()
                                         if isinstance(v, dict))},
        # 已登记但可能还没抓到内容的公众号名单,页面用它显示「待接入」
        "wechat_accounts": accounts if accounts is not None else (old.get("wechat_accounts") or []),
        "source_registry": registry,
        "fetch_warnings": warnings,
        "items": final,
    }
    payload.update(recompute_stats(final, old.get("sources") or [], registry))

    os.makedirs(DATA_DIR, exist_ok=True)
    backup_file(data_path)
    write_text(data_path, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    print(f"✓ {data_path}  ({len(final)} 条，新增 {added} / 更新 {updated}"
          f" / {os.path.getsize(data_path)/1024:.0f} KB)")
    for warning in warnings:
        print(f"  ! {warning}", file=sys.stderr)
    return payload


# --------------------------------------------------------------------------
# 抓取一个校区
# --------------------------------------------------------------------------
def scrape_campus(campus, limit, workers, with_details=True, allow_partial=False, append_only=True):
    print(f"\n=== 抓取 {CAMPUS_LABEL.get(campus, campus)} ({campus}) ===")
    first = fetch_list(campus, 1)
    total = first.get("total_all") or first.get("total") or 0
    if not isinstance(total, int) or total <= 0:
        raise RuntimeError(f"上游返回 total_all={total!r}，疑似接口异常，已放弃本次写入")
    wanted = min(limit, total) if limit else total
    pages = (wanted + PAGE_SIZE - 1) // PAGE_SIZE
    print(f"· 站内共 {total} 条，本次抓取 {wanted} 条 / {pages} 页")

    warnings = []
    raw_items = list(first.get("items") or [])
    failed_pages = []
    for page in range(2, pages + 1):
        try:
            data = fetch_list(campus, page)
        except Exception as exc:  # noqa: BLE001
            failed_pages.append(page)
            print(f"  第 {page} 页失败，跳过：{exc}", file=sys.stderr)
            continue
        got = data.get("items") or []
        raw_items.extend(got)
        if page % 5 == 0 or page == pages:
            print(f"  列表 {page}/{pages}")
        time.sleep(0.1)

    seen, items = set(), []
    for it in raw_items:
        if it.get("id") in seen:
            continue
        seen.add(it.get("id"))
        items.append(slim_item(it))
    items = items[:wanted]

    if failed_pages:
        warnings.append(f"{len(failed_pages)} 个列表页抓取失败：{failed_pages[:5]}")

    missing = wanted - len(items)
    if missing > 0:
        warnings.append(f"比目标少 {missing} 条（{missing / wanted * 100:.1f}%）")
        if missing > max(1, int(wanted * 0.02)) and not allow_partial:
            print(f"✗ 只取到 {len(items)}/{wanted} 条，缺口过大，已保留现有数据"
                  f"（确需写入请加 --allow-partial）", file=sys.stderr)
            return None

    path = os.path.join(DATA_DIR, f"{campus}.json")
    previous_items = load_previous_items(path)
    previous = len(previous_items)
    # 只增不减时,上游变少不会丢数据(合并会保留旧条目),所以只有 --prune 才拦
    if previous and len(items) < previous * 0.9 and not allow_partial and not append_only:
        print(f"✗ 本次 {len(items)} 条，明显少于现有文件里的 {previous} 条，已保留现有数据"
              f"（确需写入请加 --allow-partial）", file=sys.stderr)
        return None

    if with_details:
        print(f"· 抓取 {len(items)} 条详情（并发 {workers}）…")
        done, failed = 0, 0
        errors = []
        with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futs = {pool.submit(fetch_detail, campus, it["i"]): it for it in items}
            for fut in cf.as_completed(futs):
                it = futs[fut]
                try:
                    detail = fut.result()
                except Exception as exc:  # noqa: BLE001
                    detail = None
                    if len(errors) < 3:
                        errors.append(str(exc))
                if detail:
                    it["dt"] = detail
                else:
                    failed += 1
                done += 1
                if done % 100 == 0 or done == len(items):
                    print(f"  详情 {done}/{len(items)}" + (f"（失败 {failed}）" if failed else ""))
        if failed:
            warnings.append(f"{failed} 条详情抓取失败")
        for err in errors:
            print(f"  ! 详情异常：{err}", file=sys.stderr)

        # 详情抓取失败的条目沿用上次结果，避免一次网络抖动就把摘要清空
        reused = 0
        for it in items:
            if not it.get("dt"):
                old = previous_items.get(it["i"])
                if old and old.get("dt"):
                    it["dt"] = old["dt"]
                    reused += 1
        if reused:
            warnings.append(f"{reused} 条详情沿用上次结果")
            print(f"  · {reused} 条详情沿用上次结果")

    # 只增不减:本轮抓到的覆盖同名条目,上一轮已有、本轮上游没再给的条目继续保留;
    # 但超过保留期(默认 90 天)的历史条目在这里自动清除
    added = updated = 0
    if append_only and previous_items:
        merged = dict(previous_items)
        for it in items:
            if it["i"] in merged:
                updated += 1
            else:
                added += 1
            merged[it["i"]] = it
        items = sorted(merged.values(), key=lambda x: (x.get("d") or "", x.get("i") or ""), reverse=True)
        pruned = 0
        items, pruned = prune_old_items(items)
        if pruned:
            print(f"  · 清除发布超过 {RETENTION_DAYS} 天的历史条目 {pruned} 条")
        if added or updated:
            print(f"  · 追加合并:新增 {added} 条 / 更新 {updated} 条 / 现有 {len(items)} 条")
    else:
        added = len(items)
        items = sorted(items, key=lambda x: (x.get("d") or "", x.get("i") or ""), reverse=True)
        items, _ = prune_old_items(items)

    # 每个来源本轮新增多少 —— 页面的来源状态徽标用(按来源标识聚合)
    registry = {}
    for it in items:
        key = it.get("si") or ("src:" + (it.get("s") or ""))
        name = it.get("s") or key
        gid = (it.get("dt") or {}).get("sg")
        if not gid:      # 与 recompute_stats / 前端保持同一套推断
            gid = "wechat" if str(it.get("si") or "").startswith("weixin") else "official"
        entry = registry.setdefault(key, {"name": name, "group": gid,
                                          "status": "no_recent", "count": 0, "new": 0})
        entry["count"] += 1
        if it["i"] not in previous_items:
            entry["new"] += 1
    now_iso = now_iso_cst()
    for entry in registry.values():
        entry["status"] = "ok" if entry["new"] else "no_recent"
        entry["last_checked"] = now_iso
        if entry["new"]:
            entry["last_new"] = now_iso

    stats = recompute_stats(items, first.get("sources") or [], registry)
    payload = {
        "campus": campus,
        "campus_name": CAMPUS_LABEL.get(campus, campus),
        "scraped_at": now_iso,
        "site_last_sync": first.get("last_sync"),
        "interval_minutes": first.get("interval_minutes"),
        "total_all": total,            # 上游站点总数
        "fetched": len(items),         # 本次实际写入的条数
        "truncated": bool(limit and len(items) < total),
        "append_only": True,
        "last_import": {"at": now_iso, "added": added, "updated": updated, "items": len(items),
                        "source": "pkuknow", "crawl_new": sum(v["new"] for v in registry.values())},
        "source_registry": registry,
        "fetch_warnings": warnings,
        "groups": stats["groups"],
        "intent_totals": stats["intent_totals"],
        "category_totals": stats["category_totals"],
        "sources": stats["sources"],
        "items": items,
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    backup_file(path)
    write_text(path, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    print(f"✓ {path}  ({len(items)} 条, {os.path.getsize(path)/1024:.0f} KB)")
    for warning in warnings:
        print(f"  ! {warning}", file=sys.stderr)
    return payload


# --------------------------------------------------------------------------
# 按需加载:轻量索引 + 分片
# --------------------------------------------------------------------------
SHARD_DIR = os.path.join(DATA_DIR, "shard")

# 首屏渲染卡片必需的字段:标识、标题、来源、分类、发布时间、链接、活动起始时间。
# 其余字段（正文、摘要、地点、主办方、附件…）只有点开详情才用得上，放进 -d 分片。
ITEM_INDEX_KEYS = ("i", "t", "en", "s", "si", "c", "u", "d")
ITEM_INDEX_DT_KEYS = ("sg", "ev", "tm")


def index_item(it):
    """条目 → 首屏索引字段（丢掉的正文由 <校区>-d.json 按需补回）。

    注意：不要叫 slim_item——refresh.py 上面已经有一个同名的「外部导入 → 紧凑结构」
    转换函数，重名会把它顶掉。
    """
    out = {k: it[k] for k in ITEM_INDEX_KEYS if k in it}
    dt = it.get("dt") or {}
    keep = {k: dt[k] for k in ITEM_INDEX_DT_KEYS if dt.get(k) is not None}
    if keep:
        out["dt"] = keep
    return out


def detail_item(it):
    """条目 → 详情增量（只保留被索引裁掉、且非空的字段）。

    `g`/`ens`/`d` 三个字段索引里已经带了，这里不重复下发，省一份体积。
    """
    out = {}
    dt = it.get("dt") or {}
    keep = {k: v for k, v in dt.items() if k not in ITEM_INDEX_DT_KEYS and v not in (None, "", [], {})}
    if keep:
        out["dt"] = keep
    for k in ("ens", "g"):
        if it.get(k) not in (None, "", [], {}):
            out[k] = it[k]
    return out


def write_shards(campuses):
    """写出每校区两份分片，返回写出的文件路径列表。

    <key>.json   全量条目：切到该校区、或从 URL 直达该校区时按需加载。
    <key>-d.json 详情增量：点开某条通知时按需加载，只为真正打开的那几条取正文。
    """
    os.makedirs(SHARD_DIR, exist_ok=True)
    written = []
    for key, c in campuses.items():
        items = c.get("items") or []

        detail_map = {}
        for it in items:
            extra = detail_item(it)
            if extra and it.get("i"):
                detail_map[it["i"]] = extra
        full = {"campus": key, "campus_name": c.get("campus_name") or key, "items": items}
        det = {"campus": key, "items": detail_map}

        for name, obj in ((f"{key}.json", full), (f"{key}-d.json", det)):
            path = os.path.join(SHARD_DIR, name)
            write_text(path, json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
            written.append(path)

    # 清掉已经下线校区的残留分片，避免部署目录里留着过期数据
    live = set()
    for key in campuses:
        live.add(f"{key}.json")
        live.add(f"{key}-d.json")
    for f in glob.glob(os.path.join(SHARD_DIR, "*.json")):
        if os.path.basename(f) not in live:
            os.remove(f)
    return written


# --------------------------------------------------------------------------
# 生成 index.html
# --------------------------------------------------------------------------
def build():
    files = sorted(glob.glob(os.path.join(DATA_DIR, "*.json")))
    if not files:
        print("✗ data/ 下没有数据，请先运行 python refresh.py", file=sys.stderr)
        sys.exit(1)

    campuses = {}
    for f in files:
        key = os.path.splitext(os.path.basename(f))[0]
        with open(f, encoding="utf-8") as fh:
            campuses[key] = json.load(fh)

    # 保留期兜底:生成页面前顺手清一遍 data/*.json 里超过保留期的历史条目,
    # 让旧数据文件不需要重新抓取也能瘦身(时间未知的不清除)
    for f in files:
        key = os.path.splitext(os.path.basename(f))[0]
        c = campuses.get(key)
        if not isinstance(c, dict):
            continue
        items, pruned = prune_old_items(c.get("items") or [])
        if pruned:
            c["items"] = items
            c["fetched"] = len(items)
            c.update(recompute_stats(items, c.get("sources") or [], c.get("source_registry")))
            backup_file(f)
            write_text(f, json.dumps(c, ensure_ascii=False, separators=(",", ":")))
            print(f"· {os.path.basename(f)} 清除发布超过 {RETENTION_DAYS} 天的历史条目 "
                  f"{pruned} 条，剩余 {len(items)} 条")

    default = DEFAULT_CAMPUS if DEFAULT_CAMPUS in campuses else sorted(campuses)[0]

    # 首屏只内嵌「轻量索引」：标题 / 时间 / 来源 / 分类这些渲染卡片必需的字段。
    # 正文摘录（dt.tx）、活动地点、主办方等只在点开详情时才用到，放进 data/shard/。
    # 这样 index.html 只有原来的 ~1/4，剩下的按需取——用户不点进去就永远不下载。
    index_campuses = {k: {"items": [index_item(it) for it in (c.get("items") or [])]}
                      for k, c in campuses.items()}
    payload = {"default_campus": default, "campuses": index_campuses}

    # 内嵌进 <script type="application/json">，务必转义 < 与行分隔符
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    blob = blob.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")

    # 分片：每校区一份。campus.json 是全量（切校区时按需加载），campus-d.json 是
    # 「只有详情才有用」的字段（点开某条通知时按需加载）。
    shard_files = write_shards(campuses)

    with open(TEMPLATE, encoding="utf-8") as fh:
        html = fh.read()
    if "__NOTICE_DATA__" not in html:
        print("✗ template.html 里找不到 __NOTICE_DATA__ 占位符", file=sys.stderr)
        sys.exit(1)
    html = html.replace("__NOTICE_DATA__", blob)

    # 页脚展示「页面生成于」，让用户能一眼确认云端这轮有没有真的跑过。
    # 只替换第一次出现（<script> 赋值处）；后面 JS 里读的是 window.__BUILD_TIME__，
    # 变量名不能跟着被换掉。
    build_iso = datetime.now(CST).isoformat(timespec="seconds")
    html = html.replace("__BUILD_TIME__", build_iso, 1)

    # 标题跟随默认校区（模板里的 <title> 写成什么都可以，这里统一改写成默认校区的标题）
    label = CAMPUS_LABEL.get(default, default)
    en = CAMPUS_EN.get(default, "")
    html, n = re.subn(r"<title>.*?</title>", f"<title>「{label}」 {en}</title>", html,
                      count=1, flags=re.S)
    if not n:
        print("! template.html 里没有 <title>，标题未改写", file=sys.stderr)

    write_text(OUTPUT, html)

    print(f"✓ {OUTPUT}")
    print(f"  校区 {', '.join(campuses)} · 共 {sum(len(c.get('items', [])) for c in campuses.values())} 条"
          f" · 首屏 {os.path.getsize(OUTPUT)/1024:.0f} KB（内嵌轻量索引）")
    print(f"  按需分片 {len(shard_files)} 份 · {sum(os.path.getsize(f) for f in shard_files)/1024:.0f} KB"
          f" → {os.path.relpath(SHARD_DIR, HERE)}/")
    print("  本地直接双击 index.html 也能看：同目录有 data/shard/ 时按需加载，"
          "没有则回退到内嵌索引（详情正文可能不完整）。")


# --------------------------------------------------------------------------
def main():
    force_utf8_stdout()
    ap = argparse.ArgumentParser(description="抓取 + 生成「XX知道」单文件站点")
    ap.add_argument("--campus", default="zju,sjtu,xjtu", help="校区，逗号分隔：" + " / ".join(CAMPUS_PREFIX))
    ap.add_argument("--limit", type=int, default=0, help="每个校区最多抓取多少条，0 表示全量")
    ap.add_argument("--workers", type=int, default=8, help="详情抓取并发数")
    ap.add_argument("--build-only", action="store_true", help="不联网，只用 data/*.json 重新生成 index.html")
    ap.add_argument("--no-details", action="store_true", help="只抓列表，不抓正文详情")
    ap.add_argument("--allow-partial", action="store_true",
                    help="抓取不完整时也写入 data/*.json（默认保留旧数据）")
    ap.add_argument("--prune", action="store_true",
                    help="只保留本轮上游返回的条目（默认只增不减，历史条目继续留在库里）")
    ap.add_argument("--import", dest="import_file", metavar="FILE",
                    help="把一份 JSON 通知导入 --campus 指定的校区，然后重新生成页面")
    ap.add_argument("--replace", action="store_true",
                    help="配合 --import：整校区替换，而不是按 id 合并")
    args = ap.parse_args()

    if args.limit < 0:
        print("✗ --limit 不能为负数", file=sys.stderr)
        sys.exit(2)
    args.workers = max(1, args.workers)

    if args.build_only:
        build()
        return

    if args.import_file:
        failed = False
        for campus in [c.strip() for c in args.campus.split(",") if c.strip()]:
            try:
                import_items(campus, args.import_file, args.replace)
            except Exception as exc:  # noqa: BLE001
                print(f"✗ 导入 {campus} 失败：{exc}", file=sys.stderr)
                failed = True
        if failed:
            sys.exit(1)
        build()
        return

    # 抓取前先握手一次,拿到上游要求的会话 cookie(否则直接打 API 会 403)
    if any(CAMPUS_PREFIX.get(c.strip()) for c in args.campus.split(",")):
        prime_session()

    for campus in [c.strip() for c in args.campus.split(",") if c.strip()]:
        if campus not in CAMPUS_PREFIX:
            print(f"✗ 未知校区 {campus}，可选：{', '.join(CAMPUS_PREFIX)}", file=sys.stderr)
            continue
        if CAMPUS_PREFIX[campus] is None:
            print(f"· {CAMPUS_LABEL[campus]}（{campus}）暂无上游接口，保留 data/{campus}.json 占位页面")
            continue
        try:
            scrape_campus(campus, args.limit, args.workers, not args.no_details,
                          args.allow_partial, not args.prune)
        except Exception as exc:  # noqa: BLE001
            print(f"✗ {campus} 抓取失败：{exc}", file=sys.stderr)

    build()


if __name__ == "__main__":
    main()
