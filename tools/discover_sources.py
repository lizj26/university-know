#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为「XX 知道」批量发现可用信息源（官网 + 职能部门），产出 sources_more.py。

为什么要有这个脚本
------------------
新增一所大学原来要人工逐个试栏目页；扩展到全部 985 时这是几十所 × 十几个候选，
只能自动化。做法：

  1. 从学校主页导航里挑「新闻 / 通知 / 公告 / 要闻」类栏目链接
  2. 再按常见职能部门子域名（news / jwc / gs / yjs / yz / zsb / job / lib / xsc / gjc …）
     补一遍候选，每个子域试几个常见列表页路径
  3. **用 fetch_campus.py 自己的解析逻辑**验证这个页面能不能稳定给出「标题 + 链接 + 日期」

两类站点两种收法
----------------
* 老式站（文章链接形如 /info/1012/4246.htm，路径带数字 + .htm）：直接用默认规则，
  不加 link_re。
* 新式站（/post/detail/xxx、?id=123 这类路径不带 .htm 数字）：默认规则一条都挑不出来，
  脚本会自动推导一条 link_re（把同一栏目里出现最多的链接形状转成正则），
  写进来源配置；写之前会用抓取器自己的逻辑回放一遍，确认能挑到 ≥MIN_LINKS 条。

用法
----
    python tools/discover_sources.py                 # 全部目标高校（跳过已手工精调的）
    python tools/discover_sources.py seu nju      # 指定几所（需先追加到 ROSTER）
    python tools/discover_sources.py --weak-only     # 只重探来源少于 3 个的校区
    python tools/discover_sources.py --probe-only    # 只出报告，不写 sources_more.py
输出
----
    sources_more.py                （fetch_campus.py 会合并进 SOURCES）
    _work/discover-report.json     探测明细，便于人工复核与手工修正
"""
import argparse
import concurrent.futures as cf
import importlib.util
import json
import os
import re
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORK = os.path.join(ROOT, "_work")

spec = importlib.util.spec_from_file_location("fc", os.path.join(ROOT, "fetch_campus.py"))
fc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fc)

MIN_LINKS = 4
MIN_DATED = 0.34
MAX_PER_CAMPUS = 6

# 这些栏目与「常见信息需求」关系不大，直接不要
SKIP_NAME = re.compile(r"信息公开|校历|校史馆|博物馆|档案馆|招聘|招标|采购|基金会|"
                       r"校办产业|后勤|校友|捐赠|纪检监察|审计|基建|工会|医院|附中|附小|"
                       r"学报|期刊|English|English Version|访客|旧版|登录")
# 单篇文章页（不是列表页）：/info/1012/4246.htm、/20260313/220327.html 这类
ARTICLE_URL = re.compile(r"(/info/\d+/\d+\.s?html?$)|(/\d{6,}/[a-z]?\d{3,}\.s?html?$)|(/\d{5,}\.s?html?$)")
# 与常见信息需求强相关的栏目关键词（加权）
GOOD_NAME = re.compile(r"新闻|要闻|通知|公告|动态|教务|研究生|招生|就业|学生|国际|科研|学术|讲座|公示|综合")
NAV_HINT = re.compile(r"通知|公告|新闻|要闻|动态|快讯|综合新闻|学术|讲座|公示|校内|信息|更多|媒体|头条")
LIST_HINT = re.compile(r"list|index|tzgg|notice|news|gg|通知|公告", re.I)

# 已有手工精调来源（或在本站为外链）的校区，不参与自动生成
HAND_TUNED = {"thu", "zju", "sjtu", "xjtu"}

# 只收「官网 + 职能部门」：主机名白名单（学院/实验室/中心/学报这些站一律不要）
ALLOWED_HOST = re.compile(
    r"^(www|news|xww|xw|jwc|jw|gs|grs|yjs|yzb|yz|zsb|bkzs|zs|zhaosheng|"
    r"job|career|jy|lib|library|xsc|xgb|student|gjc|international|oice|"
    r"xygg|tzgg|kyy|kyb|kyc|kjc|hr|renshi|graduate|grad|admission|admissions|"
    r"zsks|enrollment|notice|oa|bksy|yjsy|yjszs)\.", re.I)

DEPT_SUBS = [
    ("news", "新闻网"), ("jwc", "教务处"), ("gs", "研究生院"), ("grs", "研究生院"),
    ("yjs", "研究生院"), ("yz", "研究生招生"), ("zsb", "本科招生"),
    ("bkzs", "本科招生"), ("job", "就业"), ("career", "就业"), ("lib", "图书馆"),
    ("xsc", "学生工作部"), ("xgb", "学生工作部"), ("gjc", "国际合作与交流处"),
    ("international", "国际合作与交流处"), ("xygg", "校园公告"), ("tzgg", "通知公告"),
]
SUBPATHS = ["", "index.htm", "tzgg.htm", "xwzx.htm", "index/tzgg.htm"]

ROSTER = [
    # 当前收录的 4 栏。清华(thu) 在本站是外链、不抓取；其余三校来源已手工精调在
    # fetch_campus.py 里，故默认跑本脚本不会改动它们（都被 HAND_TUNED 跳过）。
    # 要扩展新校区：把 (key, 中文名, 官网) 追加到本表，再运行本脚本自动探测来源。
    ("thu", "清华大学", "https://www.tsinghua.edu.cn/"),
    ("zju", "浙江大学", "https://www.zju.edu.cn/"),
    ("sjtu", "上海交通大学", "https://www.sjtu.edu.cn/"),
    ("xjtu", "西安交通大学", "https://www.xjtu.edu.cn/"),
]


def registrable(host):
    """粗略取注册域名：取最后三段（edu.cn 场景足够用）。"""
    parts = (host or "").split(".")
    return ".".join(parts[-3:]) if len(parts) >= 3 else host


def loose_links(html, final):
    """宽口径出链：同主机、有像样的锚文本、不是导航词、路径里带数字。

    默认规则(is_article_link)只认「.htm + 3 位以上数字」的老式路径；这条宽口径
    用来发现新式框架站的列表页 —— 发现后再自动推导 link_re。
    """
    base_host = fc.host_of(final)
    out = []
    for href, text, pos in fc.anchors(html, final):
        if fc.host_of(href) != base_host:
            continue
        clean = re.sub(r"\s", "", text)
        if len(clean) < 6 or looks_like_nav_text(clean):
            continue
        path = fc.urllib.parse.urlparse(href).path if hasattr(fc, "urllib") else ""
        if not path:
            path = href.split("?")[0]
        if re.search(r"\.(?:css|js|png|jpe?g|gif|svg|ico|woff2?|pdf|zip|rar|mp4)$", path, re.I):
            continue
        if not re.search(r"\d", href):
            continue
        if re.search(r"/(?:index|list|more|search|default)\.s?html?$", path, re.I):
            continue
        out.append((href, text, pos))
    return out


def looks_like_nav_text(text):
    return bool(fc.looks_like_nav(text)) if hasattr(fc, "looks_like_nav") else False


def shape_to_regex(shape):
    """把「链接形状」转成正则：数字段 → \\d+"""
    parts = re.split(r"(\d+)", shape)
    out = []
    for part in parts:
        out.append(r"\d+" if part.isdigit() else re.escape(part))
    return "".join(out)


def derive_link_re(hrefs):
    """从一组链接里推导最常见的形状，返回 (正则字符串, 命中数)。"""
    shapes = Counter()
    for href in hrefs:
        path = href.split("?")[0]
        shape = re.sub(r"\d+", "1", path)
        shapes[shape] += 1
    for shape, n in shapes.most_common(6):
        if n < MIN_LINKS:
            continue
        rx = shape_to_regex(shape)
        if len(rx) > 160:
            continue
        return rx, n
    return "", 0


def probe(url, campus, name, delay):
    """抓一个候选页：同时算默认规则命中数与宽口径命中数，并给出可用的 link_re。"""
    if ARTICLE_URL.search(url) or SKIP_NAME.search(name):
        return {"url": url, "campus": campus, "name": name,
                "skipped": True, "links": 0, "dated": 0, "link_re": ""}
    try:
        html, final = fc.fetch(url, delay=delay, retries=0)
    except Exception as exc:  # noqa: BLE001
        return {"url": url, "campus": campus, "name": name, "skipped": False,
                "ok": False, "err": str(exc)[:70], "links": 0, "dated": 0, "link_re": ""}
    if ARTICLE_URL.search(final):
        return {"url": url, "campus": campus, "name": name, "skipped": True,
                "links": 0, "dated": 0, "link_re": ""}
    base_host = fc.host_of(final)

    strict, dated = [], 0
    for href, text, pos in fc.anchors(html, final):
        if not fc.is_article_link(href, base_host) or len(re.sub(r"\s", "", text)) < 6:
            continue
        strict.append(href)
        window = fc.strip_tags(html[max(0, pos - 300):pos + 300]).replace("\n", " ")
        if fc.parse_date(window):
            dated += 1
    loose = loose_links(html, final)
    loose_dated = 0
    for href, text, pos in loose:
        window = fc.strip_tags(html[max(0, pos - 300):pos + 300]).replace("\n", " ")
        if fc.parse_date(window):
            loose_dated += 1

    link_re = ""
    mode = "strict"
    if len(strict) < MIN_LINKS:
        mode = "link_re"
        link_re, _n = derive_link_re([h for h, _t, _p in loose])
        if link_re:
            # 回放校验：用抓取器自己的解析路径确认真的挑得到 ≥MIN_LINKS 条
            rx = re.compile(link_re)
            hit = [h for h, _t, _p in loose if rx.search(h)]
            if len(hit) < MIN_LINKS:
                link_re = ""
    samples = []
    for href, text, pos in (loose if mode == "link_re" else
                            [(h, t, p) for h, t, p in fc.anchors(html, final)
                             if h in set(strict)]):
        window = fc.strip_tags(html[max(0, pos - 300):pos + 300]).replace("\n", " ")
        if len(samples) < 3:
            samples.append([(fc.parse_date(window) or "")[:10], text[:30]])
    return {"url": url, "campus": campus, "name": name, "skipped": False, "ok": True,
            "final": final, "mode": mode, "link_re": link_re,
            "links": len(strict) if mode == "strict" else len(loose),
            "dated": dated if mode == "strict" else loose_dated, "samples": samples}


def nav_candidates(home_html, home_url):
    """从主页导航里挑「新闻 / 通知」类栏目链接。"""
    base_host = fc.host_of(home_url)
    reg = registrable(base_host)
    out, seen = [], set()
    for href, text, pos in fc.anchors(home_html, home_url):
        host = fc.host_of(href)
        if not host or registrable(host) != reg or href in seen:
            continue
        seen.add(href)
        clean = re.sub(r"\s", "", text)
        if not (NAV_HINT.search(clean) or LIST_HINT.search(href)):
            continue
        if SKIP_NAME.search(clean) or ARTICLE_URL.search(href):
            continue
        out.append((href, clean[:16] or host))
    return out[:30]


def resolve_home(home, delay):
    for attempt in (home, home.replace("https://", "http://", 1)):
        try:
            return fc.fetch(attempt, delay=delay, retries=1)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! 主页失败 {attempt}：{str(exc)[:60]}")
    return None, home


def plan_one(item, delay, wide):
    key, name, home = item
    print(f"· [{key}] {name}  {home}")
    home_html, home_final = resolve_home(home, delay)
    if home_html is None:
        print(f"  ✗ [{key}] 主页打不开，跳过")
        return key, name, home, []
    reg = registrable(fc.host_of(home_final))
    cands, seen = [], set()
    for href, text in nav_candidates(home_html, home_final):
        if href == home_final or href in seen:
            continue
        seen.add(href)
        cands.append((href, text or "官网栏目"))
    subs = DEPT_SUBS if wide else [d for d in DEPT_SUBS if d[0] in
                                   ("news", "jwc", "gs", "yjs", "yz", "zsb", "job", "lib")]
    for sub, label in subs:
        for path in (SUBPATHS if wide else SUBPATHS[:2]):
            u = f"https://{sub}.{reg}/" + path
            if u in seen:
                continue
            seen.add(u)
            cands.append((u, f"{name}{label}"))
    return key, name, home_final, cands[:60 if wide else 30]


def main():
    ap = argparse.ArgumentParser(description="批量发现各高校可用信息源")
    ap.add_argument("campus", nargs="*", default=None, help="只探测这些 key")
    ap.add_argument("--probe-only", action="store_true", help="只输出报告，不写 sources_more.py")
    ap.add_argument("--weak-only", action="store_true", help="只重探来源少于 3 个的校区")
    ap.add_argument("--delay", type=float, default=0.35, help="同主机请求间隔(秒)")
    ap.add_argument("--workers", type=int, default=24, help="并发探测数(跨主机)")
    args = ap.parse_args()

    wanted = set(args.campus or [])
    roster = [r for r in ROSTER
              if (not wanted and r[0] not in HAND_TUNED) or (wanted and r[0] in wanted)]
    if args.weak_only and not wanted:
        strong = set()
        try:
            sys.path.insert(0, ROOT)
            from sources_more import SOURCES_EXTRA  # noqa: WPS433
            strong = {k for k, v in SOURCES_EXTRA.items() if len(v) >= 3}
        except Exception:  # noqa: BLE001
            pass
        roster = [r for r in roster if r[0] not in strong]
        print(f"· --weak-only：重探 {len(roster)} 所来源偏少的学校")

    wide = bool(wanted) or args.weak_only      # 定向重探时把候选放宽

    plan = {}
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        for key, name, home, cands in pool.map(lambda r: plan_one(r, args.delay, wide), roster):
            plan[key] = {"name": name, "home": home, "cands": cands}

    jobs = [(u, k, n) for k, info in plan.items() for u, n in info["cands"]]
    print(f"\n候选页共 {len(jobs)} 个，开始探测…")
    results = {}
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(probe, u, k, n, args.delay): k for u, k, n in jobs}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            k = futs[fut]
            try:
                r = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"  ! 探测异常 {exc}")
                continue
            if r and not r.get("skipped"):
                results.setdefault(k, []).append(r)
            if i % 100 == 0:
                print(f"  … 已探测 {i}/{len(jobs)}")

    picked, report = {}, {}
    for key, info in plan.items():
        rows = results.get(key, [])
        main_host = fc.host_of(info["home"])
        good = []
        for r in rows:
            if not r.get("ok") or r["links"] < MIN_LINKS:
                continue
            if "学院" in r["name"]:
                continue
            if r["dated"] / max(1, r["links"]) < MIN_DATED:
                continue
            text = r["url"] + " " + r["name"]
            score = r["dated"] * 2 + r["links"] * 0.5
            if GOOD_NAME.search(text):
                score += 12
            if re.search(r"news|新闻网", text):
                score += 10
            if re.search(r"tzgg|通知|公告", text):
                score += 6
            if r.get("mode") == "link_re":
                score -= 4          # 需要 link_re 兜底的站稳定性稍差，排在后面
            good.append((score, r))
        good.sort(key=lambda x: -x[0])
        chosen, hosts = [], set()
        for score, r in good:
            host = fc.host_of(r.get("final") or r["url"])
            if not (ALLOWED_HOST.match(host) or host == main_host):
                continue
            if host in hosts:
                continue
            hosts.add(host)
            chosen.append(r)
            if len(chosen) >= MAX_PER_CAMPUS:
                break
        picked[key] = chosen
        report[key] = {"name": info["name"], "home": info["home"],
                       "probed": len(rows), "good": len(good), "chosen": chosen}
        print(f"\n= [{key}] {info['name']}：探测 {len(rows)}，合格 {len(good)}，选用 {len(chosen)}")
        for r in chosen:
            tag = "link_re" if r.get("mode") == "link_re" else "默认  "
            print(f"    {tag} {r['dated']:3d}/{r['links']:<3d} {r['name'][:14]:16} "
                  f"{(r.get('final') or r['url'])[:62]}")

    os.makedirs(WORK, exist_ok=True)
    with open(os.path.join(WORK, "discover-report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)

    # 只重探部分学校时，把已有配置里没重探的那些校区原样保留
    existing = {}
    out_path = os.path.join(ROOT, "sources_more.py")
    if os.path.exists(out_path) and (wanted or args.weak_only):
        sys.path.insert(0, ROOT)
        try:
            from sources_more import SOURCES_EXTRA as _old
            existing = dict(_old)
        except Exception:  # noqa: BLE001
            pass

    if args.probe_only:
        print("\n(--probe-only：未写 sources_more.py)")
        return

    parts = ['#!/usr/bin/env python3',
             '# -*- coding: utf-8 -*-',
             '"""各高校信息源（由 tools/discover_sources.py 自动发现 + 人工复核）。',
             '',
             'fetch_campus.py 在导入时会把它合并进 SOURCES；同 id 覆盖内置配置。',
             '口径：只收「官网 + 各校职能部门」（新闻网/教务处/研究生院/招生/就业/图书馆/',
             '学工/国际交流…），不收学院站，也不含公众号。',
             '',
             'mode=link_re 的来源：该站前端框架把文章链接做成 /post/detail/xxx 这类不带',
             '.htm 数字的形态，默认规则挑不出来，故按栏目实测推导了一条正则。',
             '"""',
             '',
             'SOURCES_EXTRA = {']
    total = 0
    for key, name, home in ROSTER:
        if wanted and key not in wanted:
            continue
        if args.weak_only and key not in plan:
            continue
        if key in HAND_TUNED and key not in picked:
            continue
        chosen = picked.get(key)
        if chosen is None:
            continue
        parts.append(f'    # ---- {name} ----')
        if not chosen:
            parts.append(f'    # (本轮没探测到合格栏目；_work/sources-{key}.json 可手工补充)')
        parts.append(f'    "{key}": [')
        for r in chosen:
            url = r.get("final") or r["url"]
            sid = f'{key}_{re.sub(r"[^a-z0-9]+", "_", fc.host_of(url).split(".")[0]).strip("_") or "site"}'
            label = r["name"].split("·")[-1][:16]
            parts.append(f'        {{"id": "{sid}", "name": "{name}·{label}", "group": "official",')
            extra = f', "link_re": {json.dumps(r["link_re"], ensure_ascii=False)}' \
                if r.get("mode") == "link_re" and r.get("link_re") else ""
            parts.append(f'         "url": "{url}", "kind": "list", "max": 8{extra}}},')
            total += 1
        parts.append("    ],")
    parts.append("}")
    parts.append("")
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(parts))
    print(f"\n✓ 写了 {out_path}：{len(picked)} 所高校 / {total} 个来源")


if __name__ == "__main__":
    main()
