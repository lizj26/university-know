#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线验证 refresh.py 的写入保护逻辑:不联网,只打桩 fetch_list / fetch_detail。

用法:
    python tests/pipeline_test.py
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)          # tests/ 在根下一层
# 临时夹具目录：放在系统临时区，跑完由本脚本自己清掉，
# 不在项目里留 _pipe_test/（早先版本留在项目根下，每跑一次就多一堆残留 json）。
import shutil
import tempfile

TMP = os.path.join(tempfile.mkdtemp(prefix="thu-pipe-test-"), "offline")
os.makedirs(TMP, exist_ok=True)
_cleanup_done = False


def cleanup_tmp():
    """删掉本次运行的临时目录（幂等）。"""
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True
    shutil.rmtree(os.path.dirname(TMP), ignore_errors=True)

spec = importlib.util.spec_from_file_location("refresh", os.path.join(PROJ, "refresh.py"))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)
ORIG_FETCH_DETAIL = R.fetch_detail        # 打桩前先留住真函数
R.DATA_DIR = TMP
R.force_utf8_stdout()


def load_cloud_update():
    """加载 cloud_update.py（只用来测它的日志翻译函数，不触发任何联网/构建）。"""
    s = importlib.util.spec_from_file_location("cloud_update", os.path.join(PROJ, "cloud_update.py"))
    mod = importlib.util.module_from_spec(s)
    s.loader.exec_module(mod)
    return mod

DATA = os.path.join(TMP, "t.json")
results = []


def _iso(days_ago, hh=0, mm=0):
    """距离现在 days_ago 天的 ISO 时间。refresh.py 现在会清除发布超过 90 天的条目,
    所以夹具日期必须相对当前时间生成,不能写死。"""
    from datetime import datetime, timedelta
    t = datetime.now() - timedelta(days=days_ago)
    return t.strftime("%Y-%m-%dT") + "%02d:%02d:00+08:00" % (hh, mm)


def mk_item(i, published=None):
    return {
        "id": i, "title": "标题 " + i, "source_name": "来源A", "source_id": "weixinzs_1",
        "published_at": published or _iso(20), "url": "https://example.com/" + i,
        "category": "校园动态", "intent_group": "information",
    }


def make_fetch(total, fail_pages=(), with_details=True):
    def fetch(campus, page):
        if page in fail_pages:
            raise RuntimeError("模拟第 %d 页失败" % page)
        start = (page - 1) * R.PAGE_SIZE
        n = max(0, min(R.PAGE_SIZE, total - start))
        return {
            "total_all": total,
            "last_sync": _iso(20),
            "interval_minutes": 5,
            "items": [mk_item("id%d" % (start + i)) for i in range(n)],
            "sources": [{"name": "来源A", "source_group": "wechat", "count": total},
                        {"name": "从来没出现过", "source_group": "official", "count": 99}],
            "groups": [{"id": "wechat", "sources": 9, "count": 999}],
            "category_totals": [{"name": "假的", "count": 999}],
            "intent_totals": [{"intent_group": "假的", "count": 999}],
        }
    return fetch


def payload(name="t"):
    with open(os.path.join(TMP, name + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def fresh():
    for name in ("t", "zju"):
        for suffix in (".json", ".json.bak", ".json.tmp"):
            path = os.path.join(TMP, name + suffix)
            if os.path.exists(path):
                os.remove(path)


def case(name, fn):
    fresh()
    try:
        results.append((name, fn()))
    except Exception as exc:  # noqa: BLE001
        results.append((name, "RAISED %s: %s" % (type(exc).__name__, exc)))


# 1) 完整抓取应当写入,并重算统计
def c1():
    R.fetch_list = make_fetch(100)
    R.fetch_detail = lambda campus, i: {"su": "摘要"}
    p = R.scrape_campus("t", 0, 4, True, False)
    d = payload()
    assert p is not None, "完整抓取却拒绝写入"
    assert len(d["items"]) == 100, len(d["items"])
    assert d["fetched"] == 100 and d["truncated"] is False, (d["fetched"], d["truncated"])
    assert d["fetch_warnings"] == [], d["fetch_warnings"]
    names = [s["name"] for s in d["sources"]]
    assert names == ["来源A"], names                       # 0 条的来源被剔除
    assert d["sources"][0]["count"] == 100, d["sources"][0]
    assert d["groups"] == [{"id": "wechat", "sources": 1, "count": 100}], d["groups"]
    assert d["category_totals"] == [{"name": "校园动态", "count": 100}], d["category_totals"]
    assert d["intent_totals"] == [{"intent_group": "information", "count": 100}], d["intent_totals"]
    return "写入 100 条,统计已按 items 重算,0 条来源被剔除"


# 2) 有一页失败、缺口 > 2%:默认必须拒绝写入并保留旧数据
def c2():
    R.fetch_list = make_fetch(100)
    R.fetch_detail = lambda campus, i: {"su": "摘要"}
    R.scrape_campus("t", 0, 4, True, False)
    before = payload()
    R.fetch_list = make_fetch(100, fail_pages=(3,))
    p = R.scrape_campus("t", 0, 4, True, False)
    after = payload()
    assert p is None, "缺口 30/100 却仍然写入了"
    assert after == before, "旧数据被覆盖"
    return "拒绝写入并保留 %d 条旧数据" % len(after["items"])


# 3) 缺口 <= 2% 的正常抖动可以写
def c3():
    R.fetch_list = make_fetch(100)
    R.fetch_detail = lambda campus, i: {"su": "摘要"}
    R.fetch_list = make_fetch(1001)          # 34 页,最后不足 2%
    p = R.scrape_campus("t", 0, 4, True, False)
    assert p is not None
    return "1001 条完整写入"


# 4) total_all = 0:必须抛错而不是把好数据清空
def c4():
    R.fetch_list = make_fetch(100)
    R.fetch_detail = lambda campus, i: {"su": "摘要"}
    R.scrape_campus("t", 0, 4, True, False)

    def empty(campus, page):
        return {"total_all": 0, "items": [], "sources": []}
    R.fetch_list = empty
    try:
        R.scrape_campus("t", 0, 4, True, False)
    except RuntimeError as exc:
        assert "total_all" in str(exc), exc
        assert len(payload()["items"]) == 100, "旧数据被清空"
        return "抛 RuntimeError 并保留 100 条旧数据"
    raise AssertionError("total_all=0 没有报错")


# 5) --limit 必须写出自洽的文件
def c5():
    R.fetch_list = make_fetch(100)
    R.fetch_detail = lambda campus, i: {"su": "摘要"}
    p = R.scrape_campus("t", 60, 4, True, False)
    d = payload()
    assert p is not None
    assert d["total_all"] == 100 and d["fetched"] == 60 and d["truncated"] is True, \
        (d["total_all"], d["fetched"], d["truncated"])
    return "--limit 60 → items=60, fetched=60, total_all=100, truncated=True"


# 6) 详情失败时沿用上次结果,不清空摘要
def c6():
    R.fetch_list = make_fetch(100)
    R.fetch_detail = lambda campus, i: {"su": "上次的摘要"}
    R.scrape_campus("t", 0, 4, True, False)
    assert payload()["items"][0]["dt"]["su"] == "上次的摘要"
    R.fetch_list = make_fetch(100)
    R.fetch_detail = lambda campus, i: None          # 这次全部失败
    p = R.scrape_campus("t", 0, 4, True, False)
    d = payload()
    assert p is not None
    assert d["items"][0]["dt"]["su"] == "上次的摘要", d["items"][0].get("dt")
    assert any("沿用上次" in w for w in d["fetch_warnings"]), d["fetch_warnings"]
    assert os.path.exists(DATA + ".bak"), "覆盖写入前没有留下备份"
    with open(DATA + ".bak", encoding="utf-8") as fh:
        assert len(json.load(fh)["items"]) == 100, "备份内容不对"
    return "100 条详情沿用上次结果,warnings=%s,已生成备份" % d["fetch_warnings"]


# 7) --allow-partial 时才允许写残缺数据,并写明缺口
def c7():
    R.fetch_list = make_fetch(100, fail_pages=(3,))
    R.fetch_detail = lambda campus, i: {"su": "摘要"}
    p = R.scrape_campus("t", 0, 4, True, True)
    d = payload()
    assert p is not None, "allow_partial=True 仍被拒绝"
    assert any("比目标少" in w for w in d["fetch_warnings"]), d["fetch_warnings"]
    assert any("列表页抓取失败" in w for w in d["fetch_warnings"]), d["fetch_warnings"]
    return "写入 %d 条并记录 warnings=%s" % (len(d["items"]), d["fetch_warnings"])


# 8) 明显缩水(少于现有 90%)且未 allow_partial 时拒绝
def c8():
    R.fetch_list = make_fetch(1000)
    R.fetch_detail = lambda campus, i: {"su": "摘要"}
    R.scrape_campus("t", 0, 4, True, False)
    assert len(payload()["items"]) == 1000
    R.fetch_list = make_fetch(200)      # 上游只剩 200 条
    p = R.scrape_campus("t", 0, 4, True, False, False)   # 关掉只增不减才拦
    assert p is None, "数据从 1000 缩到 200 却写了进去"
    assert len(payload()["items"]) == 1000
    return "数据缩水时拒绝写入,保留 1000 条"


# 16) 只增不减:上游变少时旧条目继续保留(THU 走的就是这条路)
def c16():
    R.fetch_list = make_fetch(300)
    R.fetch_detail = lambda campus, i: {"su": "摘要"}
    R.scrape_campus("t", 0, 4, True, False, True)
    first = payload()
    assert len(first["items"]) == 300, len(first["items"])
    assert first["append_only"] is True
    assert first["last_import"]["added"] == 300, first["last_import"]
    assert first["sources"] and first["sources"][0]["status"] == "ok", first["sources"][0]

    # 上游这轮只给 120 条:旧条目必须全部留下,状态变成「无新内容」
    R.fetch_list = make_fetch(120)
    R.scrape_campus("t", 0, 4, True, False, True)
    d = payload()
    assert len(d["items"]) == 300, len(d["items"])          # 只增不减
    assert d["last_import"]["added"] == 0, d["last_import"]
    assert d["last_import"]["updated"] == 120, d["last_import"]
    assert d["sources"][0]["status"] == "no_recent", d["sources"][0]
    assert d["source_registry"], "没有写出来源注册表"
    return "上游缩到 120 条时仍保留 300 条,状态转为 no_recent"


# 9) 非 dict 的详情响应不再中断整个校区抓取
def c9():
    R.fetch_list = make_fetch(60)
    R.fetch_detail = lambda campus, i: {"su": "摘要"}
    p = R.scrape_campus("t", 0, 4, True, False)
    assert p is not None
    original = R.get_json
    R.get_json = lambda path, retries=3: []
    try:
        got = ORIG_FETCH_DETAIL("thu", "x")
    finally:
        R.get_json = original
    assert got is None, got
    return "fetch_detail 对非 dict 响应返回 None"


# 10) 导入自建信息源:合并 / 替换 / 字段补全 / 统计重算
def c10():
    src = os.path.join(TMP, "incoming.json")
    with open(src, "w", encoding="utf-8") as fh:
        json.dump([
            {"i": "a1", "t": "浙大讲座", "s": "浙大官微", "si": "zjuer_1", "d": _iso(6, 10),
             "u": "https://example.com/a1", "c": "讲座活动", "g": "activity",
             "dt": {"sg": "wechat", "su": "摘要"}},
            {"id": "a2", "title": "缺分类的通知", "s": "浙大官网", "si": "zju_site",
             "d": _iso(5, 9), "u": "https://example.com/a2"},
        ], fh, ensure_ascii=False)

    p = R.import_items("zju", src)
    d = payload("zju")
    assert p is not None
    assert [it["i"] for it in d["items"]] == ["a2", "a1"], [it["i"] for it in d["items"]]   # 按时间倒序
    assert d["items"][0]["c"] == R.DEFAULT_CATEGORY, d["items"][0]["c"]                     # 缺字段给默认值
    assert d["campus_name"] == "浙大知道", d["campus_name"]
    assert d["total_all"] == 2 and d["fetched"] == 2, (d["total_all"], d["fetched"])
    names = sorted(s["name"] for s in d["sources"])
    assert names == sorted(["浙大官微", "浙大官网"]), names
    assert d["groups"] == [{"id": "wechat", "sources": 1, "count": 1},
                           {"id": "official", "sources": 1, "count": 1}], d["groups"]
    assert any("没有 category" in w for w in d["fetch_warnings"]), d["fetch_warnings"]
    return "导入 2 条,字段补全 + 统计重算 %s" % d["groups"]


# 11) 再次导入:按 id 合并,更新已有、追加新的
def c11():
    first = os.path.join(TMP, "incoming.json")
    with open(first, "w", encoding="utf-8") as fh:
        json.dump([
            {"i": "a1", "t": "浙大讲座", "s": "浙大官微", "si": "zjuer_1", "d": _iso(6, 10),
             "u": "https://example.com/a1", "c": "讲座活动", "g": "activity", "dt": {"sg": "wechat"}},
            {"i": "a2", "t": "缺分类的通知", "s": "浙大官网", "si": "zju_site",
             "d": _iso(5, 9), "u": "https://example.com/a2"},
        ], fh, ensure_ascii=False)
    R.import_items("zju", first)
    assert len(payload("zju")["items"]) == 2

    src = os.path.join(TMP, "incoming2.json")
    with open(src, "w", encoding="utf-8") as fh:
        json.dump({"items": [
            {"i": "a1", "t": "浙大讲座(改)", "s": "浙大官微", "d": _iso(4, 10),
             "u": "https://example.com/a1", "c": "讲座活动", "g": "activity", "dt": {"sg": "wechat"}},
            {"i": "a3", "t": "新增的通知", "s": "浙大官网", "si": "zju_site", "d": _iso(3, 10),
             "u": "https://example.com/a3", "c": "校园动态", "g": "information", "dt": {"sg": "official"}},
        ]}, fh, ensure_ascii=False)
    R.import_items("zju", src)
    d = payload("zju")
    assert len(d["items"]) == 3, len(d["items"])
    assert d["items"][0]["i"] == "a3", d["items"][0]["i"]
    a1 = [it for it in d["items"] if it["i"] == "a1"][0]
    assert a1["t"] == "浙大讲座(改)", a1["t"]
    assert any("新增 1 条 / 更新 1 条" in w for w in d["fetch_warnings"]), d["fetch_warnings"]
    return "合并正确:共 3 条,更新 1 新增 1"


# 12) --replace:整校区换成新的一份
def c12():
    first = os.path.join(TMP, "incoming3.json")
    with open(first, "w", encoding="utf-8") as fh:
        json.dump([{"i": "b1", "t": "第一条", "s": "交大官微", "si": "weixinzs_9",
                    "d": _iso(2, 10), "u": "https://example.com/b1",
                    "c": "社团公益", "g": "activity", "dt": {"sg": "wechat"}}], fh, ensure_ascii=False)
    R.import_items("zju", first)
    assert len(payload("zju")["items"]) == 1

    second = os.path.join(TMP, "incoming4.json")
    with open(second, "w", encoding="utf-8") as fh:
        json.dump([{"i": "c1", "t": "替换后的唯一一条", "s": "浙大官网", "si": "zju_site",
                    "d": _iso(1, 10), "u": "https://example.com/c1",
                    "c": "校园动态", "g": "information", "dt": {"sg": "official"}}], fh, ensure_ascii=False)
    R.import_items("zju", second, replace=True)
    d = payload("zju")
    assert [it["i"] for it in d["items"]] == ["c1"], [it["i"] for it in d["items"]]
    assert d["total_all"] == 1 and d["truncated"] is False
    assert os.path.exists(os.path.join(TMP, "zju.json.bak")), "替换前没有备份"
    with open(os.path.join(TMP, "zju.json.bak"), encoding="utf-8") as fh:
        assert [it["i"] for it in json.load(fh)["items"]] == ["b1"], "备份内容不对"
    return "整校区替换为 1 条,旧数据已备份"


# 13) 导入的坏输入要报错而不是写坏文件
def c13():
    R.import_items("zju", os.path.join(TMP, "incoming3.json"), replace=True)
    good = payload("zju")

    bad = os.path.join(TMP, "bad.json")
    with open(bad, "w", encoding="utf-8") as fh:
        json.dump([{"t": "没有 id"}], fh, ensure_ascii=False)
    try:
        R.import_items("zju", bad)
        raise AssertionError("缺 id 的条目没有被拒绝")
    except RuntimeError as exc:
        assert "i(id)" in str(exc), exc

    with open(bad, "w", encoding="utf-8") as fh:
        json.dump({"items": "不是数组"}, fh, ensure_ascii=False)
    try:
        R.import_items("zju", bad)
        raise AssertionError("非数组没有被拒绝")
    except RuntimeError as exc:
        assert "数组" in str(exc), exc

    try:
        R.import_items("nope", bad)
        raise AssertionError("未知校区没有被拒绝")
    except RuntimeError as exc:
        assert "未知校区" in str(exc), exc

    assert payload("zju") == good, "失败的导入改动了数据文件"
    return "缺 id / 非数组 / 未知校区都被拒绝,数据未被改动"


# 14) 只增不减 + 来源注册表并入 sources
def c14():
    def mk(i, t):
        return {"i": i, "t": t, "s": "来源A", "si": "src_a", "d": _iso(6, 10),
                "u": "https://example.com/" + i, "c": "校园动态", "g": "information", "dt": {"sg": "official"}}

    round1 = os.path.join(TMP, "round1.json")
    with open(round1, "w", encoding="utf-8") as fh:
        json.dump({
            "items": [mk("a1", "第一轮第一条"), mk("a2", "第一轮第二条")],
            "wechat_accounts": ["某社团"],
            "source_registry": {
                "src_a": {"name": "来源A", "group": "official", "status": "ok", "count": 2, "new": 2,
                          "url": "https://a.example.com", "source_id": "src_a",
                          "last_checked": "2026-10-09T10:00:00+08:00"},
                "wx:某社团": {"name": "某社团", "group": "wechat", "status": "pending", "count": 0, "new": 0},
            },
        }, fh, ensure_ascii=False)
    R.import_items("zju", round1)
    d = payload("zju")
    assert len(d["items"]) == 2, len(d["items"])
    assert d["append_only"] is True
    assert d["last_import"]["added"] == 2 and d["last_import"]["items"] == 2, d["last_import"]
    srcs = {s["name"]: s for s in d["sources"]}
    assert srcs["来源A"]["status"] == "ok" and srcs["来源A"]["count"] == 2, srcs["来源A"]
    assert srcs["来源A"]["url"] == "https://a.example.com", srcs["来源A"]
    assert srcs["某社团"]["status"] == "pending" and srcs["某社团"]["count"] == 0, srcs["某社团"]
    assert d["groups"] == [{"id": "official", "sources": 1, "count": 2}], d["groups"]

    # 第二轮只带来 1 条新内容:旧条目必须保留
    round2 = os.path.join(TMP, "round2.json")
    with open(round2, "w", encoding="utf-8") as fh:
        json.dump({"items": [mk("a3", "第二轮新增")],
                   "source_registry": {"src_a": {"name": "来源A", "group": "official", "status": "ok",
                                                 "count": 1, "new": 1, "url": "https://a.example.com"}}},
                  fh, ensure_ascii=False)
    R.import_items("zju", round2)
    d = payload("zju")
    assert len(d["items"]) == 3, len(d["items"])            # 只增不减
    assert d["last_import"]["added"] == 1 and d["last_import"]["updated"] == 0, d["last_import"]
    srcs = {s["name"]: s for s in d["sources"]}
    assert srcs["来源A"]["count"] == 3, srcs["来源A"]        # 按合并后的 items 现算
    assert srcs["某社团"]["count"] == 0, srcs["某社团"]      # 注册表里的待接入条目仍在
    return "两轮合并后 3 条(旧条目未丢),来源状态已并入 sources"


# 15) 后一轮导入没带注册表时,沿用已有来源状态
def c15():
    withreg = os.path.join(TMP, "withreg.json")
    with open(withreg, "w", encoding="utf-8") as fh:
        json.dump({
            "items": [{"i": "a5", "t": "带注册表的一轮", "s": "来源A", "si": "src_a",
                       "d": _iso(6, 10), "u": "https://example.com/a5",
                       "c": "校园动态", "g": "information", "dt": {"sg": "official"}}],
            "source_registry": {"src_a": {"name": "来源A", "group": "official", "status": "ok",
                                          "url": "https://a.example.com", "source_id": "src_a",
                                          "last_checked": "2026-10-09T10:00:00+08:00"}},
        }, fh, ensure_ascii=False)
    R.import_items("zju", withreg)

    plain = os.path.join(TMP, "plain.json")
    with open(plain, "w", encoding="utf-8") as fh:
        json.dump([{"i": "a4", "t": "没有注册表的导入", "s": "来源A", "si": "src_a",
                    "d": _iso(3, 10),
                    "u": "https://example.com/a4", "c": "校园动态", "g": "information",
                    "dt": {"sg": "official"}}], fh, ensure_ascii=False)
    R.import_items("zju", plain)
    d = payload("zju")
    srcs = {s["name"]: s for s in d["sources"]}
    assert srcs["来源A"]["status"] == "ok", srcs["来源A"]
    assert srcs["来源A"]["url"] == "https://a.example.com", srcs["来源A"]
    assert srcs["来源A"]["count"] == 2, srcs["来源A"]
    assert len(d["items"]) == 2, len(d["items"])
    return "注册表缺失时沿用上一轮状态(仍为 ok)与链接"


# 17) 同名官网栏目与公众号必须是两个来源(按来源标识聚合,不按显示名)
def c17():
    src = os.path.join(TMP, "collide.json")
    with open(src, "w", encoding="utf-8") as fh:
        json.dump({
            "items": [
                {"i": "s1", "t": "图书馆网站通知", "s": "浙江大学图书馆", "si": "zju_lib",
                 "d": _iso(6, 10), "u": "https://example.com/s1", "c": "校园服务",
                 "g": "information", "dt": {"sg": "official"}},
                {"i": "s2", "t": "图书馆公众号推文", "s": "浙江大学图书馆", "si": "wx:浙江大学图书馆",
                 "d": _iso(5, 10), "u": "https://example.com/s2", "c": "讲座活动",
                 "g": "activity", "dt": {"sg": "wechat"}},
            ],
            "source_registry": {
                "zju_lib": {"name": "浙江大学图书馆", "group": "official", "status": "no_recent",
                            "count": 1, "new": 0},
                "wx:浙江大学图书馆": {"name": "浙江大学图书馆", "group": "wechat", "status": "ok",
                                      "count": 1, "new": 1},
            },
        }, fh, ensure_ascii=False)
    R.import_items("zju", src)
    d = payload("zju")
    srcs = {s["key"]: s for s in d["sources"]}
    assert len(srcs) == 2, list(srcs)
    assert srcs["zju_lib"]["source_group"] == "official" and srcs["zju_lib"]["count"] == 1, srcs["zju_lib"]
    assert srcs["wx:浙江大学图书馆"]["source_group"] == "wechat", srcs["wx:浙江大学图书馆"]
    assert srcs["wx:浙江大学图书馆"]["count"] == 1, srcs["wx:浙江大学图书馆"]
    assert srcs["zju_lib"]["status"] == "no_recent", srcs["zju_lib"]
    groups = {g["id"]: g for g in d["groups"]}
    assert groups["official"]["count"] == 1 and groups["wechat"]["count"] == 1, d["groups"]
    return "同名来源拆成 official / wechat 两条,计数与状态各自正确"


# 18) 限流提示必须分粒度：账号级只能换账号（等待无效），fakeid 级会自动跳过。
#     旧版一律回"等数小时至 1 天解禁"，对账号级限流是**错的**，会让人陷入
#     "等一天→重跑→还是 0 篇→再等一天" 的死循环。
def c18():
    CU = load_cloud_update()

    def log_with(seg):
        path = os.path.join(TMP, "run.log")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("== 公众号 fetch_wechat.py ==\n" + seg + "\n")
        return CU.wechat_status_from_log(path)

    acct = log_with("连续 3 个号都取回 0 篇 …… 判定为【账号级限流】。200013 freq control")
    assert "换登录账号" in acct, acct
    assert "等数小时" not in acct, acct          # 旧措辞必须消失
    assert "pool2" in acct, acct                  # 要给出可执行动作

    fake = log_with("判定为【fakeid 级限流】，只坏在这几个号上。200013 freq control")
    assert "单号限流" in fake or "fakeid 级" in fake, fake
    assert "自动跳过" in fake, fake
    assert "换登录账号" not in fake, fake         # 单号限流不该让人去换账号

    return "账号级→建议换账号；单号级→说明会自动跳过"


for name, fn in [
    ("完整抓取写入 + 统计重算", c1),
    ("缺口过大时拒绝写入", c2),
    ("2% 以内抖动允许写入", c3),
    ("total_all=0 时报错保数据", c4),
    ("--limit 写出自洽文件", c5),
    ("详情失败沿用上次结果", c6),
    ("--allow-partial 才写残缺数据", c7),
    ("数据明显缩水时拒绝写入", c8),
    ("非 dict 详情响应不中断", c9),
    ("导入自建信息源 + 字段补全", c10),
    ("重复导入按 id 合并", c11),
    ("--replace 整校区替换", c12),
    ("导入坏输入被拒绝", c13),
    ("只增不减 + 来源状态", c14),
    ("注册表缺失时沿用状态", c15),
    ("THU 只增不减(上游变少也不丢)", c16),
    ("同名官网/公众号不合并", c17),
    ("限流提示分粒度(账号级 vs 单号级)", c18),
]:
    case(name, fn)

failed = 0
for name, detail in results:
    bad = isinstance(detail, str) and detail.startswith("RAISED")
    failed += 1 if bad else 0
    print(("  FAIL " if bad else "  OK   ") + name + "  [" + str(detail) + "]")
print("\n通过 %d/%d" % (len(results) - failed, len(results)))
cleanup_tmp()
sys.exit(1 if failed else 0)
