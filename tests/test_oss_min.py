#!/usr/bin/env python3
"""oss_min 的自检。

三层验证：
  ① 规则层：规范头按小写名升序、冒号无空格、无 x-oss-* 时为空
  ② 对拍层：与官方 oss2 SDK 的签名实现逐字节比对（最硬的证据）
  ③ 端到端：起一个本地 mock 服务，验证 PUT/GET 真的能跑通

关于「官方文档示例」的说明
--------------------------
阿里云文档「在 Header 中包含签名（V1）」的示例里，两个 x-oss-meta-* 头是
`magic` 排在 `author` 前面，**与它自己写的"按名称字典序升序"规则矛盾**。
查官方 SDK 源码 oss2/auth.py 的 `__get_headers_string`：
    canon_headers.sort(key=lambda x: x[0])      # x[0] 是小写头名 → author 在前
可见**是文档示例写错了**，实际服务按小写头名升序。本实现与 SDK 一致，
因此不复现文档示例的顺序，改为与 SDK 对拍。

运行：python deploy/serverless/test_oss_min.py
"""

import base64
import hashlib
import hmac
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                              # tests/ 在根下一层
SERVERLESS = os.path.join(ROOT, "deploy", "serverless")   # oss_min.py 所在
sys.path.insert(0, SERVERLESS)
import oss_min  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def load_official_sdk():
    """尽力加载官方 oss2（本机已装或打过包都行）；加载不到就跳过对拍层。"""
    try:
        import oss2.auth as auth  # noqa: F401
        return auth
    except Exception:  # noqa: BLE001
        pass
    for cand in (os.path.join(SERVERLESS, "dist", "_stage"),
                 os.path.join(SERVERLESS, "dist", "_stage", "site-packages")):
        if os.path.isdir(cand):
            sys.path.insert(0, cand)
            try:
                import oss2.auth as auth  # noqa: F401
                return auth
            except Exception:  # noqa: BLE001
                sys.path.pop(0)
    return None


class Stub:
    """喂给 SDK 的假请求对象。"""

    def __init__(self, method, headers, params=None):
        self.method = method
        self.headers = headers
        self.params = params or {}

    def __repr__(self):
        return f"<Stub {self.method} {self.headers}>"


def sdk_string_to_sign(auth, method, headers, bucket, key):
    """调官方 SDK 的私有方法，拿到它的待签字符串。"""
    obj = object.__new__(auth.ProviderAuth)          # 绕过 __init__，不碰凭证
    return obj._ProviderAuth__get_string_to_sign(Stub(method, headers), bucket, key)


def test_rules():
    print("① 规范头规则")
    check("无 x-oss-* 时为空",
          oss_min.canonicalized_oss_headers({"Date": "x", "Content-Type": "y"}) == "")
    # 故意乱序 + 混合大小写传入，期望输出：小写、升序、冒号无空格、每条带 \n
    got = oss_min.canonicalized_oss_headers(
        {"X-OSS-Meta-Magic": "abracadabra", "x-oss-meta-author": "alice"})
    check("小写 + 按名升序 + 冒号无空格",
          got == "x-oss-meta-author:alice\nx-oss-meta-magic:abracadabra\n", repr(got))
    check("单个规范头也有结尾换行",
          oss_min.canonicalized_oss_headers({"x-oss-meta-a": "1"}) == "x-oss-meta-a:1\n")


def test_against_sdk(auth):
    print("② 与官方 oss2 SDK 对拍")
    if auth is None:
        print("  [SKIP] 本机没装 oss2，跳过对拍（想启用：pip install oss2 后重跑本脚本）")
        return

    cases = [
        ("PUT + MD5 + CT + 两个规范头",
         "PUT", {"content-md5": "eB5eJF1ptWaXm4bijSPyxw==", "content-type": "text/html",
                 "date": "Thu, 17 Nov 2005 18:49:58 GMT",
                 "x-oss-meta-author": "foo@bar.com", "x-oss-magic": "abracadabra"}),
        ("GET 无附加头", "GET", {"date": "Wed, 28 Dec 2022 09:56:32 GMT"}),
        ("STS 场景", "PUT", {"content-type": "application/json",
                            "date": "Wed, 28 Dec 2022 09:56:32 GMT",
                            "x-oss-security-token": "CAIS_TOKEN"}),
        ("混合大小写 + 乱序传入", "PUT",
         {"X-OSS-Meta-Z": "z", "x-oss-meta-A": "a", "Date": "D", "x-oss-meta-m": "m"}),
    ]
    for label, method, headers in cases:
        bucket, key = "examplebucket", "dir/obj.json"
        mine = oss_min.string_to_sign(
            method, headers.get("content-md5"), headers.get("content-type"),
            headers.get("date"), headers, oss_min.OSSClient._resource(
                type("X", (), {"bucket": bucket})(), key))
        theirs = sdk_string_to_sign(auth, method, headers, bucket, key)
        check(f"待签字符串一致：{label}", mine == theirs,
              f"\n      本实现={mine!r}\n      官方  ={theirs!r}")
        # 再比最终签名（AK/Secret 任意，只要两边算法一致）
        secret = "yourAccessKeySecret"
        sig_mine = base64.b64encode(hmac.new(secret.encode(), mine.encode(),
                                             hashlib.sha1).digest()).decode()
        sig_theirs = base64.b64encode(hmac.new(secret.encode(), theirs.encode(),
                                               hashlib.sha1).digest()).decode()
        check(f"签名一致：{label}", sig_mine == sig_theirs, f"{sig_mine} vs {sig_theirs}")


class MockOSS(BaseHTTPRequestHandler):
    store, seen = {}, {}

    def _do(self, verb):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        MockOSS.seen[verb] = {k.lower(): v for k, v in dict(self.headers).items()}
        if verb == "PUT":
            MockOSS.store[self.path] = body
            self.send_response(200)
            self.end_headers()
        elif self.path in MockOSS.store:
            self.send_response(200)
            self.send_header("Content-Length", str(len(MockOSS.store[self.path])))
            self.end_headers()
            self.wfile.write(MockOSS.store[self.path])
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


def test_end_to_end():
    print("③ 本地 mock 服务端到端")
    srv = HTTPServer(("127.0.0.1", 0), MockOSS)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    cli = oss_min.OSSClient("examplebucket", "oss-cn-hangzhou.aliyuncs.com",
                            access_key_id="LTAI_test", access_key_secret="secret_test",
                            base_url=base)
    cli.put("data/thu.json", b'{"ok":1}', content_type="application/json")
    check("写入后能读回相同字节", cli.get("data/thu.json") == b'{"ok":1}')
    check("不存在时返回 None", cli.get("nope/x.json") is None)
    check("对象键含子目录能正确落到路径",
          "/data/thu.json" in MockOSS.store)

    hdr = MockOSS.seen.get("PUT", {})
    auth = hdr.get("authorization", "")
    check("Authorization 形如 'OSS AK:base64'",
          auth.startswith("OSS LTAI_test:") and len(auth.split(":")[-1]) > 20, auth)
    check("Date 是 GMT 格式", "GMT" in hdr.get("date", ""), hdr.get("date", ""))
    check("Content-Type 透传",
          hdr.get("content-type") == "application/json", hdr.get("content-type", ""))

    MockOSS.seen.clear()
    cli2 = oss_min.OSSClient("examplebucket", "oss-cn-hangzhou.aliyuncs.com",
                             access_key_id="STS.ak", access_key_secret="sk",
                             security_token="TOKEN123", base_url=base)
    cli2.put("a/b.txt", b"x", content_type="text/plain")
    check("STS token 随请求发送",
          MockOSS.seen.get("PUT", {}).get("x-oss-security-token") == "TOKEN123")
    check("STS token 进入待签字符串（改它则签名变）",
          oss_min.string_to_sign("PUT", None, "text/plain", "D",
                                 {"x-oss-security-token": "T"}, "/b/k")
          != oss_min.string_to_sign("PUT", None, "text/plain", "D", {}, "/b/k"))
    srv.shutdown()


def main():
    print("oss_min 自检\n" + "=" * 56)
    test_rules()
    test_against_sdk(load_official_sdk())
    test_end_to_end()
    print("=" * 56)
    if FAILED:
        print(f"✗ {len(FAILED)} 项失败：" + "; ".join(FAILED))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
