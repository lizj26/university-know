#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/wxmp_spider.py 的离线单元测试（不联网）。

跑法：
    python tests/test_wxmp_spider.py -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import wxmp_spider as S  # noqa: E402
import wxmp_pool as P  # noqa: E402


# ---------------------------------------------------------------- 响应夹具
def make_searchbiz_resp() -> dict:
    return {
        "base_resp": {"ret": 0, "err_msg": "ok"},
        "total": 2,
        "list": [
            {
                "fakeid": "MjM5MjAxNDM4MA==",
                "nickname": "人民日报",
                "alias": "rmrbwx",
                "round_head_img": "http://mmbiz.qpic.cn/x.jpg",
                "service_type": 0,
                "signature": "参与、沟通、记录时代。",
                "username": "",
                "verify_status": 3,
            },
            {
                "fakeid": "MzAxMTU5OTY3MQ==",
                "nickname": "浙江大学CC98论坛",
                "alias": "zjucc98",
                "signature": "",
                "service_type": 0,
                "verify_status": 1,
            },
        ],
    }


def make_appmsgpublish_resp() -> dict:
    """严格照 pkucli 注释里的三层嵌套：publish_page(str) → publish_list[].publish_info(str) → appmsgex[]。"""
    articles = [
        {
            "aid": "2247483650_1",
            "appmsgid": 2247483650,
            "itemidx": 1,
            "title": "  第一篇文章  ",
            "link": "https://mp.weixin.qq.com/s/AAA",
            "cover": "https://mmbiz.qpic.cn/cover1.jpg",
            "digest": "摘要一",
            "author_name": "小编",
            "update_time": 1760000000,
            "create_time": 1759990000,
        },
        {
            "aid": "2247483649_2",
            "appmsgid": 2247483649,
            "itemidx": 2,
            "title": "第二篇文章",
            "link": "https://mp.weixin.qq.com/s/BBB",
            "cover": "",
            "digest": "",
            "author_name": "",
            "update_time": 1759900000,
            "create_time": 1759900000,
        },
    ]
    publish_info = {"appmsgex": articles, "msgid": 1}
    page = {
        "total_count": 42,
        "publish_list": [
            {"publish_info": json.dumps(publish_info, ensure_ascii=False)},
            {"publish_info": "not-a-json"},          # 坏数据必须被跳过而不是炸掉
            {"no_info": True},                        # 缺字段同上
        ],
    }
    return {"base_resp": {"ret": 0, "err_msg": "ok"}, "publish_page": json.dumps(page, ensure_ascii=False)}


# ---------------------------------------------------------------- 测试
class TestToken(unittest.TestCase):
    def test_relative_redirect(self):
        self.assertEqual(
            S.extract_token("/cgi-bin/home?t=home/index&lang=zh_CN&token=362631694"),
            "362631694",
        )

    def test_absolute_redirect_with_extra_params(self):
        self.assertEqual(
            S.extract_token("https://mp.weixin.qq.com/cgi-bin/home?lang=zh_CN&token=abc123&x=1"),
            "abc123",
        )

    def test_missing_token_raises(self):
        with self.assertRaises(S.WxmpError):
            S.extract_token("/cgi-bin/home?lang=zh_CN")

    def test_empty_raises(self):
        with self.assertRaises(S.WxmpError):
            S.extract_token("")


class TestCheckRet(unittest.TestCase):
    def test_ok(self):
        S.check_ret({"base_resp": {"ret": 0}}, "x")     # 不抛

    def test_session_expired(self):
        with self.assertRaises(S.WxmpError) as cm:
            S.check_ret({"base_resp": {"ret": 200003, "err_msg": "invalid session"}}, "searchbiz")
        self.assertEqual(cm.exception.code, 200003)
        self.assertIn("重新 login", str(cm.exception))

    def test_rate_limited(self):
        with self.assertRaises(S.WxmpError) as cm:
            S.check_ret({"base_resp": {"ret": 200013, "err_msg": "freq limit"}}, "appmsgpublish")
        self.assertEqual(cm.exception.code, 200013)
        self.assertIn("限流", str(cm.exception))

    def test_unknown_ret_carries_message(self):
        with self.assertRaises(S.WxmpError) as cm:
            S.check_ret({"base_resp": {"ret": 1, "err_msg": "boom"}}, "x")
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("boom", str(cm.exception))

    def test_missing_base_resp(self):
        with self.assertRaises(S.WxmpError) as cm:
            S.check_ret({}, "x")
        self.assertEqual(cm.exception.code, -1)


class TestSearchParse(unittest.TestCase):
    def test_fake_session_returns_list(self):
        class FakeSession:
            def get(self, url, headers=None, timeout=None):
                self.url, self.headers = url, headers

                class R:
                    status_code = 200
                    text = ""

                    def raise_for_status(self):
                        pass

                    def json(self_inner):
                        return make_searchbiz_resp()

                return R()

        sess = FakeSession()
        ctx = S.Ctx(store=None, session=sess, token="T1", fingerprint="f" * 32,
                    referer="https://mp.weixin.qq.com/cgi-bin/appmsg?x=1")
        hits = S.search_biz(ctx, "人民日报", 5)

        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0]["fakeid"], "MjM5MjAxNDM4MA==")
        self.assertEqual(hits[0]["nickname"], "人民日报")
        # 关键协议细节：必须带 token / fingerprint / ajax=1，query 要 URL 编码
        self.assertIn("token=T1", sess.url)
        self.assertIn("fingerprint=" + "f" * 32, sess.url)
        self.assertIn("ajax=1", sess.url)
        self.assertIn("%E4%BA%BA%E6%B0%91%E6%97%A5%E6%8A%A5", sess.url)   # 人民日报
        # xhr 头：Referer 必须是编辑器页，否则后台判权限不足
        self.assertEqual(sess.headers["X-Requested-With"], "XMLHttpRequest")
        self.assertEqual(sess.headers["Referer"], ctx.referer)


class TestPublishParse(unittest.TestCase):
    def test_three_layer_nesting(self):
        arts, total = S.parse_publish_page(make_appmsgpublish_resp())
        self.assertEqual(total, 42)
        self.assertEqual(len(arts), 2)                  # 坏 publish_info / 缺字段被跳过
        self.assertEqual(arts[0]["title"], "第一篇文章")   # 首尾空格被 strip
        self.assertEqual(arts[0]["update_time"], 1760000000)
        self.assertEqual(arts[1]["link"], "https://mp.weixin.qq.com/s/BBB")

    def test_missing_publish_page(self):
        self.assertEqual(S.parse_publish_page({"base_resp": {"ret": 0}}), ([], 0))

    def test_broken_publish_page_json(self):
        self.assertEqual(S.parse_publish_page({"publish_page": "{not json"}), ([], 0))

    def test_empty_publish_list(self):
        page = {"total_count": 0, "publish_list": []}
        self.assertEqual(
            S.parse_publish_page({"publish_page": json.dumps(page)}), ([], 0)
        )

    def test_fetch_page_url_encodes_fakeid(self):
        seen = {}

        def fake_get_json(ctx, url):
            seen["url"] = url
            return make_appmsgpublish_resp()

        orig = S.get_json
        S.get_json = fake_get_json
        try:
            ctx = S.Ctx(store=None, session=None, token="T9", fingerprint="a" * 32, referer="R")
            arts, total = S.fetch_page(ctx, "MjM5MjAxNDM4MA==", begin=10, count=5)
        finally:
            S.get_json = orig

        self.assertIn("fakeid=MjM5MjAxNDM4MA%3D%3D", seen["url"])   # == 必须编码
        self.assertIn("begin=10", seen["url"])
        self.assertIn("count=5", seen["url"])
        self.assertIn("sub_action=list_ex", seen["url"])
        self.assertIn("type=101_1", seen["url"])
        self.assertEqual(len(arts), 2)


def _has_bs4() -> bool:
    try:
        import bs4  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_has_bs4(), "需要 bs4（html_to_markdown 的可选依赖，不影响抓取主链路）")
class TestHtml2Markdown(unittest.TestCase):
    HTML = """
    <html><body>
      <div class="rich_media_wrapper">
        <h2>标题二级</h2>
        <div id="js_content">
          <p>正文第一段，<strong>加粗</strong>和<em>斜体</em>。</p>
          <p>外链<a href="https://example.com/xx">点这里</a></p>
          <p><img data-src="https://mmbiz.qpic.cn/pic.jpg" alt="配图"></p>
          <ul><li>条目一</li><li>条目二</li></ul>
          <blockquote>引用一句话</blockquote>
        </div>
        <script>var a=1;</script>
        <p>页脚广告</p>
      </div>
    </body></html>
    """

    def test_extracts_js_content_only(self):
        md = S.html_to_markdown(self.HTML)
        self.assertIn("正文第一段", md)
        self.assertNotIn("页脚广告", md)     # 正文容器外的东西被丢掉
        self.assertNotIn("var a=1", md)      # script 被剔除
        self.assertNotIn("标题二级", md)      # h2 在 js_content 之外

    def test_inline_formats(self):
        md = S.html_to_markdown(self.HTML)
        self.assertIn("**加粗**", md)
        self.assertIn("*斜体*", md)
        self.assertIn("[点这里](https://example.com/xx)", md)

    def test_image_uses_data_src(self):
        md = S.html_to_markdown(self.HTML)
        self.assertIn("![配图](https://mmbiz.qpic.cn/pic.jpg)", md)

    def test_list_and_blockquote(self):
        md = S.html_to_markdown(self.HTML)
        self.assertIn("- 条目一", md)
        self.assertIn("- 条目二", md)
        self.assertIn("> 引用一句话", md)

    def test_no_excessive_blank_lines(self):
        md = S.html_to_markdown(self.HTML)
        self.assertNotIn("\n\n\n", md)

    def test_plain_fragment_without_js_content(self):
        md = S.html_to_markdown("<p>只有一段</p>")
        self.assertIn("只有一段", md)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = S.Store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_session_roundtrip(self):
        self.assertIsNone(self.store.load_session())
        self.store.save_session("846199141", "b" * 32, "3699431467")
        s = self.store.load_session()
        self.assertEqual(s["token"], "846199141")
        self.assertEqual(s["fingerprint"], "b" * 32)
        self.assertEqual(s["bizuin"], "3699431467")
        self.assertIn("created_at", s)

    def test_cookies_roundtrip(self):
        import requests

        sess = requests.Session()
        sess.cookies.set("slave_sid", "abc", domain=".qq.com", path="/")
        sess.cookies.set("data_ticket", "xyz", domain=".qq.com", path="/")
        self.store.save_cookies(sess)

        loaded = self.store.load_cookies()
        names = {c["name"] for c in loaded}
        self.assertEqual(names, {"slave_sid", "data_ticket"})
        self.assertEqual({c["value"] for c in loaded}, {"abc", "xyz"})

        sess2 = S.build_session(self.store)
        self.assertEqual(sess2.cookies.get("slave_sid"), "abc")

    def test_legacy_flat_dict_compatible(self):
        """_work/wechat/wxmp-cookies.json 是扁平 {name: value}，load 要能读。"""
        self.store.cookies_path.write_text(
            json.dumps({"wxuin": "91616372135765", "slave_sid": "TkEz"}), "utf-8"
        )
        loaded = self.store.load_cookies()
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["domain"], ".qq.com")

    def test_import_legacy(self):
        ck = Path(self.tmp.name) / "c.json"
        tk = Path(self.tmp.name) / "t.txt"
        ck.write_text(json.dumps({"wxuin": "1", "slave_sid": "2"}), "utf-8")
        tk.write_text("846199141\n", "utf-8")
        S.import_legacy(self.store, ck, tk, "c" * 32)
        self.assertEqual(self.store.load_session()["token"], "846199141")
        self.assertEqual(len(self.store.load_cookies()), 2)

    def test_import_legacy_without_token_raises(self):
        ck = Path(self.tmp.name) / "c.json"
        ck.write_text(json.dumps({"wxuin": "1"}), "utf-8")
        with self.assertRaises(S.WxmpError):
            S.import_legacy(self.store, ck, None, None)

    def test_clear(self):
        self.store.save_session("t", "f", None)
        self.store.clear()
        self.assertIsNone(self.store.load_session())
        self.assertEqual(self.store.load_cookies(), [])

    def test_fingerprint_shape(self):
        fp = S.new_fingerprint()
        self.assertEqual(len(fp), 32)
        self.assertTrue(all(c in "0123456789abcdef" for c in fp))
        self.assertNotEqual(fp, S.new_fingerprint())


class TestDiag(unittest.TestCase):
    """诊断判定：账号级 / fakeid 级 / 正常 / 凭据过期，四种结论各自的触发条件。"""

    def p(self, name, code, n=0):
        return S.Probe(name=name, fakeid="f_" + name, code=code, articles=n)

    def test_all_limited_is_account_level(self):
        key, text, advice = S.classify_verdict(
            True, 0, [self.p("A", 200013), self.p("B", 200013), self.p("C", 200013)]
        )
        self.assertEqual(key, "account")
        self.assertIn("账号级", text)
        self.assertIn("pool2", advice)

    def test_mixed_is_fakeid_level(self):
        key, text, _ = S.classify_verdict(
            True, 0, [self.p("A", 0, 5), self.p("B", 200013), self.p("C", 0, 3)]
        )
        self.assertEqual(key, "target")
        self.assertIn("fakeid", text)

    def test_all_ok(self):
        key, _, _ = S.classify_verdict(True, 0, [self.p("A", 0, 5), self.p("B", 0, 4)])
        self.assertEqual(key, "ok")

    def test_expired_credential_beats_rate_limit(self):
        """searchbiz 都 200003 时，不该被误判成账号级限流。"""
        key, text, _ = S.classify_verdict(False, 200003, [])
        self.assertEqual(key, "credential_expired")
        self.assertIn("200003", text)

    def test_expired_in_probe(self):
        key, _, _ = S.classify_verdict(True, 0, [self.p("A", 200003), self.p("B", 0, 5)])
        self.assertEqual(key, "credential_expired")

    def test_empty_articles_not_counted_as_success(self):
        """ret=0 但 0 篇，不算"这个号是好的"，否则会把账号级误判成 fakeid 级。"""
        key, _, _ = S.classify_verdict(True, 0, [self.p("A", 0, 0), self.p("B", 200013)])
        self.assertNotEqual(key, "target")

    def test_no_probes(self):
        key, _, _ = S.classify_verdict(True, 0, [])
        self.assertEqual(key, "empty")

    def test_search_fail_but_lists_ok(self):
        key, _, _ = S.classify_verdict(False, 1, [self.p("A", 0, 5)])
        self.assertEqual(key, "search_only")

    def test_load_account_names_three_formats(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "wxmp-accounts-xjtu.json").write_text(
                json.dumps(["西安交通大学", "西安交大网信中心"]), "utf-8"
            )
            (root / "wxmp-accounts.json").write_text(
                json.dumps({"accounts": [{"name": "浙江大学", "max": 15}]}), "utf-8"
            )
            names = S.load_account_names("xjtu", root)
            self.assertEqual(names[:2], ["西安交通大学", "西安交大网信中心"])
            self.assertIn("浙江大学", names)          # 全局文件也会合并进来

            (root / "wxmp-accounts-sjtu.json").write_text(
                json.dumps({"上海交大": {"max": 15, "fakeid": "MzA3"}}), "utf-8"
            )
            self.assertEqual(S.load_account_names("sjtu", root)[0], "上海交大")

    def test_load_account_names_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(S.load_account_names("nope", Path(d)), [])


class TestExportCompat(unittest.TestCase):
    """导出的 Cookie 头必须能被 fetch_wechat.py 直接吃掉（两个脚本的接口契约）。"""

    def _load_fw(self):
        import importlib.util

        tmp = tempfile.mkdtemp(prefix="wx-export-test-")
        work = Path(tmp) / "work"
        (work / "wechat").mkdir(parents=True)
        (Path(tmp) / "data").mkdir()
        spec = importlib.util.spec_from_file_location("fw_export", ROOT / "fetch_wechat.py")
        fw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fw)
        fw.WORK_DIR = str(work)
        fw.LEGACY_WORK_DIR = str(work)
        fw.WECHAT_DIR = str(work / "wechat")
        fw.DATA_DIR = str(Path(tmp) / "data")
        return fw, work / "wechat"

    def _store_with(self, root: Path):
        import requests

        store = S.Store(root)
        sess = requests.Session()
        sess.cookies.set("slave_sid", "abc", domain=".qq.com")
        sess.cookies.set("data_ticket", "xyz", domain=".qq.com")
        store.save_cookies(sess)
        store.save_session("846199141", "f" * 32)
        return store

    def test_header_parsed_by_main_pipeline(self):
        fw, wxdir = self._load_fw()
        with tempfile.TemporaryDirectory() as d:
            store = self._store_with(Path(d))
            target = wxdir / "wxmp-cookie-header-pool2.txt"
            info = S.export_cookie_header(store, target)

            self.assertEqual(info["cookies"], 2)
            pairs = fw.parse_cookie_header(target.read_text("utf-8"))
            self.assertEqual(pairs, {"slave_sid": "abc", "data_ticket": "xyz"})

    def test_exported_pool2_recognized_as_second_credential(self):
        fw, wxdir = self._load_fw()
        with tempfile.TemporaryDirectory() as d:
            store = self._store_with(Path(d))
            S.export_cookie_header(store, wxdir / "wxmp-cookie-header.txt")
            S.export_cookie_header(store, wxdir / "wxmp-cookie-header-pool2.txt")

            pool = fw.list_credential_pool("zju")
            self.assertEqual(len(pool), 2)
            descs = [desc for _ck, desc in pool]
            self.assertEqual(descs, ["wxmp-cookie-header.txt", "wxmp-cookie-header-pool2.txt"])
            self.assertEqual(pool[1][0]["slave_sid"], "abc")

    def test_single_slot_readable_via_load_cookies(self):
        fw, wxdir = self._load_fw()
        with tempfile.TemporaryDirectory() as d:
            store = self._store_with(Path(d))
            S.export_cookie_header(store, wxdir / "wxmp-cookie-header.txt")
            ck, path = fw.load_cookies("zju")
            self.assertEqual(ck["data_ticket"], "xyz")
            self.assertTrue(str(path).endswith("wxmp-cookie-header.txt"))

    def test_write_token(self):
        fw, wxdir = self._load_fw()
        with tempfile.TemporaryDirectory() as d:
            store = self._store_with(Path(d))
            info = S.export_cookie_header(store, wxdir / "wxmp-cookie-header.txt", write_token=True)
            self.assertEqual(info["token"], "846199141")
            self.assertEqual((wxdir / "wxmp-token.txt").read_text("utf-8").strip(), "846199141")

    def test_refuses_overwrite_without_force(self):
        fw, wxdir = self._load_fw()
        with tempfile.TemporaryDirectory() as d:
            store = self._store_with(Path(d))
            target = wxdir / "wxmp-cookie-header.txt"
            S.export_cookie_header(store, target)
            with self.assertRaises(S.WxmpError):
                S.export_cookie_header(store, target)          # 第二次被拒
            info = S.export_cookie_header(store, target, force=True)
            self.assertEqual(info["cookies"], 2)
            self.assertTrue(target.with_suffix(".txt.bak").exists())   # 旧文件留档

    def test_no_cookies_raises(self):
        with tempfile.TemporaryDirectory() as d:
            store = S.Store(Path(d))
            with self.assertRaises(S.WxmpError):
                S.export_cookie_header(store, Path(d) / "x.txt")


class TestSogou(unittest.TestCase):
    """搜狗公开索引：结果页解析 + 跳转链拼接（结构照抄 2026-10 的真实页面）。"""

    PAGE = """
<ul>
 <li id="sogou_vr_11002601_box_0" d="ab735a258a90e8e1-6bee54fcbd896b2a">
  <div class="img-box"><a href="/link?url=AAA&amp;type=2&amp;query=%E6%B5%99&amp;token=T"><img></a></div>
  <div class="txt-box">
   <h3><a target="_blank" href="/link?url=AAA&amp;type=2&amp;query=q&amp;token=T"
          id="sogou_vr_11002601_title_0" uigs="article_title_0">浙江大学<em><!--red_beg-->招生<!--red_end--></em>简章</a></h3>
   <p class="txt-info" id="sogou_vr_11002601_summary_0">摘要&ldquo;内容&rdquo;……</p>
   <div class="s-p"><span class="all-time-y2">考博之路</span>
     <span class="s2"><script>document.write(timeConvert('1792590232'))</script></span></div>
  </div>
 </li>
 <li id="sogou_vr_11002601_box_1" d="bb">
  <div class="txt-box">
   <h3><a href="/link?url=BBB&amp;type=2" id="sogou_vr_11002601_title_1"
          uigs="article_title_1">第二条</a></h3>
   <div class="s-p"><span class="all-time-y2">某号</span></div>
  </div>
 </li>
</ul>
"""

    def test_parse_two_items(self):
        items = S.parse_sogou_page(self.PAGE)
        self.assertEqual(len(items), 2)

    def test_highlight_and_html_comment_stripped(self):
        items = S.parse_sogou_page(self.PAGE)
        self.assertEqual(items[0]["title"], "浙江大学招生简章")   # em/注释都不该留下

    def test_account_and_timestamp(self):
        items = S.parse_sogou_page(self.PAGE)
        self.assertEqual(items[0]["account"], "考博之路")
        self.assertEqual(items[0]["timestamp"], 1792590232)

    def test_summary_entity_decoded(self):
        items = S.parse_sogou_page(self.PAGE)
        self.assertIn("“内容”", items[0]["summary"])

    def test_sogou_link_absolute_and_unescaped(self):
        items = S.parse_sogou_page(self.PAGE)
        link = items[0]["sogou_link"]
        self.assertTrue(link.startswith("https://weixin.sogou.com/link?url="))
        self.assertNotIn("&amp;", link)

    def test_real_link_is_empty_before_resolve(self):
        items = S.parse_sogou_page(self.PAGE)
        self.assertEqual(items[0]["link"], "")

    def test_empty_page(self):
        self.assertEqual(S.parse_sogou_page("<html><body>没有结果</body></html>"), [])

    def test_jump_fragments_joined_and_at_removed(self):
        page = ("<script>var url = ''; url += 'https://mp.'; url += 'weixin.qq.c';"
                " url += 'om/s?src=11&'; url += 'timestamp=1792817526';</script>")
        self.assertEqual(S.parse_sogou_jump(page),
                         "https://mp.weixin.qq.com/s?src=11&timestamp=1792817526")

    def test_jump_without_fragments(self):
        self.assertEqual(S.parse_sogou_jump("<html>改版了</html>"), "")

    def test_guard_raises_on_antispider(self):
        with self.assertRaises(S.WxmpError):
            S._sogou_guard('<html>.../antispider/thank.php...</html>')

    def test_guard_passes_normal_page(self):
        S._sogou_guard("<html>normal</html>")     # 不应抛


def make_history_resp(can_continue=1, ret=0) -> dict:
    inner = {
        "list": [
            {   # 一条群发带两条图文 → 要摊平成 2 条
                "comm_msg_info": {"id": 2650906488, "type": 49, "datetime": 1715735985},
                "app_msg_ext_info": {
                    "title": "主图文标题",
                    "digest": "摘要",
                    "author": "作者A",
                    "cover": "http://mmbiz.qpic.cn/a.jpg",
                    "content_url": "http://mp.weixin.qq.com/s?__biz=MzA5&mid=1&idx=1&sn=aa&chksm=bb#rd",
                    "is_multi": 1,
                    "multi_app_msg_item_list": [
                        {"title": "次图文标题", "digest": "", "author": "",
                         "content_url": "http://mp.weixin.qq.com/s?__biz=MzA5&mid=1&idx=2&sn=cc&amp;chksm=dd#rd"},
                    ],
                },
            },
            {   # 非图文消息（文本/语音）→ 要跳过
                "comm_msg_info": {"id": 2650900000, "type": 1, "datetime": 1715735000},
            },
            {   # 另一条单图文
                "comm_msg_info": {"id": 2650904974, "type": 49, "datetime": 1714437985},
                "app_msg_ext_info": {"title": "更早一篇", "digest": "", "author": "",
                                     "content_url": "http://mp.weixin.qq.com/s?__biz=MzA5&mid=2&idx=1&sn=ee#rd"},
            },
        ]
    }
    return {"ret": ret, "errmsg": "ok", "can_msg_continue": can_continue,
            "msg_count": 3, "general_msg_list": json.dumps(inner)}


class TestCurlParse(unittest.TestCase):
    """抓包工具给的 cURL 格式五花八门，三种写法都得认。"""

    URL = ("https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=MzA5NzA5NjU2Ng%3D%3D"
           "&scene=124&uin=MTIzNDU2Nzg%3D&key=abc123&pass_ticket=XY%2Fz9k%3D")

    def test_double_quote_H_form(self):
        got = S.parse_curl(f"""curl '{self.URL}' -H 'Cookie: wap_sid2=AAA; rewardsn=bbb'""")
        self.assertEqual(got["cookie"], "wap_sid2=AAA; rewardsn=bbb")

    def test_single_quote_with_double_quotes(self):
        got = S.parse_curl(f'''curl "{self.URL}" -H "Cookie: wap_sid2=AAA"''')
        self.assertEqual(got["cookie"], "wap_sid2=AAA")

    def test_dash_b_form(self):
        got = S.parse_curl(f"""curl '{self.URL}' -b 'wap_sid2=CCC'""")
        self.assertEqual(got["cookie"], "wap_sid2=CCC")

    def test_url_params_extracted(self):
        got = S.parse_curl(f"""curl '{self.URL}' -H 'Cookie: wap_sid2=AAA'""")
        self.assertEqual(got["uin"], "MTIzNDU2Nzg=")      # 已 unquote
        self.assertEqual(got["key"], "abc123")
        self.assertEqual(got["pass_ticket"], "XY/z9k=")
        self.assertEqual(got["__biz"], "MzA5NzA5NjU2Ng==")

    def test_appmsg_token_extracted(self):
        got = S.parse_curl(f"""curl '{self.URL}&appmsg_token=931_aRav~~' -H 'Cookie: wap_sid2=AAA'""")
        self.assertEqual(got["appmsg_token"], "931_aRav~~")

    def test_no_cookie_returns_no_key(self):
        got = S.parse_curl("curl 'https://mp.weixin.qq.com/s/abc'")
        self.assertNotIn("cookie", got)

    def test_mask_keeps_prefix_only(self):
        masked = S.mask_cookie("wap_sid2=CJmHq6cBEqq8; rewardsn=abc")
        self.assertIn("wap_sid2=CJmHq6…", masked)
        self.assertNotIn("CJmHq6cBEqq8", masked)         # 长值不许全露
        self.assertIn("rewardsn=abc", masked)            # 短值原样


class TestHistory(unittest.TestCase):
    """profile_ext?action=getmsg 解析：这是"拿到某号全部新发布"的唯一入口。"""

    def test_flat_counts_multi_and_skips_non_article(self):
        items, _ = S.parse_history(make_history_resp())
        self.assertEqual(len(items), 3)          # 2（多图文摊平）+ 0（type=1 跳过）+ 1

    def test_hash_rd_stripped_and_entity_decoded(self):
        items, _ = S.parse_history(make_history_resp())
        for it in items:
            self.assertNotIn("#rd", it["link"])
            self.assertNotIn("&amp;", it["link"])
        self.assertIn("chksm=dd", items[1]["link"])   # 次图文保留了 chksm（正文能打开）

    def test_datetime_from_comm_msg_info(self):
        items, _ = S.parse_history(make_history_resp())
        self.assertEqual(items[0]["datetime"], 1715735985)
        self.assertEqual(items[0]["msgid"], 2650906488)

    def test_multi_idx_numbered(self):
        items, _ = S.parse_history(make_history_resp())
        self.assertEqual([i["idx"] for i in items[:2]], [1, 2])

    def test_can_continue_flag(self):
        self.assertTrue(S.parse_history(make_history_resp(can_continue=1))[1])
        self.assertFalse(S.parse_history(make_history_resp(can_continue=0))[1])

    def test_error_ret_stops_pagination(self):
        self.assertFalse(S.parse_history(make_history_resp(can_continue=1, ret=-3))[1])

    def test_broken_json_is_empty_not_crash(self):
        items, cont = S.parse_history({"ret": 0, "general_msg_list": "{not json"})
        self.assertEqual(items, [])
        self.assertFalse(cont)

    def test_empty_list(self):
        items, cont = S.parse_history({"ret": 0, "general_msg_list": json.dumps({"list": []})})
        self.assertEqual(items, [])
        self.assertFalse(cont)


class TestResolveBiz(unittest.TestCase):
    """号名 → biz：fakeid 与 __biz 同值，直接复用已有缓存。"""

    def _cache(self, d, payload):
        p = Path(d) / "fakeid.json"
        p.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        return p

    def test_exact_key(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._cache(d, {"浙江大学": {"fakeid": "MzA5", "nickname": "浙江大学"}})
            self.assertEqual(S.resolve_biz("浙江大学", None, c)[0], "MzA5")

    def test_exact_nickname_case_insensitive(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._cache(d, {"xjtu": {"fakeid": "MzE", "nickname": "西安交通大学"}})
            self.assertEqual(S.resolve_biz("西安交通大学", None, c)[0], "MzE")

    def test_fuzzy_single_hit(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._cache(d, {"a": {"fakeid": "MzA", "nickname": "浙江大学本科招生"},
                                "b": {"fakeid": "MzB", "nickname": "复旦大学"}})
            self.assertEqual(S.resolve_biz("本科招生", None, c)[0], "MzA")

    def test_ambiguous_raises(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._cache(d, {"a": {"fakeid": "MzA", "nickname": "浙江大学"},
                                "b": {"fakeid": "MzB", "nickname": "浙江大学cc98"}})
            with self.assertRaises(S.WxmpError):
                S.resolve_biz("浙江", None, c)

    def test_biz_passthrough(self):
        self.assertEqual(S.resolve_biz(None, "MzX", Path("nope.json"))[0], "MzX")

    def test_missing_cache_raises(self):
        with self.assertRaises(S.WxmpError):
            S.resolve_biz("浙江大学", None, Path("no_such_dir/fakeid.json"))


class TestProbeVerdict(unittest.TestCase):
    """端点探针的判读：这几条区分决定了「是删了还是要凭据还是要换方案」。"""

    def test_404_is_real_removal(self):
        v, _ = S.probe_verdict({"http": 404, "size": 0})
        self.assertEqual(v, "路由不存在")

    def test_200003_means_needs_credentials(self):
        v, meaning = S.probe_verdict({"http": 200, "json": True, "ret": 200003})
        self.assertEqual(v, "要凭据")
        self.assertIn("不是废弃", meaning)

    def test_minus_three_means_needs_wap_session(self):
        v, meaning = S.probe_verdict({"http": 200, "json": True, "ret": -3, "cookie_count": 0})
        self.assertEqual(v, "要 session")
        self.assertIn("wap_sid2", meaning)
        self.assertIn("0", meaning)          # cookie_count 要透出来

    def test_200013_is_tightening_not_removal(self):
        v, meaning = S.probe_verdict({"http": 200, "json": True, "ret": 200013})
        self.assertEqual(v, "被限/收紧")
        self.assertIn("不是接口下线", meaning)

    def test_ret_zero_is_alive(self):
        self.assertEqual(S.probe_verdict({"http": 200, "json": True, "ret": 0})[0], "存活")

    def test_10004_still_alive(self):
        self.assertEqual(S.probe_verdict({"http": 200, "json": True, "ret": 10004})[0], "存活")

    def test_html_verify_wall(self):
        self.assertEqual(S.probe_verdict({"http": 200, "title": "验证"})[0], "环境校验墙")

    def test_html_error_page(self):
        self.assertEqual(S.probe_verdict({"http": 200, "title": "错误"})[0], "参数错误页")

    def test_probe_targets_cover_key_endpoints(self):
        names = {n for n, _, _ in S.PROBE_TARGETS}
        for required in ("appmsg", "appmsgpublish", "profile_ext.getmsg", "appmsgalbum"):
            self.assertIn(required, names)


class TestJitter(unittest.TestCase):
    def test_jitter_within_bounds(self):
        t0 = time.perf_counter()
        S.jitter_sleep(100)
        elapsed = time.perf_counter() - t0
        self.assertGreaterEqual(elapsed, 0.045)   # 100ms * 0.5 下限
        self.assertLessEqual(elapsed, 0.25)       # 100ms * 1.5 上限 + 余量


class TestCaptureParams(unittest.TestCase):
    """抓包落下的参数文件：history 要能自动拾取，且不能被脏数据带偏。"""

    def _write(self, tmp: Path, payload) -> Path:
        p = tmp / "wxmp-wap-params.json"
        p.write_text(payload if isinstance(payload, str) else json.dumps(payload), "utf-8")
        return p

    def test_reads_known_keys(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(Path(d), {"uin": "MTIz=", "key": "k", "pass_ticket": "a/b=",
                                      "appmsg_token": "931_x~~", "__biz": "MzA5=="})
            got = S.load_capture_params(p)
        self.assertEqual(got["uin"], "MTIz=")              # 已 unquote
        self.assertEqual(got["appmsg_token"], "931_x~~")
        self.assertEqual(got["__biz"], "MzA5==")

    def test_drops_unknown_and_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(Path(d), {"uin": "u", "cookie": "wap_sid2=xxx", "key": "  "})
            self.assertEqual(S.load_capture_params(p), {"uin": "u"})

    def test_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(S.load_capture_params(Path(d) / "nope.json"), {})

    def test_broken_json_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(Path(d), "{not json")
            self.assertEqual(S.load_capture_params(p), {})

    def test_non_dict_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(Path(d), "[1,2,3]")
            self.assertEqual(S.load_capture_params(p), {})


class TestHistoryRequest(unittest.TestCase):
    """getmsg 的请求参数：appmsg_token 必须真的带上（旧版接口靠它）。"""

    def test_appmsg_token_and_uin_in_params(self):
        seen = {}

        class FakeResp:
            content = b"{}"

            @staticmethod
            def json():
                return {"ret": 0, "general_msg_list": '{"list":[]}'}

        def fake_get(url, params=None, headers=None, **kw):
            seen.update(params or {})
            seen["_cookie"] = (headers or {}).get("Cookie", "")
            return FakeResp()

        import requests
        orig = requests.get
        requests.get = fake_get
        try:
            S.fetch_history_page("MzA5==", "Cookie: wap_sid2=X==", 10, 10,
                                 uin="MTIz", key="k", pass_ticket="t", appmsg_token="931_a~~")
        finally:
            requests.get = orig

        self.assertEqual(seen["appmsg_token"], "931_a~~")
        self.assertEqual(seen["uin"], "MTIz")
        self.assertEqual(seen["offset"], 10)
        self.assertEqual(seen["__biz"], "MzA5==")
        self.assertNotIn("Cookie: ", seen["_cookie"])      # `Cookie: ` 前缀要去掉
        self.assertIn("wap_sid2=X==", seen["_cookie"])


class TestWatch(unittest.TestCase):
    """增量轮询：只跑第一页 + 已见集合去重，是"不漏篇"的工程化关键。"""

    ITEMS = [
        {"title": "A", "link": "https://mp.weixin.qq.com/s/A", "datetime": 1760000000},
        {"title": "B", "link": "https://mp.weixin.qq.com/s/B", "datetime": 1759990000},
        {"title": "坏数据", "link": "", "datetime": 0},
    ]

    def test_diff_new_keeps_order_and_skips_empty(self):
        got = S.diff_new({"https://mp.weixin.qq.com/s/B"}, self.ITEMS)
        self.assertEqual([x["title"] for x in got], ["A"])

    def test_seen_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "seen.json"
            S.save_seen(p, {"a", "b"})
            self.assertEqual(S.load_seen(p), {"a", "b"})

    def test_seen_broken_file_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "seen.json"
            p.write_text("{bad", "utf-8")
            self.assertEqual(S.load_seen(p), set())

    def test_watch_writes_only_new(self):
        calls = {"n": 0}

        def fake_list_history(*a, **kw):
            calls["n"] += 1
            self.assertEqual(a[9], 1)            # 第 10 个位置参数是 max_pages，只抓第一页
            return list(self.ITEMS)

        orig = S.list_history
        S.list_history = fake_list_history
        try:
            with tempfile.TemporaryDirectory() as d:
                state = Path(d) / "seen.json"
                out = Path(d) / "new.jsonl"
                n1 = S.watch_history("MzA5==", "Cookie: x", cycles=1, state_path=state, output=out)
                n2 = S.watch_history("MzA5==", "Cookie: x", cycles=1, state_path=state, output=out)
                lines = out.read_text("utf-8").strip().split("\n")
        finally:
            S.list_history = orig

        self.assertEqual(n1, 2)                  # 两条有链接的
        self.assertEqual(n2, 0)                  # 第二轮全是见过的
        self.assertEqual(len(lines), 2)
        self.assertIn("seen_at", json.loads(lines[0]))
        self.assertEqual(calls["n"], 2)


class TestHistoryWithPool(unittest.TestCase):
    """方案 A 的接线：history 应该能从一个号池里选号、失败换号、并把结果记成健康度。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        for name, cookie in (("a1", "wap_sid2=AAA=="), ("b1", "wap_sid2=BBB==")):
            (self.dir / f"{name}.txt").write_text(f"Cookie: {cookie}\n", "utf-8")
        self.orig = S.fetch_history_page

    def tearDown(self):
        S.fetch_history_page = self.orig
        self._tmp.cleanup()

    def _patch(self, fn):
        S.fetch_history_page = fn

    def test_uses_pool_and_reports_success(self):
        seen = []

        def fake(biz, cookie, *a, **kw):
            seen.append(cookie)
            return make_history_resp(can_continue=0)

        self._patch(fake)
        rows = S.list_history("MzA5==", "", 5, None, "", "", "", "", 1, 1,
                              pool=P.CredentialPool(self.dir, quota=5))
        self.assertTrue(rows)
        self.assertEqual(len(seen), 1)              # 一次任务只用一个号
        self.assertIn("wap_sid2=", seen[0])
        rows_state = P.CredentialPool(self.dir, quota=5).status()
        self.assertEqual(sum(r["ok"] for r in rows_state), 1)

    def test_swaps_to_another_cred_on_failure(self):
        seen = []

        def fake(biz, cookie, *a, **kw):
            seen.append(cookie)
            if "AAA==" in cookie:
                return make_history_resp(ret=-3)
            return make_history_resp(can_continue=0)

        self._patch(fake)
        rows = S.list_history("MzA5==", "", 5, None, "", "", "", "", 1, 1,
                              pool=P.CredentialPool(self.dir, quota=5))
        self.assertTrue(rows)
        self.assertEqual(len(seen), 2)              # 换了一次号
        self.assertIn("BBB==", seen[1])
        st = {r["name"]: r for r in P.CredentialPool(self.dir, quota=5).status()}
        self.assertEqual(st["a1"]["state"], "cooling")   # 坏号进了冷却
        self.assertEqual(st["b1"]["state"], "ready")

    def test_all_creds_failing_raises_clear_error(self):
        def fake(biz, cookie, *a, **kw):
            return make_history_resp(ret=-3)

        self._patch(fake)
        with self.assertRaises(S.WxmpError) as cm:
            S.list_history("MzA5==", "", 5, None, "", "", "", "", 1, 1,
                           pool=P.CredentialPool(self.dir, quota=5), max_swaps=1)
        self.assertIn("ret=-3", str(cm.exception))
        self.assertIn("已试 2 个号", str(cm.exception))

    def test_cred_params_override_cli_args(self):
        (self.dir / "a1.json").write_text(
            json.dumps({"uin": "FROM_JSON", "appmsg_token": "931_x~~"}), "utf-8")
        got = {}

        def fake(biz, cookie, offset, count, uin, key, ticket, token, *a, **kw):
            got.update(uin=uin, token=token)
            return make_history_resp(can_continue=0)

        self._patch(fake)
        S.list_history("MzA5==", "", 5, None, "FROM_CLI", "", "", "", 1, 1,
                       pool=P.CredentialPool(self.dir, quota=5))
        self.assertEqual(got["uin"], "FROM_JSON")
        self.assertEqual(got["token"], "931_x~~")


if __name__ == "__main__":
    unittest.main(verbosity=2)
