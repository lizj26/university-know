#!/usr/bin/env python3
"""handler 的本地集成测试：起一个 mock OSS，跑一遍完整入口流程。

用 SKIP_REFRESH=1 + SKIP_CAMPUS=1 跳过真实抓取，从而在 1 秒内验证
「准备可写目录 → 拉基线 → 跑 cloud_update → 回写对象存储」这条链路本身。

运行：python deploy/serverless/test_handler.py
"""

import importlib.util
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                          # tests/ 在根下一层
SERVERLESS = os.path.join(ROOT, "deploy", "serverless")   # handler.py / oss_min.py 所在

sys.path.insert(0, SERVERLESS)

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


class MockOSS(BaseHTTPRequestHandler):
    store, requests = {}, []

    def _do(self, verb):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        MockOSS.requests.append((verb, self.path))
        if verb == "PUT":
            MockOSS.store[self.path] = body
            self.send_response(200)
            self.end_headers()
        elif self.path in MockOSS.store:
            payload = MockOSS.store[self.path]
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"<Error><Code>NoSuchKey</Code></Error>")

    def do_PUT(self):
        self._do("PUT")

    def do_GET(self):
        self._do("GET")

    def log_message(self, *a):
        pass


def load_handler(work_dir):
    os.environ["WORK_DIR"] = work_dir           # 必须在 import 前设好（模块级常量）
    spec = importlib.util.spec_from_file_location(
        "thu_handler", os.path.join(SERVERLESS, "handler.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["thu_handler"] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    print("handler 本地集成测试\n" + "=" * 56)
    srv = HTTPServer(("127.0.0.1", 0), MockOSS)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    work = tempfile.mkdtemp(prefix="thu-fc-test-")
    os.chdir(ROOT)                              # find_code_dir 依赖 cwd 找到 cloud_update.py
    os.environ.update({
        "BUCKET": "examplebucket",
        "ENDPOINT": "oss-cn-hangzhou.aliyuncs.com",
        "PREFIX": "thu-know",
        "STORAGE_BACKEND": "oss",
        "OSS_BASE_URL": f"http://127.0.0.1:{port}",   # 走 mock 服务
        "OSS_AK": "AK_TEST", "OSS_SK": "SK_TEST",
        "SKIP_REFRESH": "1", "SKIP_CAMPUS": "1",      # 跳过真实抓取，只验证链路
    })

    try:
        mod = load_handler(work)
        check("能定位到代码目录", os.path.isfile(os.path.join(mod.find_code_dir(), "cloud_update.py")))

        result = mod.handler({}, None)
        print("  handler 返回：", result)
        check("首次运行 ok=True", result.get("ok") is True, str(result))
        up = result.get("uploaded", [])
        # 本轮 SKIP_REFRESH=1 不重建页面，工作目录里没有 data/shard/ 分片，
        # 所以能回写的只有三校 data/*.json。按「工作目录里真实存在的文件」核对：
        # uploaded 里除真实文件外还有「根目录镜像」的汇总行（data/shard/*.json (…6 份)），
        # 以及本轮根本不存在的分片（会被跳过），两者都要排除。
        def is_data_file(u):
            p = u.split(" (")[0]
            return p.startswith("data/") and p.endswith(".json") and "*" not in p
        expected_data = len([f for f in os.listdir(os.path.join(ROOT, "data"))
                             if f.endswith(".json")])
        check(f"回写了 {expected_data} 个 data/*.json",
              sum(1 for u in up if is_data_file(u)) == expected_data, str(up))
        check("回写了 index.html", any(u.startswith("index.html") for u in up), str(up))
        # 分片存在时必须一并回写（否则线上点开详情取不到正文）。
        # 这里额外验证「分片清单已挂进 SYNC_UP」，保证真跑构建时不会漏传。
        check("分片已在回写清单里（data/shard/…）",
              any(rel.startswith("data/shard/") for rel in mod.SYNC_UP),
              str([r for r in mod.SYNC_UP if r.startswith("data/shard/")]))

        keys = {k.lstrip("/") for k in MockOSS.store}
        check("桶里出现 thu-know/index.html",
              "thu-know/index.html" in keys, str(sorted(keys))[:200])
        check("桶里出现 thu-know/data/xjtu.json",
              "thu-know/data/xjtu.json" in keys, str(sorted(keys))[:200])
        html = MockOSS.store.get("/thu-know/index.html", b"")[:4000].lower()
        check("index.html 内容确为 HTML 页面", b"<html" in html or b"<!doctype" in html,
              repr(html[:60]))

        # 第二轮：桶里已有数据，应能"拉回基线"（验证无状态补偿）
        MockOSS.requests.clear()
        result2 = mod.handler({}, None)
        pulls = [p for m, p in MockOSS.requests if m == "GET" and p.startswith("/thu-know/data/")]
        print("  第二轮拉取基线次数：", len(pulls))
        check("第二轮从桶里拉回了基线数据", len(pulls) >= expected_data, str(pulls))
        check("第二轮仍 ok", result2.get("ok") is True, str(result2))

        # 第三轮：WITH_WECHAT=1 时应把 --with-wechat 传给 cloud_update.py。
        # 这一步仍然跳过真实抓取（SKIP_REFRESH/SKIP_CAMPUS 保持 1），
        # 但 fetch_wechat.py 会真的被调用 —— 它会先去 _work/ 找 cookies，
        # 函数包/工作目录里都没有，于是打印「没有 cookies」并降级，不会联网。
        os.environ["WITH_WECHAT"] = "1"
        try:
            result3 = mod.handler({}, None)
            check("WITH_WECHAT=1 时仍返回 ok", result3.get("ok") is True, str(result3))
            # 从桶里取回的 run.log 应能看到公众号这一步真的跑了
            log_blob = MockOSS.store.get("/thu-know/_work/run.log", b"").decode("utf-8", "replace")
            check("run.log 里出现 fetch_wechat.py",
                  "fetch_wechat.py" in log_blob, repr(log_blob[-200:]))
        finally:
            os.environ.pop("WITH_WECHAT", None)
    finally:
        srv.shutdown()
        shutil.rmtree(work, ignore_errors=True)

    print("=" * 56)
    if FAILED:
        print(f"✗ {len(FAILED)} 项失败：" + "; ".join(FAILED))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
