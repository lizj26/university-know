#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 tools/campus_roster.py 里的校区表同步进全站各处配置（幂等，可反复跑）。

改动位置
--------
1. refresh.py            CAMPUS_PREFIX / CAMPUS_LABEL / CAMPUS_EN
2. template.html         CAMPUS_NAMES / CAMPUS_ORDER
3. cloud_update.py       CAMPUS_NAMES / order
4. .github/workflows/update.yml   fetch job 的 matrix.include
5. tools/refocus_data.py 默认校区列表

用法
----
    python tools/register_campus.py            # 同步（先打印将改动的行数）
    python tools/register_campus.py --check    # 只检查是否已同步，不做修改
"""
import argparse
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from campus_roster import CAMPUSES  # noqa: E402


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def sub_once(text, pattern, repl, path, what):
    new, n = re.subn(pattern, repl, text, count=1, flags=re.M | re.S)
    if n != 1:
        print(f"  ! {path}: 没找到 {what}（正则未命中），已跳过")
        return text, 0
    return new, 1


def py_dict(rows, value_fn, indent=4):
    """生成一行一个 key 的 dict 文本（缩进对齐）。"""
    lines = []
    for row in rows:
        key = row[0]
        val = value_fn(row)
        lines.append(f'{" " * indent}"{key}": {val},')
    return "{\n" + "\n".join(lines) + "\n}"


def patch_refresh(check):
    path = os.path.join(ROOT, "refresh.py")
    text = read(path)
    changed = 0
    for name, fn in (
        ("CAMPUS_PREFIX", lambda r: f'"{r[4]}"' if r[4] else "None"),
        ("CAMPUS_LABEL", lambda r: f'"{r[2]}"'),
        ("CAMPUS_EN", lambda r: f'"{r[3]}"'),
    ):
        body = py_dict(CAMPUSES, fn, indent=4)
        pat = rf'^{name} = \{{.*?\}}'
        text, n = sub_once(text, pat, f"{name} = {body}", "refresh.py", name)
        changed += n
    if changed and not check:
        write(path, text)
    return changed


def patch_template(check):
    path = os.path.join(ROOT, "template.html")
    text = read(path)
    changed = 0
    names = "  var CAMPUS_NAMES = {\n"
    rows = []
    for r in CAMPUSES:
        ext = f', ext:"{r[5]}"' if len(r) > 5 and r[5] else ""
        rows.append(f'    {r[0]}:{{ zh:"{r[2]}", en:"{r[3]}"{ext} }},')
    names += "\n".join(rows)
    names = names.rstrip(",") + "\n  };"
    text, n = sub_once(text, r'^  var CAMPUS_NAMES = \{.*?^  \};', names,
                       "template.html", "CAMPUS_NAMES")
    changed += n
    order = "  var CAMPUS_ORDER = [" + ", ".join(f'"{r[0]}"' for r in CAMPUSES) + "];"
    text, n = sub_once(text, r'^  var CAMPUS_ORDER = \[.*?\];', order,
                       "template.html", "CAMPUS_ORDER")
    changed += n
    if changed and not check:
        write(path, text)
    return changed


def patch_cloud_update(check):
    path = os.path.join(ROOT, "cloud_update.py")
    text = read(path)
    body = "CAMPUS_NAMES = {\n"
    body += "\n".join(f'    "{r[0]}": "{r[2][:-2]}",' for r in CAMPUSES)
    body += "\n}"
    text, n1 = sub_once(text, r'^CAMPUS_NAMES = \{.*?^\}', body,
                        "cloud_update.py", "CAMPUS_NAMES")
    order = "    order = [" + ", ".join(f'"{r[0]}"' for r in CAMPUSES) + "]"
    text, n2 = sub_once(text, r'^    order = \[.*?\]', order,
                        "cloud_update.py", "order")
    if n1 + n2 and not check:
        write(path, text)
    return n1 + n2


def patch_workflow(check):
    """matrix.include：外链校区（清华）本站不抓取，不进矩阵；zju 保留低速档（WAF），
    其余走「大规模档」。

    大规模档 = --workers 6（同校不同主机并行）+ --timeout 12 --retries 1
    （卡死来源最坏耗时从 75s 降到 24s，见 docs/规模与耗时.md）。
    """
    path = os.path.join(ROOT, ".github", "workflows", "update.yml")
    text = read(path)
    lines = []
    for r in CAMPUSES:
        key = r[0]
        if len(r) > 5 and r[5]:      # 外链校区：本站不抓取，交给外部站点
            continue
        if key == "zju":
            cmd = ("python fetch_campus.py --campus zju --workers 2 --delay 2.0 "
                   "--domain-delay 1.0 --rotate --timeout 15 --retries 1 --no-build")
        else:
            cmd = (f"python fetch_campus.py --campus {key} --workers 6 --delay 1.2 "
                   f"--timeout 12 --retries 1 --no-build")
        lines.append(f"          - campus: {key}\n            command: {cmd}")
    block = "        include:\n" + "\n".join(lines)
    text, n = sub_once(text, r'^        include:\n(?:          - campus:.*\n?)*', block + "\n",
                       "update.yml", "matrix.include")
    if n and not check:
        write(path, text)
    return n


def patch_refocus(check):
    path = os.path.join(ROOT, "tools", "refocus_data.py")
    text = read(path)
    default = ", ".join(f'"{r[0]}"' for r in CAMPUSES if not (len(r) > 5 and r[5]))
    text, n = sub_once(text, r'^    campuses = args\.campus or \[.*?\]',
                       f"    campuses = args.campus or [{default}]",
                       "tools/refocus_data.py", "默认校区列表")
    if n and not check:
        write(path, text)
    return n


def main():
    ap = argparse.ArgumentParser(description="把校区表同步进全站配置")
    ap.add_argument("--check", action="store_true", help="只检查，不改文件")
    args = ap.parse_args()

    names = "、".join(r[2] for r in CAMPUSES[:8]) + ("" if len(CAMPUSES) <= 8 else " 等")
    print(f"· 校区表共 {len(CAMPUSES)} 所：" + names)
    total = 0
    for fn in (patch_refresh, patch_template, patch_cloud_update,
               patch_workflow, patch_refocus):
        total += fn(args.check)
    print(("✓ 已同步" if not args.check else "· 检查完成") + f"：{total} 处配置块")
    if args.check:
        print("  （--check 未写文件；去掉该参数即会写入）")


if __name__ == "__main__":
    main()
