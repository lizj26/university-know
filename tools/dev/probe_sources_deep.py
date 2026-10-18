#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第二轮:对第一轮没找到文章列表的站点深挖子页;西交介绍页穷举外链域名。"""
import gzip
import json
import os
import re
import ssl
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def get(url, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 Chrome/120.0", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        raw = r.read(2_500_000)
        if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except Exception:
                raw = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw)
        m = re.search(r"charset=([\w-]+)", r.headers.get("Content-Type", ""), re.I)
        enc = m.group(1) if m else "utf-8"
        try:
            text = raw.decode(enc, "replace")
        except LookupError:
            text = raw.decode("utf-8", "replace")
        if text.count("\ufffd"):
            for alt in ("gb18030", "utf-8"):
                cand = raw.decode(alt, "replace")
                if cand.count("\ufffd") < text.count("\ufffd"):
                    text = cand
        return text, r.url


def article_count(html, base):
    n = 0
    for m in re.finditer(r'(?is)<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html):
        href = urllib.parse.urljoin(base, m.group(1).strip())
        p = urllib.parse.urlparse(href)
        text = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", m.group(2)))
        if (p.scheme in ("http", "https") and re.search(r"\.(?:s?html?)$", p.path)
                and re.search(r"\d{3,}", p.path) and len(text) >= 6):
            n += 1
    return n


def probe(site_name, base_url, max_probe=25):
    """穷举 base 页面上的同主机链接,找出有文章列表的子页。"""
    try:
        t, final = get(base_url)
    except Exception as exc:  # noqa: BLE001
        return {"name": site_name, "url": base_url, "err": str(exc)[:50]}
    base_host = urllib.parse.urlparse(final).netloc.lower()
    seen, cands = set(), []
    for m in re.finditer(r'(?is)<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>([^<]{0,24})', t):
        href = urllib.parse.urljoin(final, m.group(1).strip())
        p = urllib.parse.urlparse(href)
        if p.netloc.lower() != base_host or p.scheme not in ("http", "https"):
            continue
        text = re.sub(r"\s+", "", m.group(2))
        # 越像列表页越优先:URL 或锚文本含 通知/新闻/公告/动态/tzgg/xwzx/list
        score = 0
        if re.search(r"通知|新闻|公告|动态|资讯", text):
            score += 2
        if re.search(r"tzgg|tz|news|xwzx|xwdt|list|zpxx|info", p.path, re.I):
            score += 1
        if href not in seen:
            seen.add(href)
            cands.append((score, href, text))
    cands.sort(key=lambda x: -x[0])
    best = ("", 0)
    tried = 0
    for score, href, text in cands:
        if tried >= max_probe:
            break
        if re.search(r"\.(?:jpg|png|css|js|ico|zip|pdf)$", href, re.I):
            continue
        tried += 1
        try:
            t2, f2 = get(href)
        except Exception:
            continue
        n = article_count(t2, f2)
        if n > best[1]:
            best = (f2, n)
        if best[1] >= 10:
            break
    return {"name": site_name, "url": base_url, "list_url": best[0], "list_articles": best[1],
            "probed": tried}


def main():
    rows = json.load(open(os.path.join(ROOT, "_work", "source_verify.json"), encoding="utf-8"))
    todo = [r for r in rows if r["ok"] and (r["home_articles"] < 3 and r["list_articles"] < 3)]
    print("需要深挖的站点:", len(todo))
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda r: probe(r["name"], r["url"]), todo))
    for r in results:
        print("  %-26s -> %s  (%s)" % (r["name"], r.get("list_url", "")[:80],
                                       r.get("list_articles", r.get("err", "?"))))
    out = os.path.join(ROOT, "_work", "source_probe2.json")
    json.dump(results, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("明细 ->", out)


if __name__ == "__main__":
    main()
