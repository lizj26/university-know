#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测一个公众号后台登录态还能用哪些「读别人文章」的接口。

背景
----
社群反馈：个人主体 / 未认证订阅号的编辑器「超链接」面板里，
「选择其他公众号 → 搜号 → 列文章」入口被收掉了。若属实，
`searchbiz` + `appmsgpublish` 这条路线在个人号上就整条失效。

但「面板上没有入口」和「接口无权访问」需要分开验证：前端藏起入口、
后端仍放行的情况并不罕见。本脚本只做只读探测，各打 1 次请求，
用返回的 `ret` 判定到底是「没权限」还是「只是界面没了」。

侦察的三个接口
--------------
1. `searchbiz`      —— 超链接面板「选择其他公众号」的搜号接口
2. `appmsgpublish`  —— 选中公众号后列文章（sub_action=list_ex）
3. `appmsgalbum`    —— 公开合集，零凭据，作为对照组（有 __biz+album_id 时才测）

用法
----
    python tools/dev/probe_wx_perm.py
    python tools/dev/probe_wx_perm.py --cookie _work/wechat/wxmp-cookie-header.txt
    python tools/dev/probe_wx_perm.py --token 123456789          # 自动抠不到 token 时手动给
    python tools/dev/probe_wx_perm.py --query 北京大学学生会
    python tools/dev/probe_wx_perm.py --biz MzA5MTc0MDAwMA== --album 1234567890

只依赖标准库；不发任何写操作，不落盘，不打印 cookie 内容。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://mp.weixin.qq.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

DEFAULT_COOKIE = os.path.join("_work", "wechat", "wxmp-cookie-header.txt")

RET_HINT = {
    0: "成功",
    200003: "登录态失效（cookie 过期）",
    200013: "触发频控（账号级，换号才解）",
    200074: "该接口对当前账号未开放（权限）",
    64002: "无权限",
}


def parse_cookie_header(text: str) -> dict:
    text = re.sub(r"(?i)^\s*cookie:\s*", "", text.strip())
    pairs = {}
    for part in re.split(r";\s*", text):
        if "=" in part:
            k, _, v = part.partition("=")
            if k.strip() and v.strip():
                pairs[k.strip()] = v.strip()
    return pairs


def load_cookie(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        raw = fh.read().strip()
    if raw.startswith("{"):
        data = json.loads(raw)
        return {str(k): str(v) for k, v in data.items() if v}
    pairs = parse_cookie_header(raw.splitlines()[0] if raw.splitlines() else "")
    if not pairs:
        raise SystemExit(f"! {path} 里没解析出 cookie 键值对")
    return pairs


def _get(path: str, cookie: dict, referer: str = BASE, timeout: int = 25):
    """GET 一个 cgi，返回 (正文, 最终 URL)。"""
    req = urllib.request.Request(
        path if path.startswith("http") else BASE + path,
        headers={
            "Cookie": "; ".join(f"{k}={v}" for k, v in cookie.items()),
            "User-Agent": UA,
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "*/*",
            "Accept-Language": "zh,zh-CN;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace"), resp.geturl()


def call(path: str, cookie: dict, referer: str = BASE, timeout: int = 25):
    """GET 一个 cgi，返回 (ret, err_msg, 解析后的 JSON, 最终 URL)。"""
    body, final = _get(path, cookie, referer, timeout)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return -1, f"非 JSON（前 80 字：{body[:80]!r}）", None, final
    base_resp = data.get("base_resp") or {}
    return base_resp.get("ret", -1), base_resp.get("err_msg", ""), data, final


def _logged_out(body: str, final: str) -> bool:
    """首页被踢回登录页的几个特征。"""
    return ("loginpage" in final
            or "/cgi-bin/loginpage" in body
            or "扫码登录" in body[:6000]
            or "session timeout" in body.lower())


def find_token(cookie: dict):
    """返回 (token 或 None, 状态)。状态：cookie / page / expired / unknown。"""
    for key in ("token", "wx_token"):
        if cookie.get(key, "").isdigit():
            return cookie[key], "cookie"
    probes = [
        "/cgi-bin/home?t=home/index&lang=zh_CN",
        "/cgi-bin/appmsg?t=media/appmsg_edit_v2&action=edit&isNew=1&type=77"
        "&createType=0&lang=zh_CN",
    ]
    patterns = [
        r"[?&]token=(\d{5,})",
        r"\bvar\s+token\s*=\s*[\"']?(\d{5,})",
        r"token[\"']?\s*[:=]\s*[\"']?(\d{5,})",
    ]
    expired = False
    for path in probes:
        try:
            body, final = _get(path, cookie, referer=BASE)
        except Exception as exc:  # noqa: BLE001
            print(f"   (取 token 时 {path} 失败：{exc}）")
            time.sleep(1.2)
            continue
        if _logged_out(body, final):
            expired = True
            time.sleep(1.2)
            continue
        for hay in (final, body):
            for pat in patterns:
                m = re.search(pat, hay)
                if m:
                    return m.group(1), "page"
        time.sleep(1.2)
    return None, ("expired" if expired else "unknown")


def line(tag: str, ok: bool | None, detail: str) -> str:
    mark = {True: "[ OK ]", False: "[FAIL]", None: "[ ?? ]"}[ok]
    return f"{mark} {tag:<14} {detail}"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="只读探测公众号后台接口权限")
    ap.add_argument("--cookie", default=None,
                    help=f"cookie 头文件（默认 {DEFAULT_COOKIE}）")
    ap.add_argument("--token", default=None, help="手动指定 cgi token")
    ap.add_argument("--query", default="人民日报", help="searchbiz 的测试关键词")
    ap.add_argument("--biz", default=None, help="可选：__biz（测公开合集对照）")
    ap.add_argument("--album", default=None, help="可选：album_id（配合 --biz）")
    args = ap.parse_args()

    path = args.cookie or DEFAULT_COOKIE
    if not os.path.exists(path):
        print(f"! 找不到 cookies 文件：{path}")
        print("  先登录 https://mp.weixin.qq.com/ ，F12 → Network → 复制整行 Cookie，")
        print(f"  存成 {DEFAULT_COOKIE}（本项目既有约定）。")
        return 2

    cookie = load_cookie(path)
    print(f"· cookies：{path}（{len(cookie)} 个键）")
    for must in ("slave_sid", "data_ticket", "slave_user"):
        if must not in cookie:
            print(f"  ! 缺少关键 cookie `{must}`，很可能被判定未登录")

    if args.token:
        token, state = args.token, "参数"
    else:
        token, state = find_token(cookie)
    if state == "expired":
        print("! 登录态已过期：首页被踢回 loginpage。重新登录后台、导出 cookies 后再测。")
        return 3
    if not token:
        print("! 抠不到 token。请在后台编辑器地址栏里复制 token= 后面那串数字，用 --token 传进来。")
        return 3
    print(f"· token：{token}（来源：{state}）")

    fingerprint = "".join(random.choice("0123456789abcdef") for _ in range(32))
    editor = (f"{BASE}/cgi-bin/appmsg?t=media/appmsg_edit_v2&action=edit&isNew=1"
              f"&type=77&createType=0&token={token}&lang=zh_CN"
              f"&timestamp={int(time.time() * 1000)}")
    try:  # 先把编辑器页打开一次，取得 Referer 资格（pkucli 的 warmup 同款）
        call(editor, cookie, referer=BASE)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(1.0)

    q = urllib.parse.quote(args.query)
    fakeid = None
    print("\n—— 探测结果 —————————————————————————————")
    try:
        ret, msg, data, _u = call(
            f"/cgi-bin/searchbiz?action=search_biz&begin=0&count=1&query={q}"
            f"&fingerprint={fingerprint}&token={token}&lang=zh_CN&f=json&ajax=1",
            cookie, referer=editor)
        lst = (data or {}).get("list") or []
        ok = ret == 0 and bool(lst)
        hint = RET_HINT.get(ret, "")
        print(line("searchbiz", ok,
                   f"ret={ret} {msg or hint} 命中={len(lst)}"
                   + (f" 首个={lst[0].get('nickname')}" if lst else "")))
        if lst:
            fakeid = lst[0].get("fakeid")
    except Exception as exc:  # noqa: BLE001
        print(line("searchbiz", False, f"请求异常：{exc}"))

    time.sleep(1.5)
    if fakeid:
        try:
            ret, msg, data, _u = call(
                f"/cgi-bin/appmsgpublish?sub=list&search_field=null&begin=0&count=1"
                f"&query=&fakeid={urllib.parse.quote(fakeid)}&type=101_1"
                f"&free_publish_type=1&sub_action=list_ex&fingerprint={fingerprint}"
                f"&token={token}&lang=zh_CN&f=json&ajax=1",
                cookie, referer=editor)
            page = (data or {}).get("publish_page") or ""
            total, first = None, None
            try:
                inner = json.loads(page) if page else {}
                total = inner.get("total_count")
                for item in inner.get("publish_list") or []:
                    info = json.loads(item.get("publish_info") or "{}")
                    for a in info.get("appmsgex") or []:
                        first = a.get("link")
                        break
                    if first:
                        break
            except Exception:  # noqa: BLE001
                pass
            ok = ret == 0 and bool(page)
            print(line("appmsgpublish", ok,
                       f"ret={ret} {msg or RET_HINT.get(ret, '')} total={total}"
                       + (f" link={first[:56]}…" if first else "")))
        except Exception as exc:  # noqa: BLE001
            print(line("appmsgpublish", False, f"请求异常：{exc}"))
    else:
        print(line("appmsgpublish", None, "跳过（searchbiz 没拿到 fakeid）"))

    if args.biz and args.album:
        time.sleep(1.2)
        try:
            ret, msg, data, _u = call(
                f"/mp/appmsgalbum?action=getalbum&__biz={urllib.parse.quote(args.biz)}"
                f"&album_id={urllib.parse.quote(args.album)}&count=1&f=json",
                cookie, referer=BASE)
            n = len((data or {}).get("getalbum_resp", {}).get("article_list") or [])
            print(line("appmsgalbum", ret == 0, f"ret={ret} {msg or ''} 篇数={n}（零凭据对照）"))
        except Exception as exc:  # noqa: BLE001
            print(line("appmsgalbum", False, f"请求异常：{exc}"))

    print("""
判读
----
searchbiz + appmsgpublish 都 ret=0  → 接口仍放行，面板入口消失只是界面调整
ret 非 0 / list 为空 / 200074 之类    → 当前主体确实没这门权限，个人号路线作废
200013                              → 只是频控，换一份 cookies 再测
appmsgalbum 仍 ret=0                → 公开合集不受影响，可作为兜底主路
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
