#!/usr/bin/env python3
"""把某个校区的数据"收窄"到当前 SOURCES 里配置的来源。

用途：调整 fetch_campus.py 的 SOURCES（例如只保留「官网 + 职能部门」、下线大批
学院站）后，历史 data/<校区>.json 里仍然留着已下线来源的旧条目 —— 抓取器本身
是"只增不减"的，不会主动删。跑一次本脚本即可立刻把库收窄到当前来源集合，
同时清掉 _work/ 里的同名中间产物，让后续抓取从干净状态继续。

用法
----
    python tools/refocus_data.py                 # 处理 zju,sjtu,xjtu
    python tools/refocus_data.py zju             # 只处理浙大
    python tools/refocus_data.py --dry-run       # 只看会删多少，不写盘

写盘前会自动备份成 data/<校区>.json.bak。
"""
import argparse
import glob
import importlib.util
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(ROOT, "data")
WORK_DIR = os.path.join(ROOT, "_work")


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def valid_ids_for(campus):
    fc = load_module(os.path.join(ROOT, "fetch_campus.py"), "fetch_campus_mod")
    return {str(s["id"]) for s in fc.SOURCES.get(campus) or []}


def shrink_payload(payload, ids):
    """按来源 id 集合收窄一份数据(条目 + 注册表 + 统计)。"""
    items = [it for it in (payload.get("items") or [])
             if isinstance(it, dict) and (it.get("si") in ids)]
    reg = {k: v for k, v in (payload.get("source_registry") or {}).items() if k in ids}
    old = dict(payload)
    old.update({
        "items": items,
        "source_registry": reg,
        "fetched": len(items),
        "total_all": len(items),
        "wechat_accounts": [],
    })
    return old


def main():
    ap = argparse.ArgumentParser(description="把数据收窄到当前 SOURCES 配置的来源")
    ap.add_argument("campus", nargs="*", default=None,
                    help="校区(默认 zju sjtu xjtu)")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写盘")
    args = ap.parse_args()

    campuses = args.campus or ["zju", "sjtu", "xjtu"]
    refresh = load_module(os.path.join(ROOT, "refresh.py"), "refresh_mod")

    for campus in campuses:
        ids = valid_ids_for(campus)
        if not ids:
            print(f"· [{campus}] SOURCES 里没有来源，跳过")
            continue

        data_path = os.path.join(DATA_DIR, f"{campus}.json")
        if not os.path.exists(data_path):
            print(f"· [{campus}] 没有 {data_path}，跳过")
            continue
        with open(data_path, encoding="utf-8") as fh:
            payload = json.load(fh)

        before_items = len(payload.get("items") or [])
        before_srcs = len(payload.get("source_registry") or {})
        payload = shrink_payload(payload, ids)
        payload.update(refresh.recompute_stats(
            payload["items"], payload.get("sources") or [], payload["source_registry"]))
        after_items = len(payload["items"])
        after_srcs = len(payload["source_registry"])
        print(f"· [{campus}] 条目 {before_items} → {after_items}"
              f"，来源 {before_srcs} → {after_srcs}"
              f"（保留来源：{len(ids)}）")

        if args.dry_run:
            continue
        if os.path.exists(data_path):
            shutil.copy2(data_path, data_path + ".bak")
        with open(data_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))

        for mid in (os.path.join(WORK_DIR, f"items-{campus}.json"),
                    os.path.join(WORK_DIR, f"registry-{campus}.json")):
            if not os.path.exists(mid):
                continue
            try:
                with open(mid, encoding="utf-8") as fh:
                    m = json.load(fh)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(m, dict) and "source_registry" in m:
                m = shrink_payload(m, ids)
            elif isinstance(m, dict):                      # registry-<校区>.json 本体
                m = {k: v for k, v in m.items() if k in ids}
            with open(mid, "w", encoding="utf-8") as fh:
                json.dump(m, fh, ensure_ascii=False, separators=(",", ":"))

    if not args.dry_run:
        print("✓ 收窄完成（已生成 .bak 备份）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
