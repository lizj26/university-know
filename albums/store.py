#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""落地：配置文件、增量状态、产物文件。

三种文件，职责严格分开：

    albums/albums.json             人工维护的**合集清单**（哪个合集属于哪个校区）
                                   —— 手改的文件，只存「事实」，不存抓取进度
    _work/albums/discovered-*.json 自动发现的合集（从种子文章页里挖出来的）
                                   —— 机器写的，可以被覆盖/重建，不影响人工清单
    _work/albums/state.json        增量水位（每个合集抓到哪个 msgid 了）
                                   —— 删掉只是下次全量重抓，不会丢数据

产物：
    _work/albums/albums-<校区>.json  一次抓取的完整结果（合集元信息 + 文章列表 + 健康状态）

**本包不写 `data/`**。这是刻意的：data/<校区>.json 是官网链路（fetch_campus.py /
refresh.py）和公众号正文链路（fetch_wechat.py）共用的合流点，合集抓取一旦往里写，
就又和那两条链路耦上了。要合流请用 `python -m albums export` 产出站点 item 结构，
由下游显式合并。
"""

from __future__ import annotations

import json
import os
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

CONFIG_PATH = os.path.join(HERE, "albums.json")
WORK_DIR = os.path.join(ROOT, "_work", "albums")
STATE_PATH = os.path.join(WORK_DIR, "state.json")

RETENTION_DAYS = 90          # 与站点一致；仅用于 export 时过滤旧文
SEEN_KEEP = 3000             # 每个合集记住多少条已见 key，防 state 无限膨胀


# --------------------------------------------------------------------------- #
# 通用 JSON IO（原子写：写坏了宁可保留上一版，也不要留半个文件）
# --------------------------------------------------------------------------- #
def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: str, payload) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return path


# --------------------------------------------------------------------------- #
# 人工清单
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    cfg = read_json(CONFIG_PATH, None)
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.setdefault("version", 1)
    cfg.setdefault("campuses", {})
    cfg.setdefault("albums", [])
    return cfg


def save_config(cfg: dict) -> str:
    return write_json(CONFIG_PATH, cfg)


def campus_config(cfg: dict, campus: str) -> dict:
    return (cfg.get("campuses") or {}).get(campus) or {}


def campuses(cfg: dict) -> list[str]:
    return list((cfg.get("campuses") or {}).keys())


def manual_albums(cfg: dict, campus: str | None = None) -> list[dict]:
    """人工清单里的合集，可按校区过滤。每条补上 `source="manual"`。"""
    out = []
    for rec in cfg.get("albums") or []:
        if not isinstance(rec, dict):
            continue
        if not rec.get("biz") or not rec.get("album_id"):
            continue
        if rec.get("disabled"):
            continue
        if campus and rec.get("campus") != campus:
            continue
        out.append({**rec, "source": "manual"})
    return out


def add_album(cfg: dict, campus: str, biz: str, album_id: str,
              title: str = "", account: str = "", note: str = "") -> bool:
    """往人工清单里加一条。已存在（同 biz+album_id）则补齐空字段并返回 False。"""
    for rec in cfg.get("albums") or []:
        if rec.get("biz") == biz and str(rec.get("album_id")) == str(album_id):
            changed = False
            for k, v in (("campus", campus), ("title", title), ("account", account)):
                if v and not rec.get(k):
                    rec[k] = v
                    changed = True
            if changed:
                save_config(cfg)
            return False
    cfg.setdefault("albums", []).append({
        "campus": campus, "biz": biz, "album_id": str(album_id),
        "title": title, "account": account, "note": note,
    })
    save_config(cfg)
    return True


# --------------------------------------------------------------------------- #
# 自动发现的合集
# --------------------------------------------------------------------------- #
def discovered_path(campus: str) -> str:
    return os.path.join(WORK_DIR, f"discovered-{campus}.json")


def load_discovered(campus: str) -> list[dict]:
    data = read_json(discovered_path(campus), [])
    return [d for d in data if isinstance(d, dict) and d.get("biz") and d.get("album_id")]


def save_discovered(campus: str, records: list[dict]) -> str:
    return write_json(discovered_path(campus), records)


def merge_discovered(campus: str, records: list[dict]) -> list[dict]:
    """并入已有发现结果（按 biz+album_id 去重），返回全量。"""
    merged = {(d["biz"], str(d["album_id"])): d for d in load_discovered(campus)}
    for rec in records:
        key = (rec["biz"], str(rec["album_id"]))
        old = merged.get(key) or {}
        merged[key] = {**old, **{k: v for k, v in rec.items() if v not in (None, "")},
                       "campus": campus}
    out = sorted(merged.values(), key=lambda d: (d.get("account") or "", d["album_id"]))
    save_discovered(campus, out)
    return out


def known_album_keys(cfg: dict, campus: str) -> set[str]:
    keys = {f"{r['biz']}:{r['album_id']}" for r in manual_albums(cfg, campus)}
    keys |= {f"{d['biz']}:{d['album_id']}" for d in load_discovered(campus)}
    return keys


# --------------------------------------------------------------------------- #
# 增量状态
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    data = read_json(STATE_PATH, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("albums", {})
    return data


def save_state(state: dict) -> str:
    return write_json(STATE_PATH, state)


def album_state(state: dict, key: str) -> dict:
    return (state.get("albums") or {}).get(key) or {}


def update_album_state(state: dict, key: str, **fields) -> dict:
    rec = dict(album_state(state, key))
    seen = list(rec.get("seen") or [])
    new_seen = [k for k in (fields.pop("seen", None) or []) if k not in set(seen)]
    if new_seen:
        seen.extend(new_seen)
        rec["seen"] = seen[-SEEN_KEEP:]
    rec.update(fields)
    state.setdefault("albums", {})[key] = rec
    return rec


def max_msgid(rows) -> str:
    """一组文章里最大的 msgid（微信 msgid 随发布时间递增）。"""
    best = 0
    for r in rows:
        try:
            best = max(best, int(getattr(r, "msgid", 0) or 0))
        except (TypeError, ValueError):
            continue
    return str(best) if best else ""


# --------------------------------------------------------------------------- #
# 产物
# --------------------------------------------------------------------------- #
def output_path(campus: str) -> str:
    return os.path.join(WORK_DIR, f"albums-{campus}.json")


def save_output(campus: str, payload: dict) -> str:
    return write_json(output_path(campus), payload)


# --------------------------------------------------------------------------- #
# 种子文章页（只读；用来「发现合集」）
# --------------------------------------------------------------------------- #
def seed_files(cfg: dict, campus: str) -> list[str]:
    """种子清单文件。

    优先用本包自己的 `albums/seeds-<校区>.txt`；没有再回退到公众号链路已有的
    `_work/wechat/wechat-<校区>.txt`（**只读**，不修改）。
    这里读的是数据文件而不是代码，所以不构成两条链路的代码耦合。
    """
    ccfg = campus_config(cfg, campus)
    names = ccfg.get("seeds") or [f"albums/seeds-{campus}.txt",
                                  f"_work/wechat/wechat-{campus}.txt"]
    out = []
    for name in names:
        path = name if os.path.isabs(name) else os.path.join(ROOT, name)
        if os.path.exists(path):
            out.append(path)
    return out


def load_seed_links(cfg: dict, campus: str, limit: int = 40) -> list[str]:
    links, seen = [], set()
    for path in seed_files(cfg, campus):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("http") and "mp.weixin.qq.com" in line and line not in seen:
                        seen.add(line)
                        links.append(line)
        except OSError:
            continue
    return links[:limit]


def load_seed_groups(cfg: dict, campus: str) -> list[tuple[str, list[str]]]:
    """按 `# 账号名` 注释把种子链接分组 → [(账号提示, [链接...])]。

    种子文件本来就是「一个号一组、组前一行注释」，所以分组不需要额外配置。
    分组的意义是**发现阶段能少抓很多页**：同一个号的文章页会指向同一个合集，
    每个号抽 1~2 篇就够了，不必把几十个链接全抓一遍（每篇 3.7 MB）。
    """
    groups: list[tuple[str, list[str]]] = []
    for path in seed_files(cfg, campus):
        try:
            with open(path, encoding="utf-8") as fh:
                label, urls = "", []
                for raw in fh:
                    line = raw.strip()
                    if line.startswith("#"):
                        if urls:
                            groups.append((label, urls))
                        label, urls = line.lstrip("#").strip(), []
                    elif line.startswith("http") and "mp.weixin.qq.com" in line:
                        urls.append(line)
                if urls:
                    groups.append((label, urls))
        except OSError:
            continue
    return [(label, urls) for label, urls in groups if urls]


def sample_seed_links(cfg: dict, campus: str, *, per_account: int = 2,
                      limit: int = 24) -> list[tuple[str, str]]:
    """每个号取前 N 篇 → [(账号提示, 链接)]，总量不超过 limit。"""
    picked: list[tuple[str, str]] = []
    seen = set()
    for label, urls in load_seed_groups(cfg, campus):
        taken = 0
        for url in urls:
            if taken >= max(1, per_account) or len(picked) >= limit:
                break
            if url in seen:
                continue
            seen.add(url)
            picked.append((label, url))
            taken += 1
        if len(picked) >= limit:
            break
    if not picked:     # 没有注释分组（单行清单）时退回逐条
        picked = [("", url) for url in load_seed_links(cfg, campus, limit=limit)]
    return picked
