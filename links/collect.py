#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""编排：把三条公开渠道汇总成一份「可获得的文章」台账。

渠道与它们各自能给出什么（2026-10-27 全部实测，别凭印象改）
--------------------------------------------------------------
| 渠道 | 入口 | 给得到 | 永久链接 | 成本 |
|---|---|---|---|---|
| 合集接口 | `_work/albums/*.json` | 标题/号/日期/链接 | ✅ 带 chksm | 零（已抓好） |
| 种子文章页 | `_work/wechat/wechat-<校区>.txt` | 标题/号/日期/链接 + 关联链接 | ✅ | 3.5 MB/页 |
| 搜狗搜索 | `weixin.sogou.com` | 标题/号/日期 | ❌ 只有分钟级签名链 | 1 请求/页结果 |
| 页内关联 | 上面抓到的页面 | 同类文章 | 只有带 chksm 的那部分 | 3.5 MB/页 |

所以台账分两档，这不是偷懒，是事实：

    A 档（可入库）  有永久链接，点得开、能抓正文、能进站点
    B 档（只盘点）  搜狗能证明"这篇文章存在、这个号发过"，但拿不到可长期用的链接

报告里分开列，才不会把 B 档算成"已经拿到了"。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import time

from . import article, sogou as sogou_mod
from .article import WallError

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORK_DIR = os.path.join(ROOT, "_work", "links")
CONFIG_PATH = os.path.join(HERE, "links.json")
ALBUMS_CONFIG = os.path.join(ROOT, "albums", "albums.json")

TIER_A, TIER_B = "A", "B"      # A=有永久链接  B=只有元数据


# --------------------------------------------------------------------------- #
# IO
# --------------------------------------------------------------------------- #
def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def write_json(path: str, payload) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def load_config() -> dict:
    cfg = read_json(CONFIG_PATH, {}) or {}
    cfg.setdefault("campuses", {})
    cfg.setdefault("defaults", {})
    return cfg


def campus_queries(cfg: dict, campus: str) -> list[str]:
    return list(((cfg.get("campuses") or {}).get(campus) or {}).get("queries") or [])


def account_queries(cfg: dict, campus: str, lib: dict, rules: dict,
                    limit: int = 8) -> list[str]:
    """给「还没有任何可入库文章」的号生成查询词。

    为什么按号补而不是加更多泛词：泛词（"XX大学 招生"）反复命中同一批热门号，
    边际收益很快归零；而名单里那些一次都没出现过的号，只能按名字去搜才有机会见到。
    这也正好是「发布者名录」从 3 个扩到 20+ 个的原因。
    """
    if limit <= 0:
        return []
    have = {a.get("account") for a in lib["articles"].values() if a.get("tier") == TIER_A}
    missing = [a for a in sorted(rules["accounts"]) if a not in have]
    return missing[:limit]


def campus_rules(cfg: dict, campus: str) -> dict:
    """校区的判据：账号名单 + 关键词正则。**从 albums/albums.json 单点读取**。

    账号归属只该有一份真相，重复维护必然会漂。这里只读，不写。
    """
    albums_cfg = read_json(ALBUMS_CONFIG, {}) or {}
    ccfg = ((albums_cfg.get("campuses") or {}).get(campus) or {})
    accounts = {str(a).strip() for a in (ccfg.get("accounts") or []) if a}
    pattern = ccfg.get("filter") or ""
    own = ((cfg.get("campuses") or {}).get(campus) or {})
    if own.get("extra_accounts"):
        accounts |= {str(a).strip() for a in own["extra_accounts"]}
    if own.get("filter"):
        pattern = own["filter"]
    # 别校特征词：上交/西交的 filter 里都有「交大」，不挡就会互相串号
    # （实测：西交二附院的文章被算进了上交）。exclude 只在**非名单精确命中**时生效，
    # 所以不会把本校名单里的号误杀。
    exclude = "|".join(own.get("exclude") or []) or ""
    return {"accounts": accounts, "pattern": pattern, "exclude": exclude,
            "name": ccfg.get("name") or f"{campus}知道"}


def relevant(account: str, title: str, rules: dict) -> bool:
    """这个号/这篇文是否属于该校区。

    三档，任一成立即算：① 号在名单里；② 号名命中关键词；③ 标题命中关键词。
    搜狗结果里混着大量无关号（本地媒体、培训机构、个人号），这一层必须严。
    """
    account = (account or "").strip()
    if account and account in rules["accounts"]:
        return True
    pat = rules.get("pattern") or ""
    if pat:
        if re.search(pat, account or "", re.I) or re.search(pat, title or "", re.I):
            return True
    return False


def relevant_publisher(account: str, rules: dict) -> bool:
    """只看**发布者**本身，不看标题。

    给搜狗用：搜狗结果里混着大量「别家媒体写了本校的事」（杭州日报、观察者网…），
    按标题放行会把它们算成本校公众号的发布者。判断"谁发的"必须只看号名。

    顺序是「名单精确命中 → 别校特征词否决 → 关键词命中」：
    名单优先是因为那份名单是人维护的，比正则可信。
    """
    account = (account or "").strip()
    if not account:
        return False
    if account in rules["accounts"]:
        return True
    exclude = rules.get("exclude") or ""
    if exclude and re.search(exclude, account, re.I):
        return False
    pat = rules.get("pattern") or ""
    return bool(pat and re.search(pat, account, re.I))


def prune_library(lib: dict, rules: dict) -> int:
    """把不符合本校判据的条目清出台账（跨校串号的清理，幂等）。

    历史遗留：早期只靠 filter，而「交大」同时命中上海交大和西安交大，
    于是西交二附院的一篇文章混进了上交台账。这条函数在每轮 harvest 开头兜一次底，
    判据与收新条目时完全一致，不会越界删。
    """
    drop = [k for k, a in lib["articles"].items()
            if not relevant_publisher(a.get("account") or "", rules)]
    for k in drop:
        lib["articles"].pop(k, None)
    return len(drop)


def norm_title(title: str) -> str:
    return re.sub(r"[\s\u3000\|｜:：,，.。!！?？\-—_【】\[\]()（）\"'“”‘’]+", "", title or "").lower()


def title_key(account: str, title: str) -> str:
    """跨渠道去重用的弱键（搜狗没有 biz/mid，只能靠「号+标题」）。

    打 16 位 sha1，避免把长中文标题塞进文件名/日志。
    """
    raw = f"{norm_title(account)}|{norm_title(title)}"
    return "t:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# 台账
# --------------------------------------------------------------------------- #
def library_path(campus: str) -> str:
    return os.path.join(WORK_DIR, f"library-{campus}.json")


def load_library(campus: str) -> dict:
    data = read_json(library_path(campus), None) or {}
    if not isinstance(data, dict) or "articles" not in data:
        data = {"campus": campus, "articles": {}, "runs": []}
    return data


def save_library(campus: str, lib: dict) -> str:
    return write_json(library_path(campus), lib)


def upsert(lib: dict, entry: dict) -> str:
    """并入台账。同一条（同 key）来自多渠道时合并 sources，不覆盖已有字段。"""
    arts = lib["articles"]
    old = arts.get(entry["key"])
    if not old:
        arts[entry["key"]] = entry
        return "new"
    for src in entry.get("sources") or []:
        if src not in (old.get("sources") or []):
            old.setdefault("sources", []).append(src)
    for field in ("url", "title", "account", "published", "biz", "mid", "idx"):
        if not old.get(field) and entry.get(field):
            old[field] = entry[field]
    old["tier"] = TIER_A if old.get("url") else old.get("tier", TIER_B)
    return "merged"


# --------------------------------------------------------------------------- #
# 渠道一：合集（已抓好，直接读）
# --------------------------------------------------------------------------- #
def harvest_albums(campus: str, lib: dict) -> dict:
    path = os.path.join(ROOT, "_work", "albums", f"albums-{campus}.json")
    data = read_json(path, None)
    if not data:
        return {"source": "album", "added": 0, "note": "没有合集产物（先跑 python -m albums fetch）"}
    added = 0
    keep_keys: set[str] = set()
    for it in data.get("items") or []:
        url = article.canonical_url(it.get("url") or "")
        ref = article.article_ref(url)
        if not ref:
            continue
        entry = {
            "key": article.ref_key(ref), "tier": TIER_A,
            "url": url, "title": it.get("title") or "",
            "account": it.get("account") or "",
            "published": it.get("published") or "",
            "album": it.get("album_title") or "",
            "sources": ["album"], **ref,
        }
        keep_keys.add(entry["key"])
        if upsert(lib, entry) == "new":
            added += 1

    # 合集侧有「只看最近 N 天内有更新的合集」的门槛（albums/albums.json 的
    # max_age_days）。过期合集在 albums 的产物里已经不存在了，但台账是累计的，
    # 上一轮抓的文章还会赖着 —— 这里按"当前产物里还有没有"把纯合集来源的条目清掉。
    dropped = prune_albums(lib, keep_keys)
    return {"source": "album", "added": added, "pruned": dropped,
            "total": len(data.get("items") or []),
            "stale_albums": (data.get("stats") or {}).get("stale_albums", 0)}


def prune_albums(lib: dict, keep_keys: set[str]) -> int:
    """删掉「来源只有合集、且当前合集产物里已经没有」的条目。

    只删纯合集来源：如果同一篇还从种子/搜狗拿到过，说明它另有出处，留着。
    """
    drop = []
    for key, art in lib["articles"].items():
        srcs = art.get("sources") or []
        if srcs and all(s == "album" for s in srcs) and key not in keep_keys:
            drop.append(key)
    for key in drop:
        lib["articles"].pop(key, None)
    return len(drop)


# --------------------------------------------------------------------------- #
# 渠道二：种子文章页（抓页面 → 补元信息 + 挖关联）
# --------------------------------------------------------------------------- #
def seed_files(campus: str) -> list[str]:
    names = [os.path.join(HERE, f"seeds-{campus}.txt"),
             os.path.join(ROOT, "_work", "wechat", f"wechat-{campus}.txt")]
    return [p for p in names if os.path.exists(p)]


def read_seeds(campus: str, limit: int = 0) -> list[str]:
    out, seen = [], set()
    for path in seed_files(campus):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line.startswith("http") or "mp.weixin.qq.com" not in line:
                        continue
                    if line in seen:
                        continue
                    seen.add(line)
                    out.append(line)
        except OSError:
            continue
    return out[:limit] if limit else out


def harvest_seeds(campus: str, lib: dict, *, timeout: float = 25,
                  on_page=None, limit: int = 0) -> dict:
    """逐条抓种子页。返回统计；抓不到的（验证墙/超时）单独计数，不当成 0。"""
    links = read_seeds(campus, limit=limit)
    added = wall = failed = 0
    related: list[dict] = []
    for url in links:
        try:
            html = article.http_get(url, timeout=timeout, retries=1)
            page = article.parse_article(html, url)
        except WallError:
            wall += 1
            continue
        except Exception:  # noqa: BLE001
            failed += 1
            continue
        if page["key"]:
            if upsert(lib, {
                "key": page["key"], "tier": TIER_A, "url": page["url"],
                "title": page["title"], "account": page["account"],
                "published": page["published"], "sources": ["seed"],
                "biz": page["biz"], "mid": page["mid"], "idx": page["idx"],
            }) == "new":
                added += 1
        related.extend(page["related"])
        if on_page:
            on_page(page)
    return {"source": "seed", "seeds": len(links), "added": added,
            "wall": wall, "failed": failed, "related": related}


# --------------------------------------------------------------------------- #
# 渠道三：搜狗（只有元数据，B 档）
# --------------------------------------------------------------------------- #
def harvest_sogou(campus: str, cfg: dict, lib: dict, rules: dict, *,
                  pages: int = 1, pace: float = 3.0, max_hits: int = 120,
                  verify: bool = False, extra_queries: int = 8,
                  probe_budget: int = 0, timeout: float = 25) -> dict:
    """按「号名/校区关键词」搜索，只收属于本校区的号。

    收录判据是**发布者**（号在名单里或号名命中关键词），不是标题。
    标题命中只用于"这个人提到了本校"——那种是别家的稿子，不该算本校公众号内容。

    `probe_budget > 0` 时会打开命中的文章页（每篇 3.5 MB），做两件事：
      ① 拿到真实身份 biz/mid/idx（比"号+标题"的弱键可靠，能和其他渠道对齐）；
      ② 挖出这篇所属的 `album_id` —— 这是把 B 档（只有元数据）转成 A 档
         （有永久链接）的唯一已知路径，因为合集接口给的链接一定带 chksm。

    ⚠ 搜狗链分钟级失效，所以**只能当场开页**：台账里旧的 B 档条目事后补不了，
      必须等下一次搜索重新拿到新签名链、按「号+标题」对回同一条时再补。
      这就是下面为什么对"已存在但没 probe 过"的条目也要接着探。
    """
    queries = campus_queries(cfg, campus) + account_queries(
        cfg, campus, lib, rules, limit=extra_queries)
    if not queries:
        return {"source": "sogou", "queries": 0, "added": 0,
                "note": "links/links.json 里没配 queries"}

    index = {}
    for key, arts in lib["articles"].items():
        index.setdefault(title_key(arts.get("account", ""), arts.get("title", "")), key)

    sess = sogou_mod.Sogou(pace=pace)
    added = probed = hits = 0
    blocked = ""
    found_albums: dict = {}
    pending: list[tuple] = []          # (旧 key 或 None, 新条目)
    detail = []
    try:
        for q in queries:
            for item in sess.search(q, pages=pages):
                if hits >= max_hits:
                    break
                if not relevant_publisher(item["account"], rules):
                    continue
                hits += 1
                tk = title_key(item["account"], item["title"])
                old_key = index.get(tk)
                old = lib["articles"].get(old_key) if old_key else None
                if old and old.get("tier") == TIER_A:      # 已有永久链接，不用管
                    continue
                if old and old.get("probed"):              # 探过了，别重复烧流量
                    continue

                entry = dict(old) if old else {
                    "key": tk, "tier": TIER_B, "url": "",
                    "title": item["title"], "account": item["account"],
                    "published": item["published"], "sources": [],
                }
                src = f"sogou:{q}"
                if src not in (entry.get("sources") or []):
                    entry.setdefault("sources", []).append(src)
                if not entry.get("published"):
                    entry["published"] = item["published"]

                if verify or probed < probe_budget:
                    try:
                        real = sess.resolve(item["sogou_link"])
                    except Exception:                      # noqa: BLE001
                        real = ""
                    if verify:
                        entry["reachable"] = bool(real)
                    if real and probed < probe_budget:
                        probed += 1
                        try:
                            html = article.http_get(real, timeout=timeout, retries=0)
                        except Exception:                  # noqa: BLE001
                            html = ""
                        entry["probed"] = True
                        if html:
                            page = article.parse_article(html, real)
                            if page["key"]:                # 真实身份，替换弱键
                                entry.update({"key": page["key"], "biz": page["biz"],
                                              "mid": page["mid"], "idx": page["idx"]})
                            if page["published"]:
                                entry["published"] = page["published"]
                            for aid in albums_in_page(html):
                                found_albums.setdefault(
                                    (page["biz"] or "", aid),
                                    {"biz": page["biz"] or "", "album_id": aid,
                                     "account": page["account"] or item["account"]})

                pending.append((old_key, entry))
                if not old:
                    added += 1
                    detail.append(f"{item['account']} · {item['title'][:24]}")
    except sogou_mod.SogouBlocked as exc:
        blocked = str(exc)

    for old_key, entry in pending:
        if old_key and lib["articles"].get(old_key) is not None:
            lib["articles"].pop(old_key, None)
        upsert(lib, entry)
        index[title_key(entry.get("account", ""), entry.get("title", ""))] = entry["key"]

    if found_albums:
        handoff_albums(campus, found_albums)
    return {"source": "sogou", "queries": len(queries), "hits": hits,
            "added": added, "probed": probed, "albums": len(found_albums),
            "blocked": blocked, "sample": detail[:5]}


# --------------------------------------------------------------------------- #
# 从文章页挖「这个号还有哪个合集」——把 B 档转成 A 档的钥匙
# --------------------------------------------------------------------------- #
_ALBUM_ID = re.compile(r"album_id=(\d{6,})")


def albums_in_page(html: str) -> list[str]:
    return sorted(set(_ALBUM_ID.findall(html or "")))


def discovered_albums_path(campus: str) -> str:
    return os.path.join(WORK_DIR, f"albums-found-{campus}.json")


def handoff_albums(campus: str, found: dict) -> str:
    """把挖到的合集写进 albums 包的发现清单（**数据交接，不是代码依赖**）。

    albums 包的 `resolve_albums()` 每次都读 `_work/albums/discovered-<校区>.json`，
    所以这里按同样的格式追加即可：下次 `python -m albums fetch` 就会去枚举它们。
    枚举结果是带 chksm 的永久链接 —— B 档就此变成 A 档，而且一个合集能带出几十篇。
    """
    path = os.path.join(ROOT, "_work", "albums", f"discovered-{campus}.json")
    old = read_json(path, []) or []
    merged = {}
    for d in old:
        if isinstance(d, dict) and d.get("biz") and d.get("album_id"):
            merged[(d["biz"], str(d["album_id"]))] = d
    for (biz, album_id), rec in found.items():
        key = (biz, str(album_id))
        merged[key] = {**merged.get(key, {}), **rec,
                       "biz": biz, "album_id": str(album_id), "campus": campus,
                       "source": merged.get(key, {}).get("source") or "links"}
    rows = sorted(merged.values(),
                  key=lambda d: (d.get("account") or "", str(d.get("album_id"))))
    write_json(discovered_albums_path(campus), rows)
    if os.path.isdir(os.path.dirname(path)):      # 目录存在才写，别替 albums 造目录
        write_json(path, rows)
    return discovered_albums_path(campus)


# --------------------------------------------------------------------------- #
# 渠道四：页内关联（可选，扩链）
# --------------------------------------------------------------------------- #
def expand_related(campus: str, lib: dict, rules: dict, *, budget: int = 0,
                   timeout: float = 25, min_chksm: bool = True) -> dict:
    """把台账里 A 档文章当成新的入口，抓页面挖「同一号的其它文章」。

    为什么要限量：每页 3.5 MB，而**平均一页只挖出约 1 条可抓的新链接**
    （其余关联链接缺 chksm，抓不动）。所以这是个低价比的补充手段，
    只在 `--expand N` 明确要求时跑，默认不跑。
    """
    if budget <= 0:
        return {"source": "related", "budget": 0, "added": 0}
    queue = [a for a in lib["articles"].values()
             if a.get("tier") == TIER_A and a.get("url")]
    fetched = added = wall = 0
    idx = 0
    while idx < len(queue) and fetched < budget:
        item = queue[idx]
        idx += 1
        try:
            html = article.http_get(item["url"], timeout=timeout, retries=0)
            page = article.parse_article(html, item["url"])
        except WallError:
            wall += 1
            continue
        except Exception:  # noqa: BLE001
            continue
        fetched += 1
        for ln in page["related"]:
            if min_chksm and not ln.get("permanent"):
                continue
            if not relevant_publisher(page["account"], rules):
                continue
            entry = {
                "key": ln["key"], "tier": TIER_A, "url": ln["url"],
                "title": "", "account": page["account"], "published": "",
                "sources": ["related"], "depth": 1,
                "biz": ln["biz"], "mid": ln["mid"], "idx": ln["idx"],
            }
            if upsert(lib, entry) == "new":
                added += 1
                queue.append(lib["articles"][ln["key"]])   # 只扩一层
        time.sleep(0.4)
    return {"source": "related", "budget": budget, "fetched": fetched,
            "wall": wall, "added": added}


# --------------------------------------------------------------------------- #
# 一轮
# --------------------------------------------------------------------------- #
def harvest(campus: str, *, use_sogou: bool = True, sogou_pages: int = 1,
            sogou_pace: float = 3.0, max_hits: int = 120, verify: bool = False,
            expand: int = 0, timeout: float = 25, quiet: bool = False,
            extra_queries: int = 8, probe_albums: int = 0) -> dict:
    cfg = load_config()
    rules = campus_rules(cfg, campus)
    lib = load_library(campus)
    steps = []

    def log(msg):
        if not quiet:
            print(msg)

    dropped = prune_library(lib, rules)
    log(f"· [{campus}] 台账已有 {len(lib['articles'])} 条"
        + (f"（清掉 {dropped} 条不属于本校的）" if dropped else ""))

    st = harvest_albums(campus, lib)
    steps.append(st)
    extra = ""
    if st.get("stale_albums"):
        extra += f"，跳过 {st['stale_albums']} 个过期合集"
    if st.get("pruned"):
        extra += f"，清掉 {st['pruned']} 条过期合集文章"
    log(f"  · 合集：+{st['added']}{extra}"
        + (f"（{st.get('note')}）" if st.get("note") else ""))

    st = harvest_seeds(campus, lib, timeout=timeout)
    steps.append(st)
    log(f"  · 种子：{st['seeds']} 条种子，+{st['added']}"
        f"（撞墙 {st['wall']}、失败 {st['failed']}）")

    if expand:
        st = expand_related(campus, lib, rules, budget=expand, timeout=timeout,
                            min_chksm=True)
        steps.append(st)
        log(f"  · 关联扩链：抓 {st['fetched']} 页，+{st['added']}（撞墙 {st['wall']}）")

    if use_sogou:
        st = harvest_sogou(campus, cfg, lib, rules, pages=sogou_pages,
                           pace=sogou_pace, max_hits=max_hits, verify=verify,
                           extra_queries=extra_queries,
                           probe_budget=probe_albums, timeout=timeout)
        steps.append(st)
        note = f"（被拦：{st['blocked']}）" if st.get("blocked") else ""
        log(f"  · 搜狗：{st.get('queries', 0)} 个查询，命中本校号 {st.get('hits', 0)} 篇，"
            f"+{st['added']}"
            + (f"，开页 {st['probed']} 篇挖到 {st['albums']} 个合集" if st.get("probed") else "")
            + note)

    lib["campus"] = campus
    lib["campus_name"] = rules["name"]
    lib["updated_at"] = article.now_iso()
    lib.setdefault("runs", []).append({
        "at": lib["updated_at"], "steps": steps,
        "total": len(lib["articles"]),
    })
    lib["runs"] = lib["runs"][-12:]
    save_library(campus, lib)
    return lib


# --------------------------------------------------------------------------- #
# 导出成站点 item 结构，并合并进 data/（显式动作，默认不做）
# --------------------------------------------------------------------------- #
DATA_DIR = os.path.join(ROOT, "data")


def to_site_items(lib: dict, *, confident_only: bool = True,
                  retention_days: int = 90) -> list[dict]:
    """把 A 档文章映射成 data/<校区>.json 的 item 结构。

    字段与 fetch_campus.py / fetch_wechat.py 完全一致（i/t/s/d/u/c/g/dt），
    下游不需要翻译层；`dt.sg` 仍是 `wechat`，另带 `dt.ab`（所属合集）便于溯源。

    ⚠ 只有**分类规则**这一张纯函数表复用了 `albums/classify.py`：
    它是 12 行正则、无副作用，复制一份必然漂移。其余没有任何跨包 import。
    """
    from albums.classify import classify, intent_of

    cutoff = ""
    if retention_days and retention_days > 0:
        cutoff = (datetime.datetime.now(article.CST)
                  - datetime.timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%S")
    out = []
    for art in (lib.get("articles") or {}).values():
        if art.get("tier") != TIER_A or not art.get("url") or not art.get("title"):
            continue
        if confident_only and art.get("campus_confident") is False:
            continue
        published = art.get("published") or ""
        if cutoff and published and published[:19] < cutoff:
            continue
        category = classify(art["title"])
        out.append({
            "i": art["url"],
            "t": (art.get("title") or "")[:200],
            "s": art.get("account") or "微信公众号",
            "si": None,
            "d": published,
            "u": art["url"],
            "c": category,
            "g": intent_of(category),
            "en": "",
            "ens": "",
            "dt": {"sg": "wechat", "og": art.get("account") or "",
                   "ab": art.get("album") or ""},
        })
    return out


def merge_into_data(campus: str, items: list[dict],
                    retention_days: int = 90) -> dict:
    """按 `i` 合并进 data/<校区>.json：只追加不改历史，写前留 .bak。

    刻意**不 import refresh.py**：那是建站的事，这里只做"并入数据文件"。
    保留期算法跟着站点走（默认 90 天），所以合集里的老文章不会把 data/ 撑大。
    """
    path = os.path.join(DATA_DIR, f"{campus}.json")
    old = read_json(path, None)
    if not isinstance(old, dict):
        raise RuntimeError(f"读不到 {path}")
    merged = {i["i"]: i for i in (old.get("items") or []) if i.get("i")}
    before = len(merged)
    new = 0
    for it in items:
        if it["i"] not in merged:
            new += 1
        merged[it["i"]] = it
    final = sorted(merged.values(),
                   key=lambda x: (x.get("d") or "", x.get("i") or ""), reverse=True)

    cutoff = (datetime.datetime.now(article.CST)
              - datetime.timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%S")

    def expired(x: dict) -> bool:
        # 只对**公众号来源**套保留期：官网源的生命周期归 fetch_campus.py / refresh.py 管，
        # 这里顺手删人家的条目就越界了（第一版就这么干了，剔掉 1 条官网旧新闻）。
        if (x.get("dt") or {}).get("sg") != "wechat":
            return False
        return bool(x.get("d") and x["d"][:19] < cutoff)

    kept = [x for x in final if not expired(x)]
    dropped = len(final) - len(kept)

    payload = dict(old)
    payload["items"] = kept
    # 来源注册表：给每个公众号登记一条，页面「信息来源」能直接看到
    reg = dict(old.get("source_registry") or {})
    now = article.now_iso()
    per_account: dict[str, int] = {}
    for it in kept:
        if (it.get("dt") or {}).get("sg") != "wechat":
            continue
        acc = it.get("s") or "微信公众号"
        per_account[acc] = per_account.get(acc, 0) + 1
    for acc, n in per_account.items():
        key = "wx:" + acc
        reg[key] = {"key": key, "name": acc, "source_group": "wechat",
                    "count": n, "new": 0, "status": "ok", "last_checked": now,
                    "first_seen": (reg.get(key) or {}).get("first_seen") or now,
                    "url": "https://mp.weixin.qq.com/", "source_id": "",
                    "kind": "wechat-links"}
    payload["source_registry"] = reg
    payload["wechat_accounts"] = sorted(per_account)

    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(path):
        try:
            with open(path, "rb") as src, open(path + ".bak", "wb") as dst:
                dst.write(src.read())
        except OSError:
            pass
    write_json(path, payload)
    return {"total": len(kept), "before": before, "added": new,
            "dropped_by_retention": dropped, "accounts": len(per_account)}


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def summarize(lib: dict) -> dict:
    arts = list((lib.get("articles") or {}).values())
    a = [x for x in arts if x.get("tier") == TIER_A]
    b = [x for x in arts if x.get("tier") != TIER_A]

    def per_account(rows):
        out = {}
        for x in rows:
            acc = (x.get("account") or "（未知号）").strip()
            rec = out.setdefault(acc, {"count": 0, "first": "", "last": "", "albums": set()})
            rec["count"] += 1
            d = (x.get("published") or "")[:10]
            if d:
                rec["first"] = min(rec["first"] or d, d)
                rec["last"] = max(rec["last"] or d, d)
            if x.get("album"):
                rec["albums"].add(x["album"])
        for rec in out.values():
            rec["albums"] = sorted(rec["albums"])
        return out

    pa, pb = per_account(a), per_account(b)
    merged = {}
    for acc in set(pa) | set(pb):
        ra, rb = pa.get(acc) or {}, pb.get(acc) or {}
        merged[acc] = {
            "account": acc,
            "linkable": ra.get("count", 0),
            "metadata_only": rb.get("count", 0),
            "total": ra.get("count", 0) + rb.get("count", 0),
            "first": ra.get("first") or rb.get("first") or "",
            "last": ra.get("last") or rb.get("last") or "",
            "albums": ra.get("albums") or [],
        }
    rows = sorted(merged.values(), key=lambda r: (-r["total"], r["account"]))

    src = {}
    for x in arts:
        for s in x.get("sources") or []:
            head = s.split(":")[0]
            src[head] = src.get(head, 0) + 1

    return {
        "campus": lib.get("campus", ""),
        "campus_name": lib.get("campus_name", ""),
        "updated_at": lib.get("updated_at", ""),
        "linkable": len(a),
        "metadata_only": len(b),
        "total": len(arts),
        "publishers": len(merged),
        "linkable_publishers": len([r for r in rows if r["linkable"]]),
        "by_source": src,
        "rows": rows,
    }


def save_report(campus: str, summary: dict) -> str:
    return write_json(os.path.join(WORK_DIR, f"report-{campus}.json"), summary)
