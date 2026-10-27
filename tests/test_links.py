#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""links 包的离线单测：链接还原 / 身份解析 / 可抓性判定 / 搜狗解析 / 台账合并 / 报告。

**全程不联网**：文章页与搜狗结果页都用合成 HTML，HTTP 不参与。

运行：python tests/test_links.py
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from links import article, collect, sogou                       # noqa: E402


# --------------------------------------------------------------------------- #
# 合成页面：真页面会把正文嵌进 JS 字符串，所以链接是 \x26amp; 这种转义形态
# --------------------------------------------------------------------------- #
BIZ = "MzA5NDgxNTQwNQ=="
PAGE = """<html><head><script>
var msg_title = "测试标题&nbsp;含实体";
var nickname = "浙江大学";
var ct = "1790000000";
var msg_link = "https://mp.weixin.qq.com/s?__biz=%s&mid=2651030708&idx=1&sn=aaaa&chksm=bbbb";
var biz = "%s"; var mid = "2651030708"; var idx = "1";
</script></head><body>
<p><a href="http://mp.weixin.qq.com/s?__biz=%s\\x26amp;mid=2651029408\\x26amp;idx=1\\x26amp;sn=cc\\x26amp;chksm=dd">带 chksm 的关联</a></p>
<p><a href="http://mp.weixin.qq.com/s?__biz=%s&amp;mid=2651030684&amp;idx=1&amp;sn=ee">缺 chksm 的关联</a></p>
<script>var tpl = "http://mp.weixin.qq.com/s?__biz=${window.biz}&mid=${window.mid}&idx=1";</script>
</body></html>""" % (BIZ, BIZ, BIZ, BIZ)

SHORT_PAGE = """<html><script>
var msg_title = "短链文章";
var nickname = "浙大竺院人";
var ct = "1700000000";
var msg_link = "https://mp.weixin.qq.com/s/B5Mt22iTBiUV1aAD2luGKQ";
var biz = "MjM5ODE5NzY0Nw=="; var mid = "2651089236"; var idx = "1";
</script></html>"""


class ArticleParseTest(unittest.TestCase):
    def test_meta_and_key(self):
        p = article.parse_article(PAGE, "")
        self.assertEqual(p["key"], f"{BIZ}:2651030708:1")
        self.assertEqual(p["account"], "浙江大学")
        self.assertIn("测试标题", p["title"])
        self.assertTrue(p["published"].endswith("+08:00"))
        self.assertTrue(p["permanent"])            # msg_link 带 chksm

    def test_related_unescaped_and_filtered(self):
        p = article.parse_article(PAGE, "")
        keys = {r["key"] for r in p["related"]}
        self.assertIn(f"{BIZ}:2651029408:1", keys)   # 转义形态被还原
        self.assertIn(f"{BIZ}:2651030684:1", keys)   # 裸形态
        self.assertNotIn(f"{BIZ}:2651030708:1", keys)  # 自己不算关联
        # ${window.biz} 这种模板占位符不能混进来
        self.assertEqual(len(keys), 2)
        # 只有一条带 chksm
        self.assertEqual(sum(1 for r in p["related"] if r["permanent"]), 1)

    def test_short_link_falls_back_to_vars(self):
        # 短链的 msg_link 是短链本身，取不到 biz/mid；必须回退到独立变量
        p = article.parse_article(SHORT_PAGE, "")
        self.assertEqual(p["key"], "MjM5ODE5NzY0Nw==:2651089236:1")
        self.assertTrue(p["permanent"])            # /s/ 短链本身可长期用

    def test_unescape_js(self):
        self.assertEqual(article.unescape_js(r"\x3ca\x22"), '<a"')
        self.assertEqual(article.unescape_js("a&amp;b"), "a&b")

    def test_is_permanent(self):
        self.assertTrue(article.is_permanent("https://mp.weixin.qq.com/s/abc"))
        self.assertTrue(article.is_permanent("https://mp.weixin.qq.com/s?__biz=a&chksm=b"))
        self.assertFalse(article.is_permanent("https://mp.weixin.qq.com/s?__biz=a&mid=2"))
        self.assertFalse(article.is_permanent(""))

    def test_article_ref(self):
        r = article.article_ref("https://mp.weixin.qq.com/s?__biz=AAA=&mid=123456&idx=2")
        self.assertEqual(r, {"biz": "AAA=", "mid": "123456", "idx": "2"})
        self.assertIsNone(article.article_ref("https://mp.weixin.qq.com/s/abc"))
        self.assertIsNone(article.article_ref(""))


class SogouParseTest(unittest.TestCase):
    PAGE = """<ul>
<li id="sogou_vr_11002601_box_0">
  <a href="/link?url=abc&amp;type=2" uigs="article_title_0">通知&nbsp;一</a>
  <span class="all-time-y2">浙江大学</span>
  <span class="txt-info">摘要</span>
  <script>timeConvert('1790000000')</script>
</li>
<li id="sogou_vr_11002601_box_1">
  <a href="/link?url=def name&type=2" uigs="article_title_1">讲座 二</a>
  <span class="all-time-y2">浙大团委</span>
</li>
</ul>"""

    def test_parse_page(self):
        items = sogou.parse_page(self.PAGE)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["account"], "浙江大学")
        self.assertEqual(items[0]["title"], "通知 一")
        self.assertTrue(items[0]["published"].endswith("+08:00"))
        self.assertTrue(items[0]["sogou_link"].startswith("https://weixin.sogou.com/link"))
        # href 里的空格必须被转义，否则 urllib 直接拒发
        self.assertIn("%20", items[1]["sogou_link"])

    def test_parse_empty(self):
        self.assertEqual(sogou.parse_page(""), [])


class RulesTest(unittest.TestCase):
    RULES = {"accounts": {"浙江大学", "浙大团委"}, "pattern": r"浙江大学|浙大"}

    def test_publisher_only(self):
        self.assertTrue(collect.relevant_publisher("浙江大学", self.RULES))
        self.assertTrue(collect.relevant_publisher("浙大竺院人", self.RULES))  # 号名含关键词
        # 别家媒体写了本校的事：发布者不属于本校 → 必须拒
        self.assertFalse(collect.relevant_publisher("杭州日报", self.RULES))
        self.assertFalse(collect.relevant_publisher("", self.RULES))

    def test_exclude_blocks_cross_campus(self):
        # 上交/西交的 filter 都含「交大」，必须靠 exclude 挡住对方（实测踩过）
        rules = {"accounts": {"上海交通大学"}, "pattern": r"上海交通大学|上海交大|交大",
                 "exclude": r"西安交通大学|西安交大|XJTU"}
        self.assertTrue(collect.relevant_publisher("上海交大电院", rules))
        self.assertFalse(collect.relevant_publisher("西安交大二附院", rules))
        # 名单精确命中优先于 exclude（人工名单比正则可信）
        rules["accounts"].add("西安交大二附院")
        self.assertTrue(collect.relevant_publisher("西安交大二附院", rules))

    def test_prune_library(self):
        lib = {"campus": "sjtu", "articles": {}, "runs": []}
        collect.upsert(lib, {"key": "k1", "tier": collect.TIER_A, "url": "u",
                             "account": "上海交大电院", "title": "a", "sources": ["album"]})
        collect.upsert(lib, {"key": "k2", "tier": collect.TIER_B, "url": "",
                             "account": "西安交大二附院", "title": "b", "sources": ["sogou:q"]})
        rules = {"accounts": {"上海交通大学"}, "pattern": r"上海交通大学|上海交大|交大",
                 "exclude": r"西安交通大学|西安交大"}
        self.assertEqual(collect.prune_library(lib, rules), 1)
        self.assertEqual(list(lib["articles"]), ["k1"])

    def test_title_key_is_stable(self):
        a = collect.title_key("浙江大学", "关于 2026 年招生｜通知")
        b = collect.title_key("浙江大学", "关于2026年招生|通知")
        self.assertEqual(a, b)                     # 标点/空格不影响
        self.assertNotEqual(a, collect.title_key("浙大团委", "关于 2026 年招生｜通知"))
        self.assertTrue(a.startswith("t:"))


class LibTest(unittest.TestCase):
    def setUp(self):
        self.lib = {"campus": "zju", "articles": {}, "runs": []}

    def test_upsert_merges_sources_and_keeps_url(self):
        key = "biz1:100:1"
        self.assertEqual(collect.upsert(self.lib, {
            "key": key, "tier": collect.TIER_B, "url": "", "title": "T",
            "account": "A", "sources": ["sogou:q1"]}), "new")
        self.assertEqual(collect.upsert(self.lib, {
            "key": key, "tier": collect.TIER_A, "url": "https://x",
            "title": "T", "account": "A", "sources": ["album"]}), "merged")
        rec = self.lib["articles"][key]
        self.assertEqual(rec["url"], "https://x")
        self.assertEqual(rec["tier"], collect.TIER_A)
        self.assertEqual(rec["sources"], ["sogou:q1", "album"])

    def test_summarize_splits_tiers(self):
        for i, tier in enumerate([collect.TIER_A, collect.TIER_A, collect.TIER_B]):
            collect.upsert(self.lib, {
                "key": f"k{i}", "tier": tier,
                "url": "https://x" if tier == collect.TIER_A else "",
                "title": f"t{i}", "account": "号A" if i < 2 else "号B",
                "published": "2026-10-0%dT10:00:00+08:00" % (i + 1),
                "sources": ["seed"]})
        s = collect.summarize(self.lib)
        self.assertEqual((s["linkable"], s["metadata_only"], s["total"]), (2, 1, 3))
        self.assertEqual(s["publishers"], 2)
        self.assertEqual(s["rows"][0]["account"], "号A")
        self.assertEqual(s["rows"][0]["first"], "2026-10-01")

    def test_prune_albums_drops_only_album_only_entries(self):
        lib = {"campus": "zju", "articles": {}, "runs": []}
        # 纯合集来源 → 合集产物里没有它了，应被清掉
        collect.upsert(lib, {"key": "k1", "tier": collect.TIER_A, "url": "u1",
                             "title": "a", "account": "浙江大学", "sources": ["album"]})
        # 合集 + 种子双来源 → 另有出处，保留
        collect.upsert(lib, {"key": "k2", "tier": collect.TIER_A, "url": "u2",
                             "title": "b", "account": "浙江大学",
                             "sources": ["album", "seed"]})
        # 当前产物里还在的合集条目 → 保留
        collect.upsert(lib, {"key": "k3", "tier": collect.TIER_A, "url": "u3",
                             "title": "c", "account": "浙江大学", "sources": ["album"]})
        self.assertEqual(collect.prune_albums(lib, {"k3"}), 1)
        self.assertEqual(sorted(lib["articles"]), ["k2", "k3"])

    def test_site_items_skip_metadata_only(self):
        lib = {"campus": "zju", "articles": {}, "runs": []}
        collect.upsert(lib, {"key": "a1", "tier": collect.TIER_A, "url": "https://x/1",
                             "title": "关于招生工作的通知", "account": "浙江大学",
                             "published": "2026-10-20T10:00:00+08:00", "sources": ["album"]})
        collect.upsert(lib, {"key": "b1", "tier": collect.TIER_B, "url": "",
                             "title": "只有元数据", "account": "浙江大学",
                             "published": "2026-10-20T10:00:00+08:00", "sources": ["sogou:q"]})
        items = collect.to_site_items(lib, retention_days=90)
        self.assertEqual(len(items), 1)                  # B 档不入站
        self.assertEqual(items[0]["i"], "https://x/1")
        self.assertEqual(items[0]["dt"]["sg"], "wechat")
        self.assertEqual(items[0]["c"], "学业教务")      # 「招生」命中分类
        self.assertEqual(set(items[0]),
                         {"i", "t", "s", "si", "d", "u", "c", "g", "en", "ens", "dt"})

    def test_album_id_extraction(self):
        html = ('<a href="mp/appmsgalbum?__biz=X&album_id=1234567890123">a</a>'
                '<a href="...album_id=9998887776665#rd">b</a>')
        self.assertEqual(collect.albums_in_page(html),
                         ["1234567890123", "9998887776665"])
        self.assertEqual(collect.albums_in_page(""), [])


class HandoffTest(unittest.TestCase):
    def test_handoff_merges_into_albums_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            albums_dir = os.path.join(tmp, "_work", "albums")
            os.makedirs(albums_dir)
            work = os.path.join(tmp, "_work", "links")
            with mock.patch.object(collect, "WORK_DIR", work), \
                 mock.patch.object(collect, "ROOT", tmp):
                collect.handoff_albums("zju", {
                    ("bizA", "111"): {"biz": "bizA", "album_id": "111", "account": "号A"},
                })
                path = os.path.join(albums_dir, "discovered-zju.json")
                rows = collect.read_json(path, [])
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["album_id"], "111")
                # 再交一次：同 key 不重复，补字段
                collect.handoff_albums("zju", {
                    ("bizA", "111"): {"biz": "bizA", "album_id": "111", "account": "号A2"},
                    ("bizB", "222"): {"biz": "bizB", "album_id": "222", "account": "号B"},
                })
                rows = collect.read_json(path, [])
                self.assertEqual(len(rows), 2)
                self.assertEqual({r["album_id"] for r in rows}, {"111", "222"})


class SeedReadTest(unittest.TestCase):
    def test_read_seeds_skips_comments_and_dupes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "seeds-zju.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# 注释\nhttps://mp.weixin.qq.com/s/a\n"
                         "https://mp.weixin.qq.com/s/a\n"
                         "https://example.com/x\n")
            with mock.patch.object(collect, "seed_files", return_value=[path]):
                self.assertEqual(collect.read_seeds("zju"),
                                 ["https://mp.weixin.qq.com/s/a"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
