#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫码登录状态机的**离线受控演练**：用一个本地假 mp 服务把 7 步全跑一遍。

为什么要这个测试
----------------
真实登录要手机扫码，只能验证到"二维码 + 轮询"。而第 5–7 步（bizlogin action=login
→ 从 redirect_url 抠 token → 跟首页 → 落盘会话）恰恰是最容易写错、又只能在你
恰好扫码那一刻才暴露的部分：表单字段名、redirect_url 是相对还是绝对、cookie 有没有
存下来，错一个就是"扫了没反应"。

所以这里用 http.server 起一个假后台，把 7 步按 pkucli 记录的响应形状复刻一遍，
真实走完整条 `login()` 代码路径（只把 BASE 指向 127.0.0.1）。

跑法：
    python tests/test_login_flow.py -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import wxmp_spider as S  # noqa: E402

# 1x1 PNG，够 render_qr_terminal 打开失败后优雅降级
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d4944415478da63f8ffff3f0005fe02fea735d1e1"
    "0000000049454e44ae426082"
)


class FakeMp:
    """按顺序应答的假公众号后台。"""

    def __init__(self, qr_statuses: list[int] | None = None, login_ret: int = 0):
        self.qr_statuses = list(qr_statuses or [0, 0, 4, 1])
        self.login_ret = login_ret
        self.calls: list[tuple[str, str]] = []      # (method, path?query)
        self.bodies: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]

    # ---------------------------------------------------------------- 路由
    def _handler(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):        # 安静
                pass

            def _send(self, body: bytes, ctype="application/json", cookies=(), status=200):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for c in cookies:
                    self.send_header("Set-Cookie", c)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, cookies=()):
                self._send(json.dumps(obj).encode(), "application/json", cookies)

            def do_GET(self):                                  # noqa: N802
                u = urlparse(self.path)
                with outer._lock:
                    outer.calls.append(("GET", u.path + ("?" + u.query if u.query else "")))

                if u.path == "/":
                    return self._send(b"<html>home</html>", "text/html", ("uuid=fake-uuid; path=/",))
                if u.path == "/cgi-bin/scanloginqrcode":
                    action = parse_qs(u.query).get("action", [""])[0]
                    if action == "getqrcode":
                        return self._send(TINY_PNG, "image/jpeg")
                    if action == "ask":
                        with outer._lock:
                            st = outer.qr_statuses.pop(0) if outer.qr_statuses else 1
                        return self._json({"base_resp": {"ret": 0, "err_msg": "ok"}, "status": st})
                    return self._json({"base_resp": {"ret": 1, "err_msg": "bad action"}})
                if u.path == "/cgi-bin/home":
                    qs = parse_qs(u.query)
                    outer.bodies["home_token"] = qs.get("token", [""])[0]
                    return self._send(b"<html>dashboard</html>", "text/html")
                return self._json({"base_resp": {"ret": 404, "err_msg": "not found"}})

            def do_POST(self):                                 # noqa: N802
                u = urlparse(self.path)
                qs = parse_qs(u.query)
                n = int(self.headers.get("Content-Length") or 0)
                form = parse_qs(self.rfile.read(n).decode("utf-8", "replace"), keep_blank_values=True)
                action = qs.get("action", [""])[0]
                with outer._lock:
                    outer.calls.append(("POST", f"{u.path}?action={action}"))
                    outer.bodies[action] = {k: v[0] for k, v in form.items()}

                if action == "startlogin":
                    return self._json(
                        {"base_resp": {"ret": 0, "err_msg": "ok"}},
                        ("uuid=fake-uuid; path=/",),
                    )
                if action == "login":
                    if outer.login_ret != 0:
                        return self._json({"base_resp": {"ret": outer.login_ret, "err_msg": "denied"}})
                    return self._json(
                        {
                            "base_resp": {"ret": 0, "err_msg": "ok"},
                            "redirect_url": "/cgi-bin/home?t=home/index&lang=zh_CN&token=999888",
                        },
                        (
                            "slave_sid=FAKE-SID; path=/",
                            "slave_user=gh_fake; path=/",
                            "data_ticket=FAKE-TICKET; path=/",
                            "bizuin=3699431467; path=/",
                        ),
                    )
                return self._json({"base_resp": {"ret": 1, "err_msg": "unknown"}})

        return H

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class LoginFlowTest(unittest.TestCase):
    def setUp(self):
        self._orig_base = S.BASE
        self.tmp = tempfile.TemporaryDirectory()
        self.store = S.Store(self.tmp.name)

    def tearDown(self):
        S.BASE = self._orig_base
        self.tmp.cleanup()

    def _run(self, **kw):
        with FakeMp(**kw) as fake:
            S.BASE = f"http://127.0.0.1:{fake.port}"
            result = S.login(self.store, qr_only=False)
            return fake, result

    # ------------------------------------------------------------------
    def test_full_flow_saves_session_and_cookies(self):
        fake, session = self._run()

        self.assertIsNotNone(session)
        self.assertEqual(session["token"], "999888")           # redirect_url 里抠出来的
        self.assertEqual(session["bizuin"], "3699431467")      # 从 Set-Cookie 读的
        self.assertEqual(len(session["fingerprint"]), 32)

        names = {c["name"] for c in self.store.load_cookies()}
        # 鉴权 cookie 必须全在（假服务还会同时下发 uuid，故用子集断言）
        self.assertTrue({"slave_sid", "slave_user", "data_ticket", "bizuin"} <= names, names)

        # 顺序：首页 → startlogin → qrcode → ask… → login → home
        methods = [m for m, _ in fake.calls]
        self.assertEqual(methods[0], "GET")
        self.assertIn(("POST", "/cgi-bin/bizlogin?action=startlogin"), fake.calls)
        self.assertIn(("POST", "/cgi-bin/bizlogin?action=login"), fake.calls)
        asks = [c for c in fake.calls if "action=ask" in c[1]]
        self.assertEqual(len(asks), 4)                          # 0,0,4,1 → 第四次跳出
        self.assertEqual(fake.bodies["home_token"], "999888")   # 第 7 步真的带了 token

    def test_form_fields_match_documented_protocol(self):
        fake, _ = self._run()
        start = fake.bodies["startlogin"]
        for k in ("userlang", "redirect_url", "login_type", "sessionid", "fingerprint", "token"):
            self.assertIn(k, start, f"startlogin 缺字段 {k}")
        self.assertEqual(start["login_type"], "3")             # 扫码登录
        self.assertEqual(start["ajax"], "1")
        self.assertTrue(start["sessionid"].startswith("177"))   # 毫秒时间戳 + 随机尾数

        login = fake.bodies["login"]
        for k in ("cookie_forbidden", "cookie_cleaned", "plugin_used", "plugin_used", "fingerprint"):
            self.assertIn(k, login, f"login 缺字段 {k}")
        self.assertEqual(login["fingerprint"], start["fingerprint"])   # 同一会话指纹不变

    def test_qr_image_saved(self):
        self._run()
        qr = self.store.root / "login-qrcode.jpg"
        self.assertTrue(qr.exists())
        self.assertGreater(qr.stat().st_size, 10)

    def test_login_denied_raises(self):
        with self.assertRaises(S.WxmpError) as cm:
            self._run(login_ret=200003)
        self.assertEqual(cm.exception.code, 200003)
        self.assertIsNone(self.store.load_session())            # 失败不落盘

    def test_expired_qr_raises(self):
        with self.assertRaises(S.WxmpError) as cm:
            self._run(qr_statuses=[0, 5])
        self.assertIn("二维码已失效", str(cm.exception))

    def test_qr_only_stops_before_polling(self):
        with FakeMp() as fake:
            S.BASE = f"http://127.0.0.1:{fake.port}"
            self.assertIsNone(S.login(self.store, qr_only=True))
            self.assertFalse(any("action=ask" in c[1] for c in fake.calls))
            self.assertFalse(any("action=login" in c[1] for c in fake.calls))

    def test_old_session_overwritten(self):
        self.store.save_session("OLD", "0" * 32, None)
        _, session = self._run()
        self.assertEqual(session["token"], "999888")


if __name__ == "__main__":
    unittest.main(verbosity=2)
