#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mitmproxy 插件：从微信客户端流量里捞出 `wap_sid2` 与 profile_ext 参数。

为什么需要它
------------
`mp/profile_ext?action=getmsg` 是唯一能"枚举某号全部已发布文章"的接口（见文档
10.15），它不验签、不要后台 token，**只缺一个真实微信客户端的 cookie**。这个插件
就是"在旁边把那个 cookie 记下来"的那一半，另一半是 `wxmp_spider.py history`。

用法
----
    # 安卓副号：手机装 WireGuard，扫 mitmweb 页面上的二维码
    mitmweb -s tools/wap_sid2_capture.py --mode wireguard

    # PC 微信：本机直接抓，不用配系统代理（Windows/macOS 可用）
    mitmdump -s tools/wap_sid2_capture.py --mode local

    # 自定义落盘目录（默认 _work/wechat）
    WAP_CAPTURE_DIR=_work/wechat mitmdump -s tools/wap_sid2_capture.py --mode local

落盘（默认 `_work/wechat/`）
---------------------------
    wxmp-wap-cookies.txt   一行 `Cookie: ...`（含 wap_sid2），history 默认读这个
    wxmp-wap-params.json   uin / key / pass_ticket / appmsg_token / __biz
    wap-capture.log        时间线（掩码后的凭据 + 每个闸门的判定），排查用

设计约束
--------
* **不 import mitmproxy**：addon 只依赖 flow 对象的方法（鸭子类型），这样单元测试
  可以在没装 mitmproxy 的环境里 import 本文件。
* **只落盘、不改包**：这是抓凭据的旁路，不做任何请求/响应改写。
* **不打印明文**：终端只出现掩码后的前 6 个字符，避免截图/日志泄露会话。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

DEFAULT_DIR = "_work/wechat"
COOKIE_NAME = "wxmp-wap-cookies.txt"
PARAMS_NAME = "wxmp-wap-params.json"
LOG_NAME = "wap-capture.log"
PARAM_KEYS = ("uin", "key", "pass_ticket", "appmsg_token", "__biz")
WECHAT_HOST = "mp.weixin.qq.com"


# --------------------------------------------------------------------------- #
# 纯函数区（离线可测）
# --------------------------------------------------------------------------- #
def parse_capture_url(url: str) -> dict[str, str]:
    """从请求 URL 里挑出 getmsg 需要的那几个参数（值已 URL 解码）。"""
    try:
        qs = parse_qs(urlparse(url).query)
    except ValueError:
        return {}
    out: dict[str, str] = {}
    for k in PARAM_KEYS + ("action",):
        val = (qs.get(k) or [""])[0]
        if val:
            out[k] = val
    return out


def merge_set_cookie(headers: Iterable[str]) -> dict[str, str]:
    """把若干条 `Set-Cookie` 头解析成 {name: value}（只取第一段，忽略属性）。"""
    out: dict[str, str] = {}
    for raw in headers:
        if not raw:
            continue
        first = raw.split(";", 1)[0]
        if "=" not in first:
            continue
        k, _, v = first.partition("=")
        k, v = k.strip(), v.strip()
        if k:
            out[k] = v
    return out


def cookie_header_of(request_cookie: str, fresh: dict[str, str] | None = None) -> str:
    """合成一行可直接重放的 Cookie。

    以**请求里真实的 Cookie 头**为底（顺序、字段都是微信自己写的，最不容易被
    判环境异常），再把响应新下的 cookie（比如首次打开文章才发的 `wap_sid2`）
    补进去；同名以响应值为准（更新）。
    """
    fresh = fresh or {}
    pairs: list[tuple[str, str]] = []
    for chunk in (request_cookie or "").split(";"):
        chunk = chunk.strip()
        if "=" not in chunk:
            continue
        k, _, v = chunk.partition("=")
        k = k.strip()
        if k:
            pairs.append((k, v.strip()))
    names = [k for k, _ in pairs]
    for k, v in fresh.items():
        if k in names:
            pairs = [(pk, v if pk == k else pv) for pk, pv in pairs]
        else:
            pairs.append((k, v))
            names.append(k)
    return "; ".join(f"{k}={v}" for k, v in pairs)


def has_wap_sid2(cookie: str) -> bool:
    return "wap_sid2=" in (cookie or "")


def cookie_value(cookie: str, name: str) -> str:
    """取某个 cookie 的值；没有返回空串。"""
    for chunk in (cookie or "").split(";"):
        k, _, v = chunk.strip().partition("=")
        if k.strip() == name:
            return v.strip()
    return ""


def mask_cookie(cookie: str, keep: int = 6) -> str:
    """展示用：长值只留前几位，短值原样。"""
    shown = []
    for chunk in (cookie or "").split(";"):
        chunk = chunk.strip()
        if "=" not in chunk:
            continue
        k, _, v = chunk.partition("=")
        shown.append(f"{k}={v[:keep]}…" if len(v) > keep else f"{k}={v}")
    return "; ".join(shown)


def getmsg_verdict(status: int, body: str) -> tuple[str, str]:
    """`profile_ext?action=getmsg` 的响应 → (闸门判定, 人话)。

    这是整条 A 路线最关键的判据：前三个闸门都可能"看起来成功"，只有这里返回
    ret=0 + general_msg_list 才算真的能枚举。
    """
    if status != 200:
        return "fail", f"HTTP {status}"
    text = (body or "").strip()
    if not text.startswith("{"):
        if "<title>验证</title>" in text or "环境异常" in text:
            return "wall", "撞上『验证』环境墙：cookie/UA/出口被判异常，停一会儿再试"
        return "html", "返回 HTML 而不是 JSON：多半没带微信 UA，或 cookie 是游客态"
    try:
        data = json.loads(text)
    except ValueError:
        return "html", "响应不是合法 JSON"
    br = data.get("base_resp") or {}
    ret = br.get("ret", data.get("ret"))
    if ret in (0, None):
        try:
            inner = json.loads(data.get("general_msg_list") or "{}")
            n = len(inner.get("list") or [])
        except (TypeError, ValueError):
            n = 0
        if n:
            return "ok", f"ret=0，本页 {n} 条群发 —— 闸门 3 通过，可以跑 history"
        return "empty", "ret=0 但列表为空：该号没有可枚举内容，或 __biz 不对"
    if ret == -3:
        return "fail", "ret=-3 no session：cookie 里没有真 wap_sid2，或 uin/key 不配套"
    if ret == 200013:
        return "limit", "ret=200013：被限流/能力收紧（换号、降频或换时间）"
    return "fail", f"ret={ret} {data.get('errmsg') or br.get('err_msg') or ''}".strip()


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #
class WapCapture:
    def __init__(self, out_dir: str | Path = DEFAULT_DIR) -> None:
        self.dir = Path(out_dir)
        self.params: dict[str, str] = {}
        self.saved_cookie = ""
        self.wrote = False

    # ---- mitmproxy 钩子 --------------------------------------------------- #
    def response(self, flow) -> None:            # noqa: ANN001 - mitmproxy flow
        req = flow.request
        if req.pretty_host != WECHAT_HOST:
            return

        url = req.pretty_url
        query = parse_capture_url(url)
        set_cookie: dict[str, str] = {}
        try:
            set_cookie = merge_set_cookie(flow.response.headers.get_all("Set-Cookie"))
        except Exception:                        # noqa: BLE001 - 老版本 API 差异
            pass

        req_cookie = req.headers.get("cookie", "") or ""
        cookie = cookie_header_of(req_cookie, set_cookie)
        if has_wap_sid2(cookie):
            self._remember(query)
            self._save(cookie)

        if "/mp/profile_ext" in url:
            self._report_gate(flow, query)

    # ---- 内部 ------------------------------------------------------------- #
    def _remember(self, query: dict[str, str]) -> None:
        for k in PARAM_KEYS:
            if query.get(k):
                self.params[k] = query[k]

    def _save(self, cookie: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        sid2 = cookie_value(cookie, "wap_sid2")
        changed = sid2 != self.saved_cookie and bool(sid2)
        self.saved_cookie = sid2
        path = self.dir / COOKIE_NAME
        path.write_text("Cookie: " + cookie + "\n", "utf-8")
        (self.dir / PARAMS_NAME).write_text(
            json.dumps(self.params, ensure_ascii=False, indent=2), "utf-8")
        # 别刷屏：同一份 wap_sid2 只提示一次（每条请求都会带同一串）
        if changed:
            self.wrote = True
            self._log(f"[capture] 已落盘 {path}（{len(cookie)} 字节）")
            self._log(f"[capture] {mask_cookie(cookie)}")
            print(f"\n[✓] 抓到含 wap_sid2 的 cookie → {path}", flush=True)
            print(f"    {mask_cookie(cookie)}", flush=True)
            print("    下一步：python tools/wxmp_spider.py history --name <公众号名> -n 5\n",
                  flush=True)

    def _report_gate(self, flow, query: dict[str, str]) -> None:   # noqa: ANN001
        action = query.get("action", "")
        try:
            status = flow.response.status_code
            body = flow.response.get_text(strict=False) or ""
        except Exception:                        # noqa: BLE001
            return
        if action == "getmsg":
            verdict, msg = getmsg_verdict(status, body)
            icon = {"ok": "✅", "empty": "⚠️", "limit": "⛔"}.get(verdict, "❌")
            line = f"{icon} getmsg → {msg}"
            print(f"[gate3] {line}", flush=True)
            self._log(f"[gate3] {line}")
        elif action == "home":
            ok = status == 200 and "wap_sid2" in (flow.request.headers.get("cookie", "") or "")
            line = ("home 页面已拿到，cookie 里有 wap_sid2 → 可以在列表里往下滚两屏触发 getmsg"
                    if ok else "home 页面没有 wap_sid2：这是游客态，确认微信已登录（副号）")
            print(f"[gate2] {line}", flush=True)
            self._log(f"[gate2] {line}")

    def _log(self, line: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H:%M:%S")
        with (self.dir / LOG_NAME).open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {line}\n")


def start(out_dir: str = DEFAULT_DIR) -> WapCapture:
    """addon 工厂：目录取 WAP_CAPTURE_DIR 环境变量，默认 `_work/wechat`。"""
    return WapCapture(out_dir)


addons = [start(os.environ.get("WAP_CAPTURE_DIR", DEFAULT_DIR))]


if __name__ == "__main__":
    # 自检：不启动代理，只验证纯函数（`python tools/wap_sid2_capture.py --selftest`）
    import sys

    if "--selftest" in sys.argv:
        demo = "https://mp.weixin.qq.com/mp/profile_ext?action=getmsg&__biz=MzA5%3D%3D" \
               "&uin=MTIz%3D&key=abc&pass_ticket=x%2Fy%3D&appmsg_token=931_abc~~&offset=20"
        print("params:", parse_capture_url(demo))
        merged = cookie_header_of("rewardsn=1; wap_sid2=OLD", {"wap_sid2": "NEW", "pac_uid": "9"})
        print("cookie:", mask_cookie(merged), "| has_sid2:", has_wap_sid2(merged))
        print("verdict:", getmsg_verdict(200, '{"ret":0,"general_msg_list":"{\\"list\\":[{}]}"}'))
    else:
        print("把它交给 mitmproxy 用：mitmdump -s tools/wap_sid2_capture.py --mode local")
        print("自检：python tools/wap_sid2_capture.py --selftest")
