#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线演练抓取阶段：用本地模拟站点跑真实的 crawl_campus，量出耗时代价。

为什么能离线
------------
抓取阶段的耗时 = 请求次数 × 每次请求的等待。请求次数由**代码逻辑**决定，
等待由 --delay 与网络时延决定。所以只要用本地站点复现同样的页面结构，
就能在零风险、可重复的条件下量出「每轮要发多少请求、程序自身开销多少」，
再叠加上真实网络时延做外推。

模拟站点
--------
按「一个端口 = 一个主机」起若干 HTTP 服务（每个端口一个栏目），页面结构与
真实高校站一致：列表页给出带日期的文章链接，详情页有 h1 标题与正文容器。
参数 --latency-ms 给每个响应加固定延迟，用来模拟真实网络的往返时间。

三个场景
--------
  cold   首次抓取（data/ 里没有这批 URL）→ 每来源 1 个列表页 + N 个详情页
  warm   增量抓取（URL 已在库）→ 每来源只有 1 个列表页   ← 平时的定时更新
  warm+  warm 且 --no-details

用法
----
    python tools/bench_fetch.py                          # 6 来源 × 8 条，0ms 延迟
    python tools/bench_fetch.py --sources 8 --items 8 --latency-ms 300
    python tools/bench_fetch.py --per-campus 37          # 外推到全部校区
"""
import argparse
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

LATENCY_MS = 0
ITEMS_PER_SOURCE = 8
HANG_SEC = 0
TITLES = [
    "关于2026年秋季学期选课工作的通知", "关于组织申报2027年度重点研发计划的通知",
    "关于做好本科生毕业设计（论文）工作的通知", "2027年硕士研究生招生简章发布",
    "关于开展校园安全教育活动的通知", "关于举办第七届青年教师教学竞赛的通知",
    "关于评选2026年度国家奖学金的通知", "2027年推荐免试研究生接收办法公布",
    "关于图书馆国庆假期开放安排的通知", "关于组织学生参加国际交流项目的通知",
]

LIST_TPL = """<!doctype html><html><head><meta charset="utf-8">
<title>{name}</title></head><body>
<div class="nav"><a href="/about.htm">学校概况</a><a href="/contact.htm">联系我们</a></div>
<ul class="news-list">
{rows}
</ul></body></html>"""

ROW_TPL = ('<li><a href="/info/1001/{aid}.htm">{title}</a>'
           '<span class="date">2026-10-{day:02d}</span></li>')

DETAIL_TPL = """<!doctype html><html><head><meta charset="utf-8">
<title>{title}-某大学新闻网</title></head><body>
<h1>{title}</h1>
<div class="info"><span>发布日期: 2026-10-{day:02d}</span><span>浏览次数: {views}</span></div>
<div class="wp_articlecontent"><p>{body}</p>
<p>为做好相关工作，现将有关事项通知如下：请各单位按照要求组织落实，
及时反馈进展。咨询电话：0000-12345678。特此通知。</p></div>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):        # 静音访问日志
        pass

    def do_GET(self):                 # noqa: N802
        if getattr(self.server, "hang", False):
            time.sleep(HANG_SEC)      # 模拟「站点被墙/挂起」：请求一直不回
        if LATENCY_MS:
            time.sleep(LATENCY_MS / 1000.0)
        path = self.path.split("?")[0]
        name = self.server.site_name
        if path.rstrip("/").endswith("list.htm") or path in ("/", ""):
            rows = []
            for n in range(ITEMS_PER_SOURCE):
                # 标题带栏目名：各来源之间不重名，避免被跨来源去重「吃掉」，
                # 这样冷启动的 48 条会全部入库，增量那一轮才有得跳过。
                rows.append(ROW_TPL.format(aid=1000 + n,
                                           title=f"{name}：{TITLES[n % len(TITLES)]}",
                                           day=(28 - n) % 28 + 1))
            body = LIST_TPL.format(name=name, rows="\n".join(rows))
        else:
            m = re.search(r"/(\d+)\.htm$", path)
            aid = int(m.group(1)) if m else 1000
            n = max(0, aid - 1000)
            title = f"{name}：{TITLES[n % len(TITLES)]}"
            body = DETAIL_TPL.format(title=title, day=(28 - n) % 28 + 1, views=100 + n,
                                     body=title + "。" + "本次工作面向全体在校学生和教职员工。" * 6)
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def make_cert(cert_dir):
    """自签证书：让模拟站点走 https，和真实站点一样。

    fetch_campus.py 对 http:// 地址会先试同名 https（很多学校站已上 https），
    这条回退会让每次抓取多一个请求、多一轮限速等待。为了让演练数字干净，
    模拟站也用 https（SSL_CTX 不校验证书，自签即可）。
    """
    key = os.path.join(cert_dir, "bench_key.pem")
    crt = os.path.join(cert_dir, "bench_cert.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key,
                    "-out", crt, "-days", "2", "-nodes", "-subj", "/CN=127.0.0.1"],
                   capture_output=True, check=True)
    return crt, key


def start_sites(count, base_port, crt, key, hang=0):
    servers = []
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    for i in range(count):
        srv = ThreadingHTTPServer(("127.0.0.1", base_port + i), Handler)
        srv.daemon_threads = True     # 挂起场景里线程会一直睡，别拖住进程退出
        srv.site_name = f"模拟栏目 {i + 1}"
        srv.hang = i < hang          # 前 hang 个站点完全不响应，用于测超时代价
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
    return servers


def load_fc():
    import importlib.util
    spec = importlib.util.spec_from_file_location("fc", os.path.join(ROOT, "fetch_campus.py"))
    fc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fc)
    return fc


def reset_state(fc):
    """清掉上一场景的 data/ 与注册表，让每个场景都从「库里没有」开始。"""
    for path in (os.path.join(fc.DATA_DIR, "bench.json"),
                 os.path.join(fc.WORK_DIR, "registry-bench.json"),
                 os.path.join(fc.WORK_DIR, "items-bench.json")):
        if os.path.exists(path):
            os.remove(path)


def run_scenario(label, fc, sources, args, extra_env, timeout=None, reset=False,
                 retries=None):
    if reset:
        reset_state(fc)
    os.environ.update(extra_env)
    fc.RETRY_COUNT = args.retries if retries is None else retries
    fc.DOMAIN_DELAY = 0.0
    if timeout is not None:
        fc.HTTP_TIMEOUT = timeout
    fc.REQUESTS = 0
    t0 = time.time()
    items = fc.crawl_campus("bench", args)
    wall = time.time() - t0
    print(f"  {label:10} 用时 {wall:6.2f}s | 请求 {fc.REQUESTS:4d} 次 | 条目 {len(items):3d}")
    return {"label": label, "wall": wall, "requests": fc.REQUESTS,
            "items": len(items), "crawled": items}


def main():
    global LATENCY_MS, ITEMS_PER_SOURCE, HANG_SEC
    ap = argparse.ArgumentParser(description="离线演练抓取阶段（本地模拟站点）")
    ap.add_argument("--sources", type=int, default=6, help="每校几个来源(=几个模拟主机)")
    ap.add_argument("--items", type=int, default=8, help="每个列表页几篇文章")
    ap.add_argument("--latency-ms", type=int, default=0, help="每个响应的固定延迟(毫秒)")
    ap.add_argument("--delay", type=float, default=1.2, help="同主机请求间隔(秒)")
    ap.add_argument("--workers", type=int, default=4, help="同校并发来源数")
    ap.add_argument("--retries", type=int, default=0,
                    help="失败重试次数（演练默认 0：去掉退避等待，让数字只反映正常抓取）")
    ap.add_argument("--only-hang", action="store_true", help="只跑超时/重试对比那一组")
    ap.add_argument("--hang", type=int, default=0,
                    help="让前 N 个模拟站点完全不响应，用来实测超时/重试的代价")
    ap.add_argument("--hang-sec", type=int, default=60, help="挂起时长(秒)")
    ap.add_argument("--per-campus", type=int, default=0,
                    help="按此校区数外推总耗时（云端矩阵：墙钟≈最慢校区）")
    args = ap.parse_args()

    LATENCY_MS = args.latency_ms
    ITEMS_PER_SOURCE = args.items
    HANG_SEC = args.hang_sec

    fc = load_fc()
    base_port = 39000
    tmp = tempfile.mkdtemp(prefix="bench_fetch_")
    crt, key = make_cert(tmp)
    servers = start_sites(args.sources, base_port, crt, key, hang=args.hang)
    try:
        # 沙箱里用临时目录当项目根，避免污染真实 data/ 与 _work/
        fc.HERE = tmp
        fc.WORK_DIR = os.path.join(tmp, "_work")
        fc.DATA_DIR = os.path.join(tmp, "data")
        os.makedirs(fc.DATA_DIR, exist_ok=True)
        os.makedirs(fc.WORK_DIR, exist_ok=True)

        bench_sources = [
            {"id": f"bench_{i}", "name": f"模拟栏目 {i + 1}", "group": "official",
             "url": f"https://127.0.0.1:{base_port + i}/list.htm", "kind": "list", "max": args.items}
            for i in range(args.sources)
        ]
        fc.SOURCES["bench"] = bench_sources
        fc.WECHAT_ACCOUNTS["bench"] = []

        crawl_args = SimpleNamespace(
            delay=args.delay, workers=args.workers, max_per_source=0,
            no_details=False, no_import=True, no_build=True, retries=args.retries,
            wechat_file=None, max_wechat=0, rotate=False, refresh_known=False,
            import_only=False, domain_delay=0.0)

        print(f"· 模拟站点 {args.sources} 个（127.0.0.1:{base_port}…），"
              f"每站 {args.items} 篇，响应延迟 {LATENCY_MS}ms，同主机间隔 {args.delay}s，"
              f"并发 {args.workers}\n")
        rows = []
        if args.only_hang:
            cold_items = []
        else:
            cold = run_scenario("cold 冷启动", fc, bench_sources, crawl_args, {})
            rows.append(cold)
            cold_items = cold["crawled"]

        if not args.only_hang:
            # warm：把冷启动抓到的条目写进 data/bench.json，再跑一次 → 走增量跳过
            items = json.loads(json.dumps(cold_items))
            with open(os.path.join(fc.DATA_DIR, "bench.json"), "w", encoding="utf-8") as fh:
                json.dump({"campus": "bench", "items": items}, fh, ensure_ascii=False)
            rows.append(run_scenario("warm 增量", fc, bench_sources, crawl_args, {}))

            args_nod = SimpleNamespace(**vars(crawl_args))
            args_nod.no_details = True
            rows.append(run_scenario("warm 不抓正文", fc, bench_sources, args_nod, {}))
    finally:
        for srv in servers:
            srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    if args.hang:
        print(f"\n· 超时/重试代价实测（前 {args.hang} 个站点挂起 {args.hang_sec}s 不响应）")
        old = run_scenario("旧参数 timeout25/retry2", fc, bench_sources, crawl_args, {},
                           timeout=25, reset=True, retries=2)
        new = run_scenario("新参数 timeout12/retry1", fc, bench_sources, crawl_args, {},
                           timeout=12, reset=True, retries=1)
        rows.extend([old, new])
        print(f"  → 同样 {args.sources} 个来源、其中 {args.hang} 个卡死："
              f"{old['wall']:.1f}s 降到 {new['wall']:.1f}s"
              f"（省 {old['wall'] - new['wall']:.1f}s，"
              f"{(old['wall'] - new['wall']) / max(old['wall'], 0.01) * 100:.0f}%）")
        print(f"    最坏值公式：卡死来源 ≈ timeout×(retries+1) × 该来源请求数")

    if args.per_campus:
        cold, warm = rows[0], rows[1]
        par = max(1, args.workers)
        print(f"\n· 外推（云端矩阵：每个校区一个 runner，墙钟≈最慢的那个校区）")
        print(f"    单校区 6 来源 → 冷启动 {cold['wall']:.1f}s / 增量 {warm['wall']:.1f}s")
        print(f"    {args.per_campus} 个校区同构时，墙钟仍≈ {warm['wall']:.1f}s（增量）"
              f" / {cold['wall']:.1f}s（冷启动），与校区数无关")
        print(f"    请求总量：增量 {warm['requests'] * args.per_campus} 次"
              f"，冷启动 {cold['requests'] * args.per_campus} 次")


if __name__ == "__main__":
    main()
