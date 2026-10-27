#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/wap_sid2_capture.py（mitmproxy 插件）的离线测试。

跑法：
    python tests/test_wap_capture.py -v

不依赖 mitmproxy：插件本身不 import mitmproxy，flow 用鸭子类型的假对象。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import wap_sid2_capture as W  # noqa: E402


# ---------------------------------------------------------------- 假 flow
class FakeHeaders(dict):
    def get(self, key, default=None):                    # noqa: A003
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default

    def get_all(self, key):
        return [v for k, v in self.items() if k.lower() == key.lower()]


class FakeReq:
    def __init__(self, url="https://mp.weixin.qq.com/mp/profile_ext?action=getmsg&__biz=MzA5",
                 cookie="", host="mp.weixin.qq.com"):
        self.pretty_url = url
        self.pretty_host = host
        self.headers = FakeHeaders({"cookie": cookie} if cookie else {})


class FakeResp:
    def __init__(self, body='{"ret":0}', status=200, set_cookie=()):
        self.status_code = status
        self._body = body
        self.headers = FakeHeaders({"Set-Cookie": ";".join(set_cookie)} if set_cookie else {})

    def get_text(self, strict=False):                    # noqa: ARG002
        return self._body

    @property
    def cookies(self):
        return {}


class FakeFlow:
    def __init__(self, req, resp):
        self.request, self.response = req, resp


GETMSG_OK = json.dumps({
    "ret": 0,
    "general_msg_list": json.dumps({"list": [{"comm_msg_info": {"type": 49}}, {"comm_msg_info": {"type": 49}}]}),
})


class TestHelpers(unittest.TestCase):
    def test_params_decoded(self):
        url = ("https://mp.weixin.qq.com/mp/profile_ext?action=getmsg&__biz=MzA5%3D%3D"
               "&uin=MTIz%3D&key=k1&pass_ticket=x%2Fy%3D&appmsg_token=931_ab~~&offset=20")
        got = W.parse_capture_url(url)
        self.assertEqual(got["__biz"], "MzA5==")
        self.assertEqual(got["uin"], "MTIz=")
        self.assertEqual(got["pass_ticket"], "x/y=")
        self.assertEqual(got["appmsg_token"], "931_ab~~")
        self.assertEqual(got["action"], "getmsg")
        self.assertNotIn("offset", got)                  # 只留需要的键

    def test_params_empty_on_junk(self):
        self.assertEqual(W.parse_capture_url("not a url"), {})

    def test_set_cookie_attributes_dropped(self):
        got = W.merge_set_cookie([
            "wap_sid2=CJmHq6cBEqq8==; Path=/; Domain=.qq.com; Secure; HttpOnly",
            "pac_uid=0_abc; expires=Wed, 21 Oct 2026 07:28:00 GMT",
            "broken-header",
        ])
        self.assertEqual(got["wap_sid2"], "CJmHq6cBEqq8==")   # 值里的 = 保留
        self.assertEqual(got["pac_uid"], "0_abc")
        self.assertEqual(len(got), 2)

    def test_cookie_header_appends_new_and_keeps_order(self):
        got = W.cookie_header_of("rewardsn=1; wap_sid2=OLD", {"wap_sid2": "NEW", "pac_uid": "9"})
        self.assertEqual(got, "rewardsn=1; wap_sid2=NEW; pac_uid=9")

    def test_cookie_header_without_request_cookie(self):
        self.assertEqual(W.cookie_header_of("", {"wap_sid2": "A=="}), "wap_sid2=A==")

    def test_cookie_value(self):
        self.assertTrue(W.has_wap_sid2("a=1; wap_sid2=X=="))
        self.assertFalse(W.has_wap_sid2("a=1"))
        self.assertEqual(W.cookie_value("a=1; wap_sid2=X==", "wap_sid2"), "X==")
        self.assertEqual(W.cookie_value("a=1", "wap_sid2"), "")

    def test_mask_hides_long_values(self):
        masked = W.mask_cookie("wap_sid2=CJmHq6cBEqq8; pac_uid=0_1")
        self.assertIn("wap_sid2=CJmHq6…", masked)
        self.assertNotIn("CJmHq6cBEqq8", masked)
        self.assertIn("pac_uid=0_1", masked)


class TestVerdict(unittest.TestCase):
    def test_ok_with_two_pushes(self):
        verdict, msg = W.getmsg_verdict(200, GETMSG_OK)
        self.assertEqual(verdict, "ok")
        self.assertIn("2 条", msg)

    def test_no_session(self):
        verdict, msg = W.getmsg_verdict(200, '{"ret":-3,"errmsg":"no session","cookie_count":1}')
        self.assertEqual(verdict, "fail")
        self.assertIn("wap_sid2", msg)

    def test_rate_limited(self):
        self.assertEqual(W.getmsg_verdict(200, '{"base_resp":{"ret":200013}}')[0], "limit")

    def test_verify_wall(self):
        verdict, _ = W.getmsg_verdict(200, "<html><title>验证</title></html>")
        self.assertEqual(verdict, "wall")

    def test_plain_html(self):
        self.assertEqual(W.getmsg_verdict(200, "<html>hi</html>")[0], "html")

    def test_empty_list(self):
        body = json.dumps({"ret": 0, "general_msg_list": json.dumps({"list": []})})
        self.assertEqual(W.getmsg_verdict(200, body)[0], "empty")

    def test_http_error(self):
        self.assertEqual(W.getmsg_verdict(502, "")[0], "fail")


class TestAddon(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addon = W.WapCapture(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _files(self):
        d = Path(self.tmp.name)
        return d / W.COOKIE_NAME, d / W.PARAMS_NAME, d / W.LOG_NAME

    def test_writes_cookie_and_params(self):
        flow = FakeFlow(
            FakeReq(url="https://mp.weixin.qq.com/mp/profile_ext?action=getmsg&__biz=MzA5%3D%3D"
                        "&uin=MTIz&key=k&pass_ticket=t",
                    cookie="rewardsn=1"),
            FakeResp(body=GETMSG_OK, set_cookie=["wap_sid2=NEW==", "pac_uid=0_x"]),
        )
        self.addon.response(flow)
        cookie_f, params_f, log_f = self._files()
        self.assertTrue(cookie_f.exists())
        text = cookie_f.read_text("utf-8")
        self.assertTrue(text.startswith("Cookie: "))
        self.assertIn("wap_sid2=NEW==", text)
        self.assertIn("rewardsn=1", text)                # 原请求的 cookie 保留
        params = json.loads(params_f.read_text("utf-8"))
        self.assertEqual(params["__biz"], "MzA5==")
        self.assertEqual(params["uin"], "MTIz")
        self.assertEqual(params["key"], "k")
        self.assertIn("getmsg", log_f.read_text("utf-8"))

    def test_ignores_other_hosts(self):
        flow = FakeFlow(FakeReq(host="mp.qq.com", url="https://mp.qq.com/x",
                                cookie="wap_sid2=NEW=="), FakeResp())
        self.addon.response(flow)
        self.assertFalse(self._files()[0].exists())

    def test_no_wap_sid2_does_not_write(self):
        flow = FakeFlow(FakeReq(cookie="rewardsn=1"), FakeResp(body=GETMSG_OK))
        self.addon.response(flow)
        self.assertFalse(self._files()[0].exists())

    def test_gate_report_on_getmsg(self):
        self.addon.response(FakeFlow(
            FakeReq(cookie="wap_sid2=X=="),
            FakeResp(body='{"ret":-3,"errmsg":"no session"}'),
        ))
        log = self._files()[2].read_text("utf-8")
        self.assertIn("gate3", log)
        self.assertIn("no session", log)

    def test_same_cookie_logged_once(self):
        """同一条 wap_sid2 会被每个请求重复带上，日志不能刷屏。"""
        flow = FakeFlow(FakeReq(cookie="wap_sid2=SAME=="), FakeResp())
        self.addon.response(flow)
        self.addon.response(flow)
        self.assertEqual(self._files()[2].read_text("utf-8").count("[capture] 已落盘"), 1)

    def test_params_only_keeps_known_keys(self):
        self.addon.response(FakeFlow(FakeReq(cookie="wap_sid2=SAME=="), FakeResp()))
        params = json.loads(self._files()[1].read_text("utf-8"))
        self.assertEqual(params, {"__biz": "MzA5"})      # 只有 URL 上真有的那个键


if __name__ == "__main__":
    unittest.main(verbosity=2)
