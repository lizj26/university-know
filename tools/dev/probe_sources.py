#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""来源侦察:批量试访问候选院系/职能站点,看哪些可达、页面里有多少文章和官微链接。

用途:往 fetch_campus.py 的 SOURCES 里加来源之前,先用它筛一遍,避免加进死链或空栏目。

用法:
    python build/_check/probe_sources.py             # 三个校区都试
    python build/_check/probe_sources.py --campus zju
    python build/_check/probe_sources.py --campus sjtu --max 40

输出里带 ★ 的是可用的;末尾会给出可以直接粘进 SOURCES 的条目草稿。
"""
import argparse
import importlib.util
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("fc", os.path.join(ROOT, "fetch_campus.py"))
fc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fc)

# 候选站点:名字 -> 首页。加来源时往这里补即可。
CANDIDATES = {
    "zju": [
        ("公共体育与艺术部", "http://www.tyys.zju.edu.cn/"),
        ("材料学院", "https://mse.zju.edu.cn/"),
        ("建筑工程学院", "http://www.ccea.zju.edu.cn/"),
        ("光电学院", "http://www.isee.zju.edu.cn/"),
        ("化学系", "http://www.chem.zju.edu.cn/"),
        ("数学学院", "http://www.math.zju.edu.cn/"),
        ("外国语学院", "http://www.sis.zju.edu.cn/"),
        ("医学院", "http://www.med.zju.edu.cn/"),
        ("农业与生物技术学院", "http://www.cab.zju.edu.cn/"),
        ("学生就业指导中心", "http://www.career.zju.edu.cn/"),
        ("校医院", "http://zdyy.zju.edu.cn/"),
    ],
    "sjtu": [
        ("数学科学学院", "https://math.sjtu.edu.cn/"),
        ("物理与天文学院", "https://physics.sjtu.edu.cn/"),
        ("计算机学院", "https://www.cs.sjtu.edu.cn/"),
        ("材料科学与工程学院", "https://smse.sjtu.edu.cn/"),
        ("生物医学工程学院", "https://bme.sjtu.edu.cn/"),
        ("化学化工学院", "https://scce.sjtu.edu.cn/"),
        ("外国语学院", "https://sfl.sjtu.edu.cn/"),
        ("人文学院", "https://shss.sjtu.edu.cn/"),
        ("药学院", "https://pharm.sjtu.edu.cn/"),
        ("教务处", "https://jwc.sjtu.edu.cn/"),
        ("研究生院", "https://gs.sjtu.edu.cn/"),
        ("图书馆", "https://lib.sjtu.edu.cn/"),
    ],
    "xjtu": [
        ("数学与统计学院", "https://math.xjtu.edu.cn/"),
        ("物理学院", "https://physics.xjtu.edu.cn/"),
        ("管理学院", "https://som.xjtu.edu.cn/"),
        ("材料科学与工程学院", "https://mse.xjtu.edu.cn/"),
        ("化学工程与技术学院", "https://clet.xjtu.edu.cn/"),
        ("能源与动力工程学院", "https://epe.xjtu.edu.cn/"),
        ("医学部", "https://med.xjtu.edu.cn/"),
        ("人文社会科学学院", "https://rwxy.xjtu.edu.cn/"),
        ("外国语学院", "https://sfl.xjtu.edu.cn/"),
        ("计算机科学与技术学院", "https://cs.xjtu.edu.cn/"),
        ("教务处", "https://jwc.xjtu.edu.cn/"),
        ("研究生院", "https://gs.xjtu.edu.cn/"),
        ("图书馆", "https://lib.xjtu.edu.cn/"),
    ],
}


def probe(name, url, timeout=20):
    """返回 (文章数, 官微链接数, 页面标题, 落点 URL);不可达就抛异常。"""
    html, final = fc.fetch(url, delay=1.0, retries=1, timeout=timeout)
    host = fc.host_of(final)
    seen, arts = set(), 0
    for href, text, _pos in fc.anchors(html, final):
        if href in seen or not fc.is_article_link(href, host):
            continue
        if len(re.sub(r"\s", "", text)) < 6:
            continue
        seen.add(href)
        arts += 1
    wx = len(set(fc.WX_RE.findall(html)))
    return arts, wx, fc.page_title(html), final


def main():
    ap = argparse.ArgumentParser(description="探测候选信息来源是否可用")
    ap.add_argument("--campus", default="zju,sjtu,xjtu", help="逗号分隔:zju,sjtu,xjtu")
    ap.add_argument("--timeout", type=float, default=20)
    args = ap.parse_args()

    for campus in [c.strip() for c in args.campus.split(",") if c.strip()]:
        items = CANDIDATES.get(campus)
        if not items:
            print(f"· 没有为 {campus} 准备候选站点")
            continue
        print("=" * 92)
        print(f"### {campus}")
        print("%-24s %-6s %-9s %-6s %-5s %s" % ("来源", "状态", "大小", "文章", "官微", "标题"))
        usable = []
        for name, url in items:
            try:
                arts, wx, title, final = probe(name, url, args.timeout)
            except Exception as exc:  # noqa: BLE001
                print("%-24s %-6s %s" % (name, "×", type(exc).__name__))
                continue
            mark = "★" if (arts >= 5 or wx >= 3) else " "
            print("%-24s %-6s %-9s %-6d %-5d %s %s" % (name, "OK", "-", arts, wx, mark, title[:26]))
            if arts >= 5 or wx >= 3:
                usable.append((name, url, arts, wx))

        print(f"\n可用 {len(usable)} 个，可直接粘进 SOURCES[\"{campus}\"]：")
        for i, (name, url, arts, wx) in enumerate(usable, 1):
            sid = f'{campus}_probe{i}'
            print('        {"id": "%s", "name": "%s", "group": "official",' % (sid, name))
            print('         "url": "%s", "kind": "%s", "max": 10},   # 文章 %d / 官微 %d'
                  % (url, "wechat" if arts < 5 else "list", arts, wx))
        print()


if __name__ == "__main__":
    main()
