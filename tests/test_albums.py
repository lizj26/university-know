#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""albums 包的离线单测：解析 / 分页 / 增量 / 分类一致性 / 导出。

**全程不联网、不碰真实 _work**：
  * HTTP 层全部通过 `fetch=` / `fetch_text=` 注入假函数；
  * 落盘类测试把 store 的路径常量指到临时目录。

运行：python tests/test_albums.py
"""

import ast
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from albums import classify, client, collect, store          # noqa: E402
from albums.client import AlbumRef                          # noqa: E402


# --------------------------------------------------------------------------- #
# 造数据
# --------------------------------------------------------------------------- #
def article(mid, idx=1, ts="1790000000", title="标题"):
    return {"title": title, "create_time": ts, "msgid": str(mid), "itemidx": str(idx),
            "url": f"http://mp.weixin.qq.com/s?__biz=MzA=&mid={mid}&idx={idx}&sn=x&chksm=y#rd"}


def payload(rows, *, cont=0, total=10, title="合集名", account="浙江大学", ret=0):
    return {"base_resp": {"ret": ret},
            "getalbum_resp": {
                "base_info": {"title": title, "nickname": account,
                              "article_count": str(total), "username": "gh_x"},
                "article_list": rows,
                "continue_flag": cont,
            }}


class ParseTest(unittest.TestCase):
    def test_meta_and_rows(self):
        page = client.parse_album_payload(payload([article(200), article(100)], cont=1,
                                                 total=127))
        self.assertTrue(page.ok)
        self.assertEqual(page.meta.title, "合集名")
        self.assertEqual(page.meta.account, "浙江大学")     # 公众号名来自接口，不用猜
        self.assertEqual(page.meta.article_count, 127)
        self.assertTrue(page.continue_flag)
        self.assertEqual(len(page.rows), 2)
        self.assertEqual(page.rows[0].key, "200:1")
        self.assertTrue(page.rows[0].published.endswith("+08:00"))

    def test_bad_param(self):
        page = client.parse_album_payload(payload([], ret=10004))
        self.assertFalse(page.ok)
        self.assertEqual(page.ret, 10004)
        self.assertIn("10004", page.error)

    def test_rows_without_url_or_title_dropped(self):
        rows = [{"title": "", "url": "x"}, {"title": "t", "url": ""}, article(1)]
        page = client.parse_album_payload(payload(rows))
        self.assertEqual(len(page.rows), 1)

    def test_string_continue_flag(self):
        page = client.parse_album_payload(payload([article(1)], cont="1"))
        self.assertTrue(page.continue_flag)

    def test_ts_to_iso(self):
        self.assertEqual(client.ts_to_iso("0"), "")
        self.assertEqual(client.ts_to_iso("not-a-number"), "")
        self.assertEqual(client.ts_to_iso(1_700_000_000), "2023-11-15T06:13:20+08:00")


class RefTest(unittest.TestCase):
    def test_discover_both_orders_and_dedupe(self):
        html = ('<a href="mp/appmsgalbum?__biz=MzA5%3D%3D&action=getalbum'
                '&album_id=1234567890#wechat_redirect">x</a>'
                '<a href="/mp/appmsgalbum?album_id=99887766&__biz=MzIy">y</a>'
                '<a href="mp/appmsgalbum?__biz=MzA5%3D%3D&album_id=1234567890">dup</a>')
        refs = client.discover_refs(html)
        self.assertEqual([(r.biz, r.album_id) for r in refs],
                         [("MzA5==", "1234567890"), ("MzIy", "99887766")])

    def test_no_cross_pairing(self):
        # __biz 和 album_id 分属两段不相关的内容，不能配成一对
        html = '__biz=MzA5== ... 很多别的东西 ... album_id=1234567890'
        self.assertEqual(client.discover_refs(html), [])

    def test_parse_ref_forms(self):
        self.assertEqual(client.parse_ref("MzA5==:1234567890"),
                         AlbumRef("MzA5==", "1234567890"))
        self.assertEqual(client.parse_ref("https://mp.weixin.qq.com/mp/appmsgalbum"
                                          "?__biz=MzA5==&album_id=1234567890"),
                         AlbumRef("MzA5==", "1234567890"))
        self.assertIsNone(client.parse_ref("随便一段话"))

    def test_parse_account_skips_gh(self):
        self.assertEqual(client.parse_account('var user_name = "gh_abc";'), "")
        self.assertEqual(client.parse_account('var nickname = "浙大官方";'), "浙大官方")


# --------------------------------------------------------------------------- #
# 分页 / 增量
# --------------------------------------------------------------------------- #
def make_fetch(pages, is_reverse=True):
    """pages: [[row...], ...] → 按调用次序分页的假接口；记录每次的游标。

    默认 `is_reverse=True`（该合集本来就是新→旧），所以不触发"翻转重取"。
    正序合集的行为单独用 make_asc_fetch 测。
    """
    calls = []

    def fake(ref, begin_msgid=0, begin_itemidx=0, timeout=15, retries=2,
             newest_first=True):
        calls.append((begin_msgid, begin_itemidx, newest_first))
        i = len(calls) - 1
        if i >= len(pages):
            return client.AlbumPage(ref=ref, error="越界")
        rows = [client.parse_article(r) for r in pages[i]]
        cont = i < len(pages) - 1
        return client.AlbumPage(ref=ref, meta=client.AlbumMeta(title="T", account="A",
                                                              article_count=99,
                                                              is_reverse=is_reverse),
                                rows=[r for r in rows if r], continue_flag=cont)

    fake.calls = calls
    return fake


CFG = {"campuses": {"zju": {"name": "浙大", "filter": "浙江大学", "accounts": ["浙江大学"]}},
       "albums": []}
REC = {"biz": "MzA=", "album_id": "111", "title": "", "account": "", "source": "manual"}


class PaginationTest(unittest.TestCase):
    def test_walk_all_pages(self):
        fetch = make_fetch([[article(300), article(200)], [article(100)]])
        rec = collect.fetch_one(REC, campus="zju", cfg=CFG, pages=5, state={}, fetch=fetch)
        self.assertEqual(rec["fetched"], 3)
        self.assertEqual(rec["pages"], 2)
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["last_msgid"], "300")
        # 第二页应该用第一页末条的游标续取（不是 0）
        self.assertEqual(fetch.calls[1][:2], ("200", "1"))

    def test_watermark_stops_early(self):
        fetch = make_fetch([[article(300), article(200)], [article(100)]])
        state = {"albums": {"MzA=:111": {"last_msgid": "300", "seen": ["300:1", "200:1"]}}}
        rec = collect.fetch_one(REC, campus="zju", cfg=CFG, pages=5, new_only=True,
                                state=state, fetch=fetch)
        self.assertEqual(rec["pages"], 1)          # 首页整页都是旧文 → 立刻收工
        self.assertEqual(rec["new"], 0)

    def test_watermark_continues_when_page_has_new(self):
        # 首屏就有比水位新的文章（合集刚更新）→ 不能停，要一直翻到整页都是旧文
        fetch = make_fetch([[article(400), article(300)], [article(200)]])
        state = {"albums": {"MzA=:111": {"last_msgid": "300", "seen": []}}}
        rec = collect.fetch_one(REC, campus="zju", cfg=CFG, pages=5, new_only=True,
                                state=state, fetch=fetch)
        self.assertEqual(rec["pages"], 2)
        self.assertEqual(rec["new"], 3)

    def test_ascending_album_is_flipped_to_newest_first(self):
        # 正序合集：默认顺序是最旧在前。第一次按默认取，读到 is_reverse=False
        # 后必须带 newest_first=True 重取一次，之后按新→旧处理。
        old_first = [article(100), article(200)]        # 正序：旧的在前
        new_first = [article(200), article(100)]        # 倒序：新的在前
        calls = []

        def fake(ref, begin_msgid=0, begin_itemidx=0, timeout=15, retries=2,
                 newest_first=False):
            calls.append((begin_msgid, newest_first))
            rows = new_first if newest_first else old_first
            return client.AlbumPage(ref=ref,
                                    meta=client.AlbumMeta(title="T", account="A",
                                                          article_count=2,
                                                          is_reverse=False),
                                    rows=[client.parse_article(r) for r in rows],
                                    continue_flag=False)

        rec = collect.fetch_one(REC, campus="zju", cfg=CFG, pages=3, state={},
                                pacing=0, max_age_days=60, fetch=fake)
        self.assertEqual(calls[0], (0, False))         # 先按默认取
        self.assertEqual(calls[1], (0, True))          # 发现正序 → 翻转重取
        self.assertEqual([i["published"] for i in rec["items"]],
                         sorted([i["published"] for i in rec["items"]], reverse=True))
        self.assertEqual(rec["status"], "ok")

    def test_stale_album_dropped_by_age(self):
        # 最新一篇早于 60 天门槛 → 整集作废：不翻页、不收文章
        fetch = make_fetch([[article(100, ts="1600000000")],      # 2020-09
                            [article(90, ts="1600000000")]])
        rec = collect.fetch_one(REC, campus="zju", cfg=CFG, pages=5, state={},
                                pacing=0, max_age_days=60, fetch=fetch)
        self.assertEqual(rec["status"], "stale")
        self.assertTrue(rec["stale"])
        self.assertEqual(rec["items"], [])
        self.assertEqual(rec["pages"], 1)              # 没有继续翻页
        self.assertIn("更早于", rec["note"].replace("早于", "更早于"))

    def test_fresh_album_kept_by_age(self):
        # 最新一篇在门槛内 → 正常收
        fetch = make_fetch([[article(300, ts="1790000000")]])     # 2026-09
        rec = collect.fetch_one(REC, campus="zju", cfg=CFG, pages=3, state={},
                                pacing=0, max_age_days=60, fetch=fetch)
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(len(rec["items"]), 1)

    def test_age_gate_off_keeps_old_album(self):
        fetch = make_fetch([[article(100, ts="1600000000")]])
        rec = collect.fetch_one(REC, campus="zju", cfg=CFG, pages=3, state={},
                                pacing=0, max_age_days=0, fetch=fetch)
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(len(rec["items"]), 1)

    def test_cursor_stall_terminates(self):
        # 接口原地打转：continue_flag 永远是 1，游标不前进 → 必须主动停
        def stuck(ref, begin_msgid=0, begin_itemidx=0, timeout=15, retries=2,
                  newest_first=True):
            rows = [client.parse_article(article(200)), client.parse_article(article(100))]
            return client.AlbumPage(ref=ref,
                                    meta=client.AlbumMeta(title="T", is_reverse=True),
                                    rows=rows, continue_flag=True)

        rec = collect.fetch_one(REC, campus="zju", cfg=CFG, pages=20, pacing=0, fetch=stuck)
        self.assertLessEqual(rec["pages"], 2)
        self.assertIn("游标", rec["error"])
        self.assertEqual(rec["fetched"], 2)        # 重复行被去重

    def test_error_page_keeps_partial(self):
        def boom(ref, **kw):
            return client.AlbumPage(ref=ref, error="网络断了")

        rec = collect.fetch_one(REC, campus="zju", cfg=CFG, pages=3, pacing=0, fetch=boom)
        self.assertEqual(rec["status"], "error")
        self.assertEqual(rec["fetched"], 0)
        self.assertIn("网络断了", rec["error"])

    def test_empty_album(self):
        fetch = make_fetch([[]])
        rec = collect.fetch_one(REC, campus="zju", cfg=CFG, pages=3, state={}, fetch=fetch)
        self.assertEqual(rec["status"], "empty")

    def test_campus_confident(self):
        self.assertTrue(collect.campus_confident(CFG, "zju", "浙江大学", ""))
        self.assertFalse(collect.campus_confident(CFG, "zju", "上海交通大学", "某合集"))
        cfg2 = {"campuses": {"zju": {}}, "albums": []}     # 没配任何依据 → 不拦
        self.assertTrue(collect.campus_confident(cfg2, "zju", "随便一个号", ""))


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #
class ExportTest(unittest.TestCase):
    def _payload(self, published):
        return {"items": [{"key": "1:1", "url": "http://x/1", "title": "关于招生简章",
                           "published": published, "album_id": "1", "album_title": "合集",
                           "account": "浙江大学", "campus_confident": True}]}

    def test_site_item_shape(self):
        items = collect.to_site_items(self._payload("2026-10-01T10:00:00+08:00"),
                                      retention_days=0)
        it = items[0]
        self.assertEqual(set(it), {"i", "t", "s", "si", "d", "u", "c", "g", "en", "ens", "dt"})
        self.assertEqual(it["i"], "http://x/1")
        self.assertEqual(it["c"], "学业教务")          # 「招生」命中分类规则
        self.assertEqual(it["dt"]["sg"], "wechat")
        self.assertEqual(it["dt"]["ab"], "合集")

    def test_retention_filter(self):
        self.assertEqual(collect.to_site_items(self._payload("2019-01-01T00:00:00+08:00")), [])
        self.assertEqual(len(collect.to_site_items(self._payload("2019-01-01T00:00:00+08:00"),
                                                   retention_days=0)), 1)

    def test_unconfident_filtered(self):
        p = self._payload("2026-10-01T10:00:00+08:00")
        p["items"][0]["campus_confident"] = False
        self.assertEqual(collect.to_site_items(p, retention_days=0), [])
        self.assertEqual(len(collect.to_site_items(p, confident_only=False,
                                                   retention_days=0)), 1)


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #
class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="albums-test-")
        self.patches = [
            mock.patch.object(store, "WORK_DIR", self.tmp),
            mock.patch.object(store, "CONFIG_PATH", os.path.join(self.tmp, "albums.json")),
            mock.patch.object(store, "STATE_PATH", os.path.join(self.tmp, "state.json")),
        ]
        for p in self.patches:
            p.start()
        self.cfg = {"version": 1, "campuses": {"zju": {"name": "浙大"}}, "albums": []}

    def tearDown(self):
        for p in self.patches:
            p.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_add_album_dedupes(self):
        self.assertTrue(store.add_album(self.cfg, "zju", "MzA=", "111", title="A"))
        self.assertFalse(store.add_album(self.cfg, "zju", "MzA=", "111"))
        self.assertFalse(store.add_album(self.cfg, "zju", "MzA=", "111", account="浙大"))
        saved = store.load_config()
        self.assertEqual(len(saved["albums"]), 1)
        self.assertEqual(saved["albums"][0]["account"], "浙大")   # 空字段被补上

    def test_atomic_roundtrip(self):
        path = store.write_json(os.path.join(self.tmp, "x.json"), {"a": 1})
        self.assertEqual(store.read_json(path), {"a": 1})
        self.assertEqual(store.read_json(os.path.join(self.tmp, "nope.json"), {}), {})

    def test_state_seen_capped(self):
        state = {"albums": {}}
        store.update_album_state(state, "k", seen=[f"{i}:1" for i in range(3)])
        store.update_album_state(state, "k", seen=["1:1", "9:1"])
        self.assertEqual(state["albums"]["k"]["seen"], ["0:1", "1:1", "2:1", "9:1"])

    def test_max_msgid(self):
        rows = [client.parse_article(article(10)), client.parse_article(article(99))]
        self.assertEqual(store.max_msgid(rows), "99")
        self.assertEqual(store.max_msgid([]), "")

    def test_seed_groups(self):
        path = os.path.join(self.tmp, "seeds-zju.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# 说明\n# 号A\nhttps://mp.weixin.qq.com/s/a1\nhttps://mp.weixin.qq.com/s/a2\n"
                     "# 号B\nhttps://mp.weixin.qq.com/s/b1\n")
        cfg = {"campuses": {"zju": {"seeds": [path]}}, "albums": []}
        groups = store.load_seed_groups(cfg, "zju")
        self.assertEqual([(l, len(u)) for l, u in groups], [("号A", 2), ("号B", 1)])
        picked = store.sample_seed_links(cfg, "zju", per_account=1, limit=10)
        self.assertEqual([u.rsplit("/", 1)[-1] for _, u in picked], ["a1", "b1"])


# --------------------------------------------------------------------------- #
# 分类规则不能和官网链路漂移
# --------------------------------------------------------------------------- #
class ClassifySyncTest(unittest.TestCase):
    def test_rules_match_fetch_campus(self):
        path = os.path.join(ROOT, "fetch_campus.py")
        if not os.path.exists(path):
            self.skipTest("没有 fetch_campus.py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        found = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    getattr(t, "id", "") == "CATEGORY_RULES" for t in node.targets):
                found = ast.literal_eval(node.value)
                break
        self.assertIsNotNone(found, "fetch_campus.py 里找不到 CATEGORY_RULES")
        self.assertEqual(classify.CATEGORY_RULES, found,
                         "分类规则与 fetch_campus.py 不一致：改了一边就同步另一边")


if __name__ == "__main__":
    unittest.main(verbosity=2)
