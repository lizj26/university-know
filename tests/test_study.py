#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线验证「学习资料」清单：校验规则 + 构建注入结果。

不联网、不写任何文件，只读 data/study/materials.json 和已生成的 index.html。

用法:
    python refresh.py --build-only     # 先构建
    python tests/test_study.py
"""
import importlib.util
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)

spec = importlib.util.spec_from_file_location("refresh", os.path.join(PROJ, "refresh.py"))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)

OK = 0
FAIL = 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f"  [{detail}]" if detail else ""))


GOOD = {"name": "示例仓库", "tag": "课程 · 主站", "desc": "一句话说明。",
        "site": "https://example.github.io/", "siteLabel": "在线浏览",
        "repo": "https://github.com/a/b"}


def study(*items, title="某校学习资料", note="说明"):
    return {"x": {"title": title, "note": note, "items": list(items)}}


print("— 校验规则 —")
clean, problems = R.normalize_study(study(GOOD))
check("合法条目通过", list(clean) == ["x"] and len(clean["x"]["items"]) == 1 and not problems,
      f"{len(clean.get('x', {}).get('items', []))} 条")
check("站点与仓库都保留", clean["x"]["items"][0].get("site") and clean["x"]["items"][0].get("repo"))
check("siteLabel 保留", clean["x"]["items"][0].get("siteLabel") == "在线浏览")

clean, problems = R.normalize_study({"_readme": ["说明"], **study(GOOD)})
check("下划线开头的字段被忽略", list(clean) == ["x"] and not problems)

clean, problems = R.normalize_study(study({"name": "只有名字"}))
check("缺 tag/desc 被跳过并报错", not clean and any("缺少" in p for p in problems),
      problems[0] if problems else "")

clean, problems = R.normalize_study(study({**GOOD, "site": "ftp://x/y"}))
check("非 http 链接只忽略该按钮", clean["x"]["items"][0].get("repo") and "site" not in clean["x"]["items"][0])
check("非 http 链接记一条问题", len(problems) == 1, problems[0] if problems else "")

clean, problems = R.normalize_study(study({"name": "n", "tag": "t", "desc": "d"}))
check("没有任何链接的条目被跳过", not clean and any("一个都没有" in p for p in problems),
      problems[0] if problems else "")

clean, problems = R.normalize_study(study({**GOOD, "mirror": {"label": "镜像"}}))
check("mirror 缺 url 被忽略", "mirror" not in clean["x"]["items"][0] and len(problems) == 1)

clean, problems = R.normalize_study(study({**GOOD, "mirror": {"label": "Gitee", "url": "https://gitee.com/a/b"}}))
check("合法 mirror 保留", clean["x"]["items"][0].get("mirror", {}).get("label") == "Gitee")

clean, problems = R.normalize_study(study(GOOD, "这不是对象"))
check("数组里的脏数据不拖垮整份清单", len(clean["x"]["items"]) == 1 and len(problems) == 1)

clean, problems = R.normalize_study([])
check("顶层不是对象 → 空清单", clean == {} and len(problems) == 1)

clean, problems = R.normalize_study({"x": {"title": "t", "items": "不是数组"}})
check("items 不是数组 → 该校区跳过", clean == {} and len(problems) == 1)

print("— 真实清单 data/study/materials.json —")
path = os.path.join(PROJ, "data", "study", "materials.json")
check("清单文件存在", os.path.exists(path), os.path.relpath(path, PROJ))
raw = {}
if os.path.exists(path):
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
clean, problems = R.normalize_study(raw)
check("真实清单零警告", not problems, "；".join(problems) if problems else "0 条")
check("三个校区都有入口", sorted(clean) == ["sjtu", "xjtu", "zju"], ", ".join(sorted(clean)))
check("每校都有 title / note", all(v["title"] and v["note"] for v in clean.values()))
check("每校至少 2 条链接", all(len(v["items"]) >= 2 for v in clean.values()),
      "、".join(f"{k}:{len(v['items'])}" for k, v in clean.items()))
check("每条都有可用外链",
      all(any(it.get(f) for f in ("site", "repo", "mirror")) for v in clean.values() for it in v["items"]))
check("校区 key 都在顶栏顺序里", all(k in R.CAMPUS_ORDER if hasattr(R, "CAMPUS_ORDER") else True for k in clean))

print("— 构建产物 index.html —")
html_path = os.path.join(PROJ, "index.html")
html = open(html_path, encoding="utf-8").read() if os.path.exists(html_path) else ""
check("index.html 存在", bool(html))
check("占位符已被替换", "__STUDY_DATA__" not in html)
check("模板仍保留占位符",
      "__STUDY_DATA__" in open(os.path.join(PROJ, "template.html"), encoding="utf-8").read())

m = re.search(r'<script id="study-data" type="application/json">(.*?)</script>', html, re.S)
check("页面里有 study-data 数据块", bool(m))
injected = {}
if m:
    injected = json.loads(m.group(1))
check("注入内容与清单一致", sorted(injected) == sorted(clean), ", ".join(sorted(injected)))
check("注入条数与清单一致",
      sum(len(v.get("items", [])) for v in injected.values()) ==
      sum(len(v.get("items", [])) for v in clean.values()))
check("注入内容不含 < 裸字符", m and "<" not in m.group(1))

print(f"\n通过 {OK}/{OK + FAIL}")
sys.exit(0 if FAIL == 0 else 1)
