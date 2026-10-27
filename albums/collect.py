#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""编排层：发现合集 → 枚举文章 → 增量去重 → 落盘 / 导出。

三层结构，越往下越"笨"，便于测试和替换：

    __main__.py  命令行 / 人看的输出
    collect.py   编排（本文件）：分页策略、增量水位、健康状态
    client.py    HTTP + 解析（唯一联网）
    store.py     文件读写（唯一落盘）

分页与增量的两个关键事实
------------------------
1. 合集是**倒序**（新的在前）。所以增量不需要"从头翻到底"：
   翻到 msgid ≤ 上次水位的那一页就可以收工 —— 这就是 `--new-only`。
2. 续页游标是上一页最后一行的 `(msgid, itemidx)`。接口异常时会给出重复游标，
   所以必须显式检测「游标没前进」，否则会无限循环（真实踩过）。
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime
import re
import time

from . import classify, client, store
from .client import AlbumRef

MODULE_NAME = "albums"
MODULE_VERSION = "1.0.0"


# --------------------------------------------------------------------------- #
# 合集清单
# --------------------------------------------------------------------------- #
def resolve_albums(cfg: dict, campus: str) -> list[dict]:
    """人工清单 + 自动发现，按 biz:album_id 去重。

    人工清单优先：它的 campus 归属是人定的，比自动发现的推断可信；
    自动发现的记录只用来补「还没登记过的合集」，不覆盖人工填的 title/account。
    """
    merged: dict[str, dict] = {}
    for rec in store.load_discovered(campus):
        merged[f"{rec['biz']}:{rec['album_id']}"] = {
            **rec, "campus": campus, "source": "discovered"}
    for rec in store.manual_albums(cfg, campus):
        key = f"{rec['biz']}:{rec['album_id']}"
        merged[key] = {**merged.get(key, {}), **rec, "campus": campus, "source": "manual"}
    return list(merged.values())


def campus_confident(cfg: dict, campus: str, account: str, title: str) -> bool:
    """这个合集的内容是否确实属于该校区。

    三档判断，任一成立即认为可信：
      ① 公众号名在名单里（人工登记过的完整昵称）；
      ② 公众号名 / 合集名命中校区关键词；
      ③ 该校区没配任何判断依据（无从判断，就不拦）。
    不做「不认识就丢掉」——本包是采集器，判断交给 export 的 `--all` 开关。
    """
    ccfg = store.campus_config(cfg, campus)
    names = {(n or "").strip().lower() for n in (ccfg.get("accounts") or [])}
    pattern = ccfg.get("filter") or ""
    if account and account.strip().lower() in names:
        return True
    if pattern and (re.search(pattern, account or "", re.I)
                    or re.search(pattern, title or "", re.I)):
        return True
    if not names and not pattern:
        return True
    return False


# --------------------------------------------------------------------------- #
# 发现：从种子文章页里挖合集
# --------------------------------------------------------------------------- #
def discover(campus: str, cfg: dict, *, limit: int = 24, per_account: int = 2,
             workers: int = 4, timeout: float = 15, retries: int = 1,
             fetch_text=client.http_get) -> dict:
    """抓种子文章页，挖出 (biz, album_id) 并并入 discovered-<校区>.json。

    定位：把「人工找合集链接」这一步去掉。有任意一篇该号的文章链接即可。
    这也是唯一需要抓文章页的地方；枚举文章走 JSON 接口，不碰 HTML。

    **按号抽样**：种子文件按 `# 账号名` 分好组，同组文章页指向同一个合集，
    所以每号只抓 `per_account` 篇（默认 2）。不抽样的话每轮要吞 40×3.7MB。
    """
    picked = store.sample_seed_links(cfg, campus, per_account=per_account, limit=limit)
    result = {"campus": campus, "seeds": len(picked), "found": 0, "records": [],
              "errors": []}
    if not picked:
        result["errors"].append(f"没有种子链接（找 albums/seeds-{campus}.txt "
                                f"或 _work/wechat/wechat-{campus}.txt）")
        return result

    def one(item):
        label, url = item
        try:
            html = fetch_text(url, timeout=timeout, retries=retries)
        except client.AlbumError as exc:
            return label, "", [], str(exc)
        except Exception as exc:  # noqa: BLE001
            return label, "", [], f"{type(exc).__name__}: {exc}"
        return label, client.parse_account(html), client.discover_refs(html), ""

    known = store.known_album_keys(cfg, campus)
    fresh = []
    with cf.ThreadPoolExecutor(max_workers=max(1, min(workers, len(picked)))) as pool:
        for label, account, refs, error in pool.map(one, picked):
            if error:
                result["errors"].append(f"{error}")
                continue
            for ref in refs:
                if ref.key in known:
                    continue
                known.add(ref.key)
                fresh.append({"biz": ref.biz, "album_id": ref.album_id,
                              "account": account or label,
                              "source": "discovered"})

    if fresh:
        store.merge_discovered(campus, fresh)
    result["found"] = len(fresh)
    result["records"] = fresh
    return result


# --------------------------------------------------------------------------- #
# 分页
# --------------------------------------------------------------------------- #
def _max_msgid(rows) -> int:
    best = 0
    for r in rows:
        if r.msgid.isdigit():
            best = max(best, int(r.msgid))
    return best


def freshness_cutoff(max_age_days: int, today: datetime.date | None = None) -> str:
    """近 N 天的截止日期（YYYY-MM-DD）。0 表示不限，返回空串。"""
    if not max_age_days or max_age_days <= 0:
        return ""
    today = today or datetime.datetime.now(client.CST).date()
    return (today - datetime.timedelta(days=max_age_days)).isoformat()


def newest_published(rows) -> str:
    """这一页里最新的发布日期（YYYY-MM-DD）。空 = 没有可用日期。"""
    dates = [(a.published or "")[:10] for a in rows]
    dates = [d for d in dates if d]
    return max(dates) if dates else ""


def _looks_ascending(rows) -> bool:
    """这一页是不是「旧的在前」。判断不出来一律当否。"""
    dates = [(a.published or "")[:10] for a in rows if a.published]
    return len(dates) >= 2 and dates[0] < dates[-1]


def _page_fetcher(fetch):
    """把注入的 fetch 包一层，兼容不认识 `newest_first` 的假实现（测试用）。"""
    import inspect

    try:
        params = inspect.signature(fetch).parameters
        accepts = "newest_first" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        accepts = False

    def call(ref, cursor, timeout, retries, newest_first):
        if accepts:
            return fetch(ref, begin_msgid=cursor[0], begin_itemidx=cursor[1],
                         timeout=timeout, retries=retries, newest_first=newest_first)
        return fetch(ref, begin_msgid=cursor[0], begin_itemidx=cursor[1],
                     timeout=timeout, retries=retries)

    return call


def fetch_one(rec: dict, *, campus: str, cfg: dict, pages: int = 3,
              new_only: bool = False, fresh: bool = False, state: dict | None = None,
              timeout: float = 15, retries: int = 2, pacing: float = 0.8,
              max_age_days: int = 0, fetch=client.fetch_album_page) -> dict:
    """抓一个合集（自动翻页 + 增量 + 新鲜度门槛），返回合集记录（含本轮文章）。

    三件事按顺序决定"这个合集值不值得抓"：

    1. **顺序**：合集接口按各合集自己的设置返回，有的正序有的倒序
       （`base_info.is_reverse` 为 False 即正序）。正序会让增量失效——
       水位比的是一页里的最大 msgid，正序第一页永远最旧，每次都在第一页收工，
       新增文章永远看不到。所以先按默认取第一页读 `is_reverse`，
       发现是正序就带 `is_reverse=1` 重取一次（只多一个请求），之后全程按新→旧。
    2. **新鲜度**：`max_age_days > 0` 时，第一页里最新一篇早于截止日就整集作废
       （不翻页、不收文章）。判据是**合集里最新一篇文章的发布日期**，
       不是"上次抓取时间"——前者才说明这个合集还活不活着。
    3. **增量**：翻到「整页都是旧文」就收工。
    """
    ref = AlbumRef(rec["biz"], str(rec["album_id"]))
    st = {} if fresh else store.album_state(state or {}, ref.key)
    watermark = 0
    if new_only and st.get("last_msgid"):
        try:
            watermark = int(st["last_msgid"])
        except (TypeError, ValueError):
            watermark = 0
    seen = set(st.get("seen") or [])

    started = time.time()
    call = _page_fetcher(fetch)
    collected: dict[str, client.AlbumArticle] = {}
    title = (rec.get("title") or "").strip()
    account = (rec.get("account") or "").strip()
    total = 0
    pages_done = 0
    error = ""
    note = ""
    status = "ok"
    latest = ""
    fresh_unknown = False
    cursor = (0, 0)
    visited = set()
    cutoff = freshness_cutoff(max_age_days)
    desc = True

    limit = max(1, pages)
    # 第 1 页：先按默认顺序，读 is_reverse 决定要不要翻转
    page = call(ref, (0, 0), timeout, retries, False)
    pages_done += 1
    if page.ok and page.meta.is_reverse is False:
        flipped = call(ref, (0, 0), timeout, retries, True)
        if flipped.ok and flipped.rows:
            page = flipped                      # 正序合集 → 改用倒序
        else:
            desc = False                        # 翻不动就认了，后面按 fail-open 处理

    while True:
        if not page.ok:
            error = page.error
            status = "partial" if collected else "error"
            break
        if page.meta.title:
            title = title or page.meta.title
        if page.meta.account:
            account = account or page.meta.account
        if page.meta.article_count:
            total = page.meta.article_count
        rows = page.rows
        if not rows:
            break

        if pages_done == 1 and cutoff:
            latest = newest_published(rows)
            asc = _looks_ascending(rows)
            if latest and asc:
                fresh_unknown = True            # 顺序没扭过来，不能拿它判过期
            elif latest and latest < cutoff:
                stale_note = f"最新一篇 {latest}，早于 {cutoff}（{max_age_days} 天门槛）"
                note = stale_note
                status = "stale"
                collected.clear()
                break

        for art in rows:
            if art.key not in collected:
                collected[art.key] = art
        # 增量收工条件：**整页都是旧文**（本页最大 msgid ≤ 水位）。
        # 不用「按日期倒序」来推：真实数据里有些文章没有 create_time，
        # 按日期判断会误判成「顺序未知」从而多翻好几页（实测 1 页变 2 页）。
        if watermark and _max_msgid(rows) <= watermark:
            status = "ok"
            break
        if not page.continue_flag or pages_done >= limit:
            break

        last = rows[-1]
        nxt = (str(last.msgid or "0"), str(last.itemidx or "0"))
        if not last.msgid or nxt in visited:
            # 游标没前进 = 接口在原地打转，再翻就是死循环
            error = error or "分页游标没有前进，提前结束"
            status = "partial" if collected else "ok"
            break
        visited.add(nxt)
        cursor = nxt
        if pacing:
            time.sleep(pacing)
        page = call(ref, cursor, timeout, retries, desc)
        pages_done += 1

    articles = sorted(collected.values(), key=lambda a: a.published, reverse=True)
    new_count = sum(1 for a in articles if a.key not in seen)
    if not articles and status == "ok":
        status = "empty"

    items = []
    confident = campus_confident(cfg, campus, account, title)
    for art in articles:
        items.append({
            **art.as_dict(),
            "album_id": ref.album_id,
            "album_title": title,
            "account": account,
            "campus": campus,
            "campus_confident": confident,
            "is_new": art.key not in seen,
        })

    if not latest and articles:
        latest = newest_published(articles)

    return {
        "key": ref.key, "biz": ref.biz, "album_id": ref.album_id,
        "title": title, "account": account,
        "latest": latest, "stale": status == "stale",
        "fresh_unknown": fresh_unknown, "note": note, "cutoff": cutoff,
        "article_count": total, "fetched": len(articles), "new": new_count,
        "pages": pages_done, "status": status, "error": error,
        "source": rec.get("source") or "manual",
        "campus": campus,
        "campus_confident": confident,
        "last_msgid": store.max_msgid(articles),
        "elapsed_ms": int((time.time() - started) * 1000),
        "items": items,
    }


# --------------------------------------------------------------------------- #
# 一轮完整采集
# --------------------------------------------------------------------------- #
def resolve_max_age(cfg: dict, campus: str, override: int | None = None) -> int:
    """新鲜度门槛（天）。优先级：命令行 > 校区配置 > 全局配置 > 0（不限）。"""
    if override is not None:
        return max(0, int(override))
    ccfg = store.campus_config(cfg, campus)
    for value in (ccfg.get("max_age_days"), cfg.get("max_age_days")):
        if value is not None:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                continue
    return 0


def collect(campus: str, cfg: dict, *, pages: int = 3, new_only: bool = False,
            fresh: bool = False, only: list[str] | None = None,
            auto_discover: bool = True, seed_limit: int = 24, seed_per_account: int = 2,
            workers: int = 4, timeout: float = 15, retries: int = 2, pacing: float = 0.8,
            max_age_days: int | None = None,
            fetch=client.fetch_album_page, fetch_text=client.http_get) -> dict:
    """跑完一个校区的合集：先按需发现，再逐个枚举，最后更新状态并落盘。"""
    ccfg = store.campus_config(cfg, campus)
    max_age = resolve_max_age(cfg, campus, max_age_days)
    seeds_info = {}
    albums = resolve_albums(cfg, campus)

    if not albums and auto_discover:
        seeds_info = discover(campus, cfg, limit=seed_limit, per_account=seed_per_account,
                              workers=workers, timeout=timeout, fetch_text=fetch_text)
        albums = resolve_albums(cfg, campus)

    if only:
        wanted = set(only)
        albums = [a for a in albums if a["album_id"] in wanted or a["biz"] in wanted
                  or f"{a['biz']}:{a['album_id']}" in wanted]

    state = store.load_state()
    records, all_items = [], []
    for rec in albums:
        got = fetch_one(rec, campus=campus, cfg=cfg, pages=pages, new_only=new_only,
                        fresh=fresh, state=state, timeout=timeout, retries=retries,
                        pacing=pacing, max_age_days=max_age, fetch=fetch)
        album_items = got.pop("items")
        all_items.extend(album_items)
        records.append(got)
        prev = store.album_state(state, got["key"])
        store.update_album_state(
            state, got["key"],
            last_msgid=got["last_msgid"] or prev.get("last_msgid", ""),
            last_run=client.now_iso(), title=got["title"], account=got["account"],
            article_count=got["article_count"], status=got["status"],
            latest=got.get("latest", ""), stale=got.get("stale", False),
            seen=[i["key"] for i in album_items],
        )
        tail = f"  ⚠ {got['error']}" if got["error"] else (
            f"  · {got['note']}" if got.get("note") else "")
        print(f"  · {got['title'] or got['album_id']:<24} "
              f"{got['fetched']:>3} 篇（新 {got['new']:>2}） {got['status']}{tail}")

    store.save_state(state)

    # 产物是**累计**的，不是「本轮增量」：增量轮只翻 1~2 页，如果直接覆盖，
    # 下游 export 就只剩这几页的内容，等于把历史丢了。
    # 合并键用 album_id + 文章 key（不同合集的 mid 理论上不会撞，但加上更稳）。
    previous = store.read_json(store.output_path(campus), {}) or {}
    # 过期合集要从**历史**里也剔掉：只过滤本轮不写，上次抓的还会赖在文件里。
    stale_ids = {r["album_id"] for r in records if r.get("stale")}
    dropped_history = 0
    merged: dict[str, dict] = {}
    for art in previous.get("items") or []:
        if not isinstance(art, dict) or not art.get("url"):
            continue
        if art.get("album_id") in stale_ids:
            dropped_history += 1
            continue
        merged[f"{art.get('album_id')}:{art.get('key')}"] = {**art, "is_new": False}
    for art in all_items:
        merged[f"{art['album_id']}:{art['key']}"] = art
    items = sorted(merged.values(),
                   key=lambda a: (a.get("published") or "", a.get("key") or ""), reverse=True)

    by_status: dict[str, int] = {}
    for rec in records:
        by_status[rec["status"]] = by_status.get(rec["status"], 0) + 1
    payload = {
        "campus": campus,
        "campus_name": ccfg.get("name") or f"{campus}知道",
        "generated_at": client.now_iso(),
        "module": MODULE_NAME,
        "module_version": MODULE_VERSION,
        "mode": "incremental" if new_only else "full",
        "max_age_days": max_age,
        "cutoff": freshness_cutoff(max_age),
        "seeds": seeds_info,
        "albums": records,
        "items": items,
        "stats": {
            "albums": len(records),
            "by_status": by_status,
            "stale_albums": len(stale_ids),
            "dropped_by_age": dropped_history,            # 历史文件里被剔掉的过期文章
            "articles": len(items),                       # 累计
            "fetched": len(all_items),                    # 本轮翻到的
            "new_articles": sum(1 for i in all_items if i["is_new"]),
            "accounts": len({i["account"] for i in items if i.get("account")}),
        },
    }
    store.save_output(campus, payload)
    return payload


# --------------------------------------------------------------------------- #
# 导出成站点 item 结构（显式、可审计；本包自己不写 data/）
# --------------------------------------------------------------------------- #
def to_site_items(payload: dict, *, confident_only: bool = True,
                  retention_days: int = store.RETENTION_DAYS) -> list[dict]:
    """把合集文章映射成 data/<校区>.json 里的 item 结构。

    字段名与 fetch_campus.py / fetch_wechat.py 完全一致（i/t/s/d/u/c/g/dt），
    这样下游合并时不需要任何翻译层；`dt.sg` 仍为 `wechat`，
    另外多带一个 `dt.ab`（所属合集）便于页面溯源。
    """
    cutoff = ""
    if retention_days and retention_days > 0:
        cutoff = (datetime.datetime.now(client.CST)
                  - datetime.timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%S")
    out = []
    for art in payload.get("items") or []:
        if confident_only and not art.get("campus_confident", True):
            continue
        published = art.get("published") or ""
        if cutoff and published and published[:19] < cutoff:
            continue
        category = classify.classify(art.get("title") or "")
        out.append({
            "i": art["url"],
            "t": (art.get("title") or "")[:200],
            "s": art.get("account") or "微信公众号",
            "si": None,
            "d": published,
            "u": art["url"],
            "c": category,
            "g": classify.intent_of(category),
            "en": "",
            "ens": "",
            "dt": {"sg": "wechat", "og": art.get("account") or "",
                   "ab": art.get("album_title") or ""},
        })
    return out


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #
def probe(campus: str, cfg: dict, *, timeout: float = 15, retries: int = 1,
          fetch=client.fetch_album_page) -> list[dict]:
    """逐合集取第一页，检查「接口还通不通、字段还在不在」。"""
    rows = []
    for rec in resolve_albums(cfg, campus):
        ref = AlbumRef(rec["biz"], str(rec["album_id"]))
        page = fetch(ref, timeout=timeout, retries=retries)
        rows.append({
            "campus": campus,
            "album_id": ref.album_id,
            "title": page.meta.title or rec.get("title") or "",
            "account": page.meta.account or rec.get("account") or "",
            "ok": page.ok,
            "ret": page.ret,
            "rows": len(page.rows),
            "article_count": page.meta.article_count,
            "has_url": all(a.url for a in page.rows),
            "has_time": all(a.published for a in page.rows),
            "has_key": all(a.key and ":" in a.key for a in page.rows),
            "error": page.error,
        })
    return rows
