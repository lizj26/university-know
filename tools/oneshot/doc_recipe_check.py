#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证《修改指南》里的样式配方真的能用:在副本上照做一遍然后重新生成。

不改动真实站点文件,全部在 _pipe_test/doc_demo 下进行。
用法: python build/_check/doc_recipe_check.py
"""
import importlib.util
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DEMO = os.path.join(ROOT, "_pipe_test", "doc_demo")
results = []


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def recipe(html, old, new, label):
    """按文档描述做一次替换,替换不到就直接失败。"""
    if old not in html:
        raise AssertionError("找不到《修改指南》里描述的原文:" + label)
    return html.replace(old, new, 1)


# ---------- 1. 准备副本 ----------
shutil.rmtree(DEMO, ignore_errors=True)
os.makedirs(os.path.join(DEMO, "data"))
tpl = read(os.path.join(ROOT, "template.html"))

# ---------- 2. 照《修改指南》2.4 加一个 mint 色调 ----------
tpl = recipe(
    tpl,
    "--tone-slate-ink:#456582;  --tone-slate-tint:#dcebf4;  --tone-slate-surface:#f1f7fc;  --tone-slate-edge:#d5e2eb;",
    "--tone-slate-ink:#456582;  --tone-slate-tint:#dcebf4;  --tone-slate-surface:#f1f7fc;  --tone-slate-edge:#d5e2eb;\n"
    "  --tone-mint-ink:#2f6f5e; --tone-mint-tint:#d6f0e6; --tone-mint-surface:#f0faf6; --tone-mint-edge:#cfe8de;",
    "浅色 --tone-slate 行")
tpl = recipe(
    tpl,
    "--tone-slate-ink:#c5ddeb;  --tone-slate-tint:#475b71;  --tone-slate-surface:#333c4e;  --tone-slate-edge:#566d83;",
    "--tone-slate-ink:#c5ddeb;  --tone-slate-tint:#475b71;  --tone-slate-surface:#333c4e;  --tone-slate-edge:#566d83;\n"
    "  --tone-mint-ink:#a8e6d0; --tone-mint-tint:#3c5c53; --tone-mint-surface:#2c3f3b; --tone-mint-edge:#4a6b61;",
    "暗色 --tone-slate 行")
tpl = recipe(
    tpl,
    "[data-tone=slate]{--tone-ink:var(--tone-slate-ink);--tone-tint:var(--tone-slate-tint);"
    "--tone-surface:var(--tone-slate-surface);--tone-edge:var(--tone-slate-edge)}",
    "[data-tone=slate]{--tone-ink:var(--tone-slate-ink);--tone-tint:var(--tone-slate-tint);"
    "--tone-surface:var(--tone-slate-surface);--tone-edge:var(--tone-slate-edge)}\n"
    "[data-tone=mint]{--tone-ink:var(--tone-mint-ink);--tone-tint:var(--tone-mint-tint);"
    "--tone-surface:var(--tone-mint-surface);--tone-edge:var(--tone-mint-edge)}",
    "[data-tone=slate] 规则")

# ---------- 3. 照 2.6 加一个 rocket 图标 ----------
tpl = recipe(
    tpl,
    "    clock:'<circle cx=\"12\" cy=\"12\" r=\"10\"/>",
    "    rocket:'<path d=\"M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2z\"/><path d=\"m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2z\"/>',\n"
    "    clock:'<circle cx=\"12\" cy=\"12\" r=\"10\"/>",
    "P 表里的 clock 图标")

# ---------- 4. 照 2.5 加一个分类并挂到左侧「活动」分组 ----------
tpl = recipe(
    tpl,
    '    { name:"校园服务", tone:"water", icon:"buoy",         group:"information", short:"服务" }',
    '    { name:"校园服务", tone:"water", icon:"buoy",         group:"information", short:"服务" },\n'
    '    { name:"通识讲座", tone:"mint",  icon:"rocket",       group:"activity",    short:"通识" }',
    "CATEGORY_META 最后一项")
tpl = recipe(
    tpl,
    'cats:["文体活动","社团公益","讲座活动","学习成长","实习就业","交流访学"]',
    'cats:["文体活动","社团公益","讲座活动","学习成长","实习就业","交流访学","通识讲座"]',
    "renderClusters 的 activity 分组")

write(os.path.join(DEMO, "template.html"), tpl)

# 一条属于新分类的通知
write(os.path.join(DEMO, "data", "zju.json"), json.dumps({
    "campus": "zju", "campus_name": "浙大知道",
    "scraped_at": "2026-10-09T12:00:00+08:00", "site_last_sync": "2026-10-09T11:00:00+08:00",
    "interval_minutes": 5, "total_all": 1, "fetched": 1, "truncated": False, "fetch_warnings": [],
    "groups": [{"id": "wechat", "sources": 1, "count": 1}],
    "intent_totals": [{"intent_group": "activity", "count": 1}],
    "category_totals": [{"name": "通识讲座", "count": 1}],
    "sources": [{"name": "浙大官微", "source_group": "wechat", "count": 1}],
    "items": [{"i": "mint-1", "t": "通识讲座示例", "s": "浙大官微", "si": "weixinzs_x",
               "d": "2026-10-09T10:00:00+08:00", "u": "https://example.com/1",
               "c": "通识讲座", "g": "activity", "dt": {"sg": "wechat"}}],
}, ensure_ascii=False, separators=(",", ":")))

# ---------- 5. 在副本里重新生成 ----------
spec = importlib.util.spec_from_file_location("refresh", os.path.join(ROOT, "refresh.py"))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)
R.DATA_DIR = os.path.join(DEMO, "data")
R.TEMPLATE = os.path.join(DEMO, "template.html")
R.OUTPUT = os.path.join(DEMO, "index.html")
R.force_utf8_stdout()
R.build()

html = read(R.OUTPUT)
blob = re.search(r'<script id="notice-data" type="application/json">(.*?)</script>', html, re.S).group(1)
embedded = json.loads(blob.replace("\\u003c", "<"))


def check(label, cond, detail=""):
    results.append((label, bool(cond), detail))


check("新色调的浅色变量进了页面", "--tone-mint-ink:#2f6f5e" in html)
check("新色调的暗色变量进了页面", "--tone-mint-ink:#a8e6d0" in html)
check("[data-tone=mint] 映射规则存在", "[data-tone=mint]{" in html and "--tone-ink:var(--tone-mint-ink)" in html)
check("新图标 rocket 进了 P 表", "rocket:'<path" in html)
check("新分类进了 CATEGORY_META", '{ name:"通识讲座", tone:"mint",  icon:"rocket"' in html)
check("新分类挂到了左侧「活动」分组",
      'cats:["文体活动","社团公益","讲座活动","学习成长","实习就业","交流访学","通识讲座"]' in html)
check("新分类的数据被内嵌进页面",
      embedded["campuses"]["zju"]["items"][0]["c"] == "通识讲座",
      embedded["campuses"]["zju"]["items"][0]["c"])
check("默认校区跟随数据(只有 zju)", embedded["default_campus"] == "zju", embedded["default_campus"])
check("标题跟着默认校区改写", "<title>「浙大知道」 ZJU Know</title>" in html,
      re.search(r"<title>(.*?)</title>", html).group(1).encode("unicode_escape").decode())

failed = [r for r in results if not r[1]]
for label, ok, detail in results:
    print(("  OK   " if ok else "  FAIL ") + label + (("  [" + str(detail) + "]") if detail else ""))
print("\n通过 %d/%d" % (len(results) - len(failed), len(results)))
sys.exit(1 if failed else 0)
