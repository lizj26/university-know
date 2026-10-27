#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fetch_wechat.py 的离线单测：昵称规范化 / 精确匹配 / 名单加载 / 字段抽取 /
item 构造 / 合并结构 / 种子补漏。**不联网、不写真实 data/**。

运行：python build/_check/unit_wechat.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
# tests/ 在项目根下一层
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_module():
    """把 fetch_wechat 加载进来，并把它的 WORK_DIR / WECHAT_DIR / DATA_DIR 指到临时目录。

    这样单测里的名单加载、缓存读写、合并都落在临时目录，绝不碰真实数据。
    注意 WECHAT_DIR 是 WORK_DIR 的派生值（_work/wechat），必须一起改，
    否则测试会去读真实的 _work/wechat/（那里有 cookies 与名单）。
    """
    tmp = tempfile.mkdtemp(prefix="thu-wx-test-")
    work = os.path.join(tmp, "work")
    os.makedirs(os.path.join(work, "wechat"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
    spec = importlib.util.spec_from_file_location("fw", os.path.join(ROOT, "fetch_wechat.py"))
    fw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fw)
    fw.WORK_DIR = work
    fw.LEGACY_WORK_DIR = work
    fw.WECHAT_DIR = os.path.join(work, "wechat")
    fw.DATA_DIR = os.path.join(tmp, "data")
    return fw, tmp


fw, TMP = load_module()

cases = []


def case(name, fn, expect):
    cases.append((name, fn, expect))


# --------------------------------------------------------------------------
# 1. 昵称规范化：全平台昵称比对的基础
# --------------------------------------------------------------------------
case("规范化:去空格", lambda: fw.normalize_name(" 浙江 大学 "), "浙江大学")
case("规范化:全角转半角", lambda: fw.normalize_name("ＺＪＵＰＥＳ"), "zjupes")
case("规范化:全角空格", lambda: fw.normalize_name("浙江\u3000大学"), "浙江大学")
case("规范化:大小写统一", lambda: fw.normalize_name("ZJUPES"), "zjupes")
case("规范化:空值", lambda: fw.normalize_name(None), "")

# --------------------------------------------------------------------------
# 2. pick_exact：搜索结果里挑"规范化后完全相等"的那个
# --------------------------------------------------------------------------
SEARCH_ROWS = [
    {"nickname": "浙江大学校友总会", "fakeid": "F_ALUMNI"},
    {"nickname": "浙江大学", "fakeid": "F_MAIN"},
    {"nickname": "浙江大学本科生院", "fakeid": "F_UG"},
]
case("精确匹配:挑出全等而非第一个",
     lambda: fw.pick_exact(SEARCH_ROWS, "浙江大学"), ("浙江大学", "F_MAIN", True))
case("精确匹配:带空格/全角也能命中",
     lambda: fw.pick_exact(SEARCH_ROWS, " 浙江 大学 "), ("浙江大学", "F_MAIN", True))
case("精确匹配:没有全等时退回首个候选(exact=False)",
     lambda: fw.pick_exact(SEARCH_ROWS, "浙江大学某某号"), ("浙江大学校友总会", "F_ALUMNI", False))
case("精确匹配:空结果", lambda: fw.pick_exact([], "浙江大学"), (None, None, False))
case("精确匹配:别名也能对（昵称唯一）",
     lambda: fw.pick_exact([{"nickname": "竺院团学汇", "fakeid": "F_ZK"}], "竺院团学汇"),
     ("竺院团学汇", "F_ZK", True))

# --------------------------------------------------------------------------
# 3. row_get：接口返回 arr/list 两种字段名都要认
# --------------------------------------------------------------------------
case("灵活取字段:第一个非空",
     lambda: fw.row_get({"link": "U1", "url": "U2"}, "link", "url"), "U1")
case("灵活取字段:跳过缺失",
     lambda: fw.row_get({"url": "U2"}, "link", "url"), "U2")
case("灵活取字段:全缺返回 None(调用方用 `or ''` 兜底)",
     lambda: fw.row_get({}, "link", "url"), None)

# --------------------------------------------------------------------------
# 4. parse_cookie_header：浏览器整行 Cookie 的解析
# --------------------------------------------------------------------------
CH = fw.parse_cookie_header("Cookie: wxuin=123; slave_sid=abc; data_ticket=xy=z")
case("Cookie 解析:去前缀", lambda: CH.get("wxuin"), "123")
case("Cookie 解析:多字段", lambda: CH.get("slave_sid"), "abc")
case("Cookie 解析:值里带等号", lambda: CH.get("data_ticket"), "xy=z")
case("Cookie 解析:无前缀也行",
     lambda: fw.parse_cookie_header("a=1; b=2").get("b"), "2")

# --------------------------------------------------------------------------
# 5. make_item：与 fetch_campus.py 的 item 结构必须一致
# --------------------------------------------------------------------------
_body = "浙江大学将于十月举办秋季运动会，欢迎全校师生报名参加。" * 3
ITEM = fw.make_item("https://mp.weixin.qq.com/s/AAA", "关于举办秋季运动会的通知",
                    "2026-10-08T09:00:00+08:00", "浙江大学", _body)
case("item: i 就是文章链接", lambda: ITEM["i"], "https://mp.weixin.qq.com/s/AAA")
case("item: 来源名即公众号名", lambda: ITEM["s"], "浙江大学")
case("item: sg 固定 wechat", lambda: ITEM["dt"]["sg"], "wechat")
case("item: og 记公众号名", lambda: ITEM["dt"]["og"], "浙江大学")
case("item: si 为空(由合并端补 wx: 前缀)", lambda: ITEM["si"], None)
case("item: 分类命中文体活动", lambda: ITEM["c"], "文体活动")
case("item: 活动类 g=activity", lambda: ITEM["g"], "activity")
case("item: 正文摘录已写入（长度≥40 才写 tx）", lambda: (ITEM["dt"].get("tx") or "")[:4], "浙江大学")

# 信息类：不含活动关键词 → information
ITEM2 = fw.make_item("https://mp.weixin.qq.com/s/BBB", "我校荣获国家科技进步奖", "2026-10-01T09:00:00+08:00", "上海交通大学", "")
case("item: 无正文也能构造", lambda: ITEM2["t"], "我校荣获国家科技进步奖")
case("item: 短正文不写 tx", lambda: "tx" in ITEM2["dt"], False)

# --------------------------------------------------------------------------
# 6. load_accounts：内置 + 外部名单合并、去重、规范化
# --------------------------------------------------------------------------
with open(os.path.join(fw.WORK_DIR, "wxmp-accounts-zju.json"), "w", encoding="utf-8") as fh:
    json.dump({"accounts": [
        {"name": "浙江大学", "max": 15},                    # 与内置重复，应去重
        {"name": "竺院团学汇", "max": 10},                  # 新增
        {"name": " ＺＪＵＰＥＳ ", "max": 5},               # 规范化后与内置 ZJUPES 重复
    ]}, fh, ensure_ascii=False)
ACC = fw.load_accounts("zju")
NAMES = [a["name"] for a in ACC]
case("名单:外部文件已合并", lambda: "竺院团学汇" in NAMES, True)
case("名单:同名去重(浙江大学只出现一次)", lambda: NAMES.count("浙江大学"), 1)
case("名单:规范化去重(ZJUPES 只出现一次)",
     lambda: sum(1 for n in NAMES if fw.normalize_name(n) == "zjupes"), 1)
case("名单:内置兜底仍在", lambda: "浙江大学本科招生" in NAMES, True)
case("名单:max 被带出", lambda: next(a["max"] for a in ACC if a["name"] == "竺院团学汇"), 10)

# 名称→参数 的写法：外部文件应**更新**内置条目的 max，而不是被静默去重丢掉
with open(os.path.join(fw.WORK_DIR, "wxmp-accounts-xjtu.json"), "w", encoding="utf-8") as fh:
    json.dump({"西安交通大学": {"max": 12}}, fh, ensure_ascii=False)
XACC = fw.load_accounts("xjtu")
case("名单:名称→参数写法能覆盖内置 max",
     lambda: next(a["max"] for a in XACC if a["name"] == "西安交通大学"), 12)
case("名单:覆盖后不重复出现",
     lambda: [a["name"] for a in XACC].count("西安交通大学"), 1)

# 纯数组写法
with open(os.path.join(fw.WORK_DIR, "wxmp-accounts-sjtu.json"), "w", encoding="utf-8") as fh:
    json.dump(["上海交通大学", "上交大研招"], fh, ensure_ascii=False)
SACC = [a["name"] for a in fw.load_accounts("sjtu")]
case("名单:纯数组写法", lambda: "上交大研招" in SACC, True)

# --------------------------------------------------------------------------
# 7. fakeid 缓存往返
#    注意：断言延迟到末尾才求值，所以这里当场读出来再交进去
# --------------------------------------------------------------------------
fw.save_fakeid_cache({"zust": {"nickname": "浙江大学", "fakeid": "F1"}})
_v_cache_f1 = fw.load_fakeid_cache().get("zust", {}).get("fakeid")
case("缓存:写入后可读回", lambda: _v_cache_f1, "F1")

# --------------------------------------------------------------------------
# 8. merge_into_data：只增不减 + 90 天保留 + 结构化 last_import
# --------------------------------------------------------------------------
refresh = fw.load_refresh()
fp = os.path.join(fw.DATA_DIR, "zju.json")
old_item = {"i": "https://mp.weixin.qq.com/s/OLD", "t": "旧文章", "s": "浙江大学",
            "si": None, "d": "2026-10-01T09:00:00+08:00",
            "u": "https://mp.weixin.qq.com/s/OLD", "c": "校园动态", "g": "information",
            "en": "", "ens": "", "dt": {"sg": "wechat", "og": "浙江大学"}}
stale_item = {"i": "https://mp.weixin.qq.com/s/STALE", "t": "过期文章", "s": "浙江大学",
              "si": None, "d": "2020-01-01T09:00:00+08:00",
              "u": "https://mp.weixin.qq.com/s/STALE", "c": "校园动态", "g": "information",
              "en": "", "ens": "", "dt": {"sg": "wechat", "og": "浙江大学"}}
with open(fp, "w", encoding="utf-8") as fh:
    json.dump({"campus": "zju", "campus_name": "浙大知道", "items": [old_item, stale_item],
               "source_registry": {}, "wechat_accounts": ["老号"]}, fh, ensure_ascii=False)

NEW_ITEM = fw.make_item("https://mp.weixin.qq.com/s/NEW", "新通知", "2026-10-09T09:00:00+08:00",
                        "浙江大学", "")
reg = {"wx:浙江大学": {"name": "浙江大学", "count": 1, "new": 1, "status": "ok",
                    "last_checked": "2026-10-10T09:00:00+08:00"}}
# 第一轮：新条目全新入库（stale 被清除不算"新增"）
total, added = fw.merge_into_data("zju", [NEW_ITEM], reg, refresh)
with open(fp, encoding="utf-8") as fh:
    merged = json.load(fh)
mids = {it["i"] for it in merged["items"]}
case("合并:新条目录入", lambda: "https://mp.weixin.qq.com/s/NEW" in mids, True)
case("合并:旧条目保留(只增不减)", lambda: "https://mp.weixin.qq.com/s/OLD" in mids, True)
case("合并:超 90 天的被清除", lambda: "https://mp.weixin.qq.com/s/STALE" in mids, False)
case("合并:最终条数=旧留存+新入库", lambda: total, 2)
case("合并:注册表已写入",
     lambda: merged["source_registry"]["wx:浙江大学"]["count"], 1)
case("合并:公众号名累积不丢", lambda: "老号" in merged["wechat_accounts"], True)
case("合并:last_import 是结构化 dict", lambda: isinstance(merged["last_import"], dict), True)
case("合并:last_import.items 与实际一致", lambda: merged["last_import"]["items"], 2)
case("合并:last_import.crawl_new 来自注册表",
     lambda: merged["last_import"]["crawl_new"], 1)
case("合并:写入前有 .bak 备份", lambda: os.path.exists(fp + ".bak"), True)
case("合并:来源表已按 items 重算",
     lambda: any(s.get("key") == "wx:浙江大学" for s in merged.get("sources") or []), True)

# 第二轮：同一条重复导入不应重复计数
total2, added2 = fw.merge_into_data("zju", [NEW_ITEM], reg, refresh)
case("合并:重复导入新增 0(同 id 覆盖不算新增)", lambda: added2, 0)
case("合并:重复导入条数不涨", lambda: total2, total)

# --------------------------------------------------------------------------
# 9. seed_links_without_items：种子补漏要剔掉本轮已入库的
# --------------------------------------------------------------------------
with open(os.path.join(fw.WORK_DIR, "wechat-zju.txt"), "w", encoding="utf-8") as fh:
    fh.write("# 注释行应被忽略\n"
             "https://mp.weixin.qq.com/s/AAA\n"
             "https://mp.weixin.qq.com/s/BBB\n"
             "https://mp.weixin.qq.com/s/CCC\n"
             "https://example.com/not-wechat\n")
have = [{"i": "https://mp.weixin.qq.com/s/AAA", "u": "https://mp.weixin.qq.com/s/AAA"}]
pending = fw.seed_links_without_items("zju", have, limit=10)
case("种子补漏:剔除已入库的", lambda: "https://mp.weixin.qq.com/s/AAA" in pending, False)
case("种子补漏:保留未入库的",
     lambda: set(pending) >= {"https://mp.weixin.qq.com/s/BBB", "https://mp.weixin.qq.com/s/CCC"}, True)
case("种子补漏:非微信链接被过滤",
     lambda: any("example.com" in u for u in pending), False)
case("种子补漏:limit 生效", lambda: len(fw.seed_links_without_items("zju", [], limit=1)), 1)

# --------------------------------------------------------------------------
# 10. 保底：无 cookies 时不炸（降级判定）
#     用一个**全新的干净目录**测，避免受前面用例写下的文件干扰。
#     注意：case() 的断言是**延迟求值**的，所以这里必须当场算出结果再断言。
# --------------------------------------------------------------------------
_clean = tempfile.mkdtemp(prefix="thu-wx-nocookie-")
_old_work, _old_wx, _old_data = fw.WORK_DIR, fw.WECHAT_DIR, fw.DATA_DIR
fw.WORK_DIR = os.path.join(_clean, "work")
fw.LEGACY_WORK_DIR = fw.WORK_DIR
fw.WECHAT_DIR = os.path.join(fw.WORK_DIR, "wechat")
fw.DATA_DIR = os.path.join(_clean, "data")
os.makedirs(fw.WECHAT_DIR, exist_ok=True)
os.makedirs(fw.DATA_DIR, exist_ok=True)
_NO_COOKIE = fw.load_cookies("nonexistent-campus")[0]         # 当场求值
case("无 cookies 时 load_cookies 返回空", lambda: _NO_COOKIE, None)
# 还原，后面的目录用例继续用原来的临时目录
fw.WORK_DIR, fw.WECHAT_DIR, fw.DATA_DIR = _old_work, _old_wx, _old_data
fw.LEGACY_WORK_DIR = _old_work
shutil.rmtree(_clean, ignore_errors=True)

# --------------------------------------------------------------------------
# 11. 目录分离与旧布局迁移（本轮整理工作区的核心保障）
#     公众号装备必须落在 _work/wechat/，官网产物落在 _work/ 根下，互不混淆。
#     ⚠️ case() 的断言延迟到末尾才求值，而本节会**移动文件** ——
#        所有跟文件状态有关的断言都必须当场算好再交进去（见下面的 _v? 变量）。
# --------------------------------------------------------------------------

# 11.1 新位置优先：写进 _work/wechat/ 的文件能被找到
new_cookie = os.path.join(fw.WECHAT_DIR, "wxmp-cookie-header.txt")
with open(new_cookie, "w", encoding="utf-8") as fh:
    fh.write("wxuin=NEW; pass_ticket=NEWTICKET\n")
_v_hit_new = (new_cookie == fw.find_wechat_file("wxmp-cookie-header.txt")) and new_cookie
case("find_wechat_file 命中新位置", lambda: _v_hit_new, new_cookie)
_v_new = fw.load_cookies("zju")[0].get("wxuin")
case("新位置 cookies 被读到", lambda: _v_new, "NEW")

# 11.2 新位置优先于旧位置：两边都有同名文件时，读到的必须是新的
legacy_cookie = os.path.join(fw.LEGACY_WORK_DIR, "wxmp-cookie-header.txt")
with open(legacy_cookie, "w", encoding="utf-8") as fh:
    fh.write("wxuin=OLD; pass_ticket=OLDTICKET\n")
_v_prio = fw.load_cookies("zju")[0].get("wxuin")
case("新旧同名时优先新位置（不回退覆盖）", lambda: _v_prio, "NEW")
os.remove(new_cookie)                       # 只留旧位置，验证回退

# 11.3 旧位置回退：只在 _work/ 根下放文件，也应能读到（升级不丢东西）
_v_fallback_path = fw.find_wechat_file("wxmp-cookie-header.txt")
_v_fallback_cookie = fw.load_cookies("zju")[0].get("wxuin")
case("旧位置可被回退读到", lambda: _v_fallback_path, legacy_cookie)
case("旧位置 cookies 被读到", lambda: _v_fallback_cookie, "OLD")

# 11.4 迁移：旧文件被搬进 _work/wechat/，且原位置不再有
moved = fw.migrate_legacy_files(verbose=False)
_v_moved = "wxmp-cookie-header.txt" in moved
_v_in_new = os.path.exists(os.path.join(fw.WECHAT_DIR, "wxmp-cookie-header.txt"))
_v_legacy_gone = os.path.exists(legacy_cookie)
_v_second_move = fw.migrate_legacy_files(verbose=False)
case("迁移把旧 cookies 搬进新目录", lambda: _v_moved, True)
case("迁移后文件在新目录", lambda: _v_in_new, True)
case("迁移后旧位置已清空", lambda: _v_legacy_gone, False)
case("迁移不碰官网产物：同名文件留在 _work 根下（二次调用无动作）",
     lambda: _v_second_move, [])

# 11.5 write_payload 落在 _work/wechat/
p = fw.write_payload("zju", [], {}, [])
_v_payload_dir_ok = os.path.dirname(os.path.abspath(p)) == os.path.abspath(fw.WECHAT_DIR)
case("write_payload 落在 _work/wechat/", lambda: _v_payload_dir_ok, True)
case("write_payload 文件名带前缀",
     lambda: os.path.basename(p), "wechat-items-zju.json")

# 11.6 fakeid 缓存写在 _work/wechat/ 且能读回
fw.save_fakeid_cache({"浙江大学": {"nickname": "浙江大学", "fakeid": "F_MAIN"}})
_v_fakeid_exists = os.path.exists(os.path.join(fw.WECHAT_DIR, fw.FAKEID_CACHE))
_v_fakeid_back = fw.load_fakeid_cache().get("浙江大学", {}).get("fakeid")
case("fakeid 缓存落在 _work/wechat/", lambda: _v_fakeid_exists, True)
case("fakeid 缓存能读回", lambda: _v_fakeid_back, "F_MAIN")

# 11.7 命名约定：官网产物名不应落在 WECHAT_DIR 的迁移名单里
case("迁移名单不含官网产物 items-*.json",
     lambda: any(g.startswith("items-") for g in fw.MIGRATE_GLOBS), False)

# --------------------------------------------------------------------------
# 12. 限流缓解：凭据轮换池 / fakeid 分桶 / 限流记忆 / 请求抖动
#     这一组是「现有方案无法解决限流」的针对性回归。任何一条挂掉都意味着
#     账号级限流又会变回「只能干等」。
#
#     这一组**必须换一个干净的目录**：前面的用例（11.x）已经在 WECHAT_DIR 里
#     写过 cookies / fakeid / 迁移文件，直接在同一个目录里数池子会算错。
# --------------------------------------------------------------------------
WX = os.path.join(TMP, "throttle", "wechat")
os.makedirs(WX, exist_ok=True)
fw.WECHAT_DIR = WX
fw.LEGACY_WORK_DIR = os.path.join(TMP, "throttle", "_legacy")
os.makedirs(fw.LEGACY_WORK_DIR, exist_ok=True)


def _write_cred(name, wxuin):
    with open(os.path.join(WX, name), "w", encoding="utf-8") as fh:
        fh.write(f"wxuin={wxuin}; pass_ticket=t{wxuin}; appmsg_token=a{wxuin}; slave_sid=s{wxuin}")


def _pool_names():
    return [d for _c, d in fw.list_credential_pool("zju")]


# 12.1 没凭据时池子是空的（不能凭空造一份出来）
_v_pool_empty = len(fw.list_credential_pool("zju"))
case("凭据池:空目录为空", lambda: _v_pool_empty, 0)

# 12.2 第一份照旧生效（向后兼容）
_write_cred("wxmp-cookie-header.txt", "111")
_v_pool_one = len(fw.list_credential_pool("zju"))
case("凭据池:单份可读", lambda: _v_pool_one, 1)

# 12.3 **核心**：加第二份后池子变两份 —— 这是"换账号即可解限"的前提
_write_cred("wxmp-cookie-header-pool2.txt", "222")
_v_pool_two = len(fw.list_credential_pool("zju"))
case("凭据池:pool2 被识别为第 2 份", lambda: _v_pool_two, 2)
_v_pool_names2 = _pool_names()
case("凭据池:第一份排在最前", lambda: _v_pool_names2[0], "wxmp-cookie-header.txt")
case("凭据池:第二份是 pool2", lambda: _v_pool_names2[1], "wxmp-cookie-header-pool2.txt")

# 12.4 多行汇总文件：一行一份，最省事的录入方式
with open(os.path.join(WX, "wxmp-cookie-header-pool.txt"), "w", encoding="utf-8") as fh:
    fh.write("wxuin=1; pass_ticket=x\nwxuin=2; pass_ticket=y\nwxuin=3; pass_ticket=z\n")
_v_pool_multi = len(fw.list_credential_pool("zju"))
case("凭据池:多行文件按行拆分", lambda: _v_pool_multi, 5)

# 12.5 校区专用优先于全局（同一份凭据，校区专用的文件先用）
_write_cred("wxmp-cookie-header-zju.txt", "999")
_v_pool_scoped = _pool_names()[0]
case("凭据池:校区专用排最前", lambda: _v_pool_scoped, "wxmp-cookie-header-zju.txt")

# 12.6 轮换是确定性的：同一天多次调用必须选同一份，否则无法复现问题
case("轮换:同一天索引稳定",
     lambda: fw.pool_index_for_today("zju") == fw.pool_index_for_today("zju"), True)

# 12.7 越界不抛异常（池子变小/并发时不能把整轮打断）
case("轮换:越界回退到最后一份",
     lambda: isinstance(fw.pick_credential("zju", 999), tuple), True)

# 12.8 fakeid 缓存按凭据分桶：A 账号搜到的 fakeid 不能直接喂给 B 账号去拉文章
case("fakeid 分桶:默认名不变", lambda: fw._fakeid_cache_name(None), fw.FAKEID_CACHE)
case("fakeid 分桶:第 1 份用默认名",
     lambda: fw._fakeid_cache_name("wxmp-cookie-header.txt"), fw.FAKEID_CACHE)
case("fakeid 分桶:pool2 独立文件",
     lambda: fw._fakeid_cache_name("wxmp-cookie-header-pool2.txt"), "wxmp-fakeid-pool2.json")

# 12.9 分桶要真的落盘到不同文件，不能只在名字上分
fw.save_fakeid_cache({"浙江大学": {"fakeid": "F1"}}, "wxmp-cookie-header.txt")
fw.save_fakeid_cache({"浙江大学": {"fakeid": "F2"}}, "wxmp-cookie-header-pool2.txt")
case("fakeid 分桶:两份凭据各自落盘",
     lambda: fw.load_fakeid_cache("wxmp-cookie-header-pool2.txt").get("浙江大学", {}).get("fakeid"),
     "F2")
case("fakeid 分桶:互不覆盖",
     lambda: fw.load_fakeid_cache("wxmp-cookie-header.txt").get("浙江大学", {}).get("fakeid"),
     "F1")

# 12.10 限流记忆：区分「没限过」与「刚限过」
case("限流记忆:未记录=未限流", lambda: fw.is_recently_blocked("credentials", "nope.txt"), False)
fw.note_block("credentials", "wxmp-cookie-header.txt", "测试")
case("限流记忆:记下后判定为限流中",
     lambda: fw.is_recently_blocked("credentials", "wxmp-cookie-header.txt"), True)
case("限流记忆:其它凭据不受牵连",
     lambda: fw.is_recently_blocked("credentials", "other.txt"), False)
case("限流记忆:过期后自动失效",
     lambda: fw.is_recently_blocked("credentials", "wxmp-cookie-header.txt", hours=0), False)
case("限流记忆:凭据与目标分桶互不串味",
     lambda: fw.is_recently_blocked("targets", "wxmp-cookie-header.txt"), False)

# 12.11 请求抖动：固定间隔本身是特征，必须落在区间内且不为常数
_d1 = fw.gentle_sleep(0.3)
_d2 = fw.gentle_sleep(0.3)
case("抖动:落在 ±35% 区间内", lambda: 0.05 <= _d1 <= 0.41, True)
case("抖动:不是固定值（两次不同）", lambda: _d1 != _d2, True)
case("抖动:base<=0 直接返回", lambda: fw.gentle_sleep(0), 0.0)

# 12.12 list_articles 必须能回报「被限流」而不是默默返回空
#        —— 区分「这个号没新文章」和「被限了」是给出正确建议的前提


class _BlockedApi:
    """假的 api：appmsg 一律返回 200013（freq control）。"""

    def list_articles(self, fakeid, begin=0, count=5):
        return [], 200013


class _OkApi:
    def list_articles(self, fakeid, begin=0, count=5):
        if begin:
            return [], 0
        return [{"link": "https://mp.weixin.qq.com/s/x1", "title": "标题",
                 "update_time": 1700000000, "digest": "摘要"}], 0


_rows, _blk = fw.list_articles(_BlockedApi(), "F1", 5, 1, retries=0, want_block=True)
case("限流回报:200013 被标为 blocked", lambda: _blk, True)
case("限流回报:被限时不返回条目", lambda: _rows, [])

_rows2, _blk2 = fw.list_articles(_OkApi(), "F1", 5, 1, retries=0, want_block=True)
case("限流回报:正常时 blocked=False", lambda: _blk2, False)
case("限流回报:正常时能取到条目", lambda: len(_rows2), 1)

# 12.13 不带 want_block 时仍然返回纯列表（不破坏既有调用方）
case("限流回报:默认签名仍返回列表",
     lambda: type(fw.list_articles(_OkApi(), "F1", 5, 1, retries=0)).__name__, "list")

# --------------------------------------------------------------------------
# 13. 公开合集源：零凭据、不受限流，是限流期的兜底
# --------------------------------------------------------------------------
with open(os.path.join(WX, "wxmp-albums.json"), "w", encoding="utf-8") as fh:
    json.dump({"albums": [{"biz": "MzA5==", "album_id": "123", "name": "招生简章"}],
               "zju": ["MzAx:999"]}, fh)


def _albums(campus):
    return fw.load_albums(campus)


_v_alb_zju = _albums("zju")
case("合集:按校区分组生效",
     lambda: _v_alb_zju[0]["biz"] == "MzAx" and _v_alb_zju[0]["album_id"] == "999", True)
_v_alb_sjtu = _albums("sjtu")
case("合集:通用 albums 字段生效", lambda: _v_alb_sjtu[0]["name"], "招生简章")
_v_alb_none = _albums("zzz-nonexistent")
# 注意：`albums` 字段是**全局**列表，对所有校区都生效（这正是"配一份到处能用"的设计），
# 所以未分组校区会拿到全局那一条。真正"没配"的场景要另建一个没有任何 albums 文件的目录。
case("合集:全局 albums 对所有校区生效", lambda: _v_alb_none[0]["name"], "招生简章")

_empty_wx = os.path.join(TMP, "no-albums", "wechat")
os.makedirs(_empty_wx, exist_ok=True)
_prev_wx, fw.WECHAT_DIR = fw.WECHAT_DIR, _empty_wx
_v_alb_absent = _albums("zju")
fw.WECHAT_DIR = _prev_wx
case("合集:没配就返回空（不报错）", lambda: _v_alb_absent, [])
case("合集:接口地址是公开的 appmsgalbum",
     lambda: fw.ALBUM_API.endswith("/mp/appmsgalbum"), True)

# 12.14 合集不需要 cookies：这条路径在完全无凭据时也必须可用
case("合集:无需凭据即可声明",
     lambda: hasattr(fw, "fetch_album_articles") and hasattr(fw, "crawl_albums"), True)


# 12.15 合集响应解析（用假响应，不联网）
#       这条路径是限流期的兜底，解析错了就等于没有兜底。
class _FakeResp:
    def __init__(self, obj):
        self._b = json.dumps(obj).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _album_with(payload):
    """让 fetch_album_articles 拿到指定响应体，返回解析结果。

    ⚠ 必须同时打**两条**网络的桩：`_album_json` 优先用 requests，装不上才退回
    urllib。只打 urllib 的桩时，机器上恰好装了 requests 就会真联网（对假 biz 返回
    ret=10004），表现为"装了依赖，测试反而红了"——已实际踩到。
    """
    import urllib.request

    class _FakeRequestsResp:
        def json(self):
            return payload

    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: _FakeResp(payload)
    try:
        import requests
    except ImportError:
        requests = None
    orig_get = getattr(requests, "get", None)
    if requests is not None:
        requests.get = lambda *a, **k: _FakeRequestsResp()
    try:
        return fw.fetch_album_articles("MzAx==", "123")
    finally:
        urllib.request.urlopen = orig
        if requests is not None and orig_get is not None:
            requests.get = orig_get


_FAKE_OK = {"base_resp": {"ret": 0}, "getalbum_resp": {"article_list": [
    {"title": "2026 年招生简章", "url": "https://mp.weixin.qq.com/s/aaa",
     "create_time": 1700000000, "digest": "摘要A"},
    {"title": "", "url": "https://x", "create_time": 1},      # 空标题 → 丢
    {"title": "无链接", "url": "", "create_time": 1},           # 空链接 → 丢
]}}
_v_alb_rows = _album_with(_FAKE_OK)
case("合集解析:丢掉空标题/空链接，只留有效条目", lambda: len(_v_alb_rows), 1)
case("合集解析:标题正确", lambda: _v_alb_rows[0]["title"], "2026 年招生简章")
case("合集解析:链接正确",
     lambda: _v_alb_rows[0]["link"], "https://mp.weixin.qq.com/s/aaa")
case("合集解析:时间转成北京时间 ISO",
     lambda: _v_alb_rows[0]["published"].startswith("2023-11-15"), True)
case("合集解析:ret!=0 时返回空",
     lambda: _album_with({"base_resp": {"ret": 10004}}), [])
case("合集解析:非 dict 响应不崩",
     lambda: _album_with(["unexpected"]), [])
case("合集解析:缺 article_list 不崩",
     lambda: _album_with({"base_resp": {"ret": 0}}), [])

# --------------------------------------------------------------------------
# 13. 合集自动发现 + 翻页（零凭据链路的核心）
#     实测依据：文章页里印着合集的完整链接（__biz + album_id），
#     合集接口无需登录、返回的链接带 chksm 因而可抓正文。
#     这一组挂掉 = 「不用 cookies 也能持续更新」的能力没了。
# --------------------------------------------------------------------------
ALBUM_HTML = (
    '..."shareLink": \'https://mp.weixin.qq.com/mp/appmsgalbum'
    '?__biz=MzUyMDM3NzE3Nw==&amp;action=getalbum&amp;album_id=4250077557137342472'
    '#wechat_redirect\' ... '
    'href="https://mp.weixin.qq.com/mp/appmsgalbum'
    '?__biz=MzUyMDM3NzE3Nw%3D%3D&action=getalbum&album_id=4250077557137342472" '
    '其他内容 album_id=3933168466257051660 但前面没有 biz...'
)


def _refs(html):
    return fw.extract_album_refs(html)


case("合集发现:从文章页挖出 biz+album_id",
     lambda: _refs(ALBUM_HTML),
     [{"biz": "MzUyMDM3NzE3Nw==", "album_id": "4250077557137342472"}])
case("合集发现:同页重复只留一条（含 %3D%3D 归一）",
     lambda: len(_refs(ALBUM_HTML)), 1)
case("合集发现:无合集返回空",
     lambda: _refs("<html>没有任何合集</html>"), [])
case("合集发现:空输入不崩", lambda: _refs(None), [])
case("合集发现:album_id 前面没 __biz 就跳过",
     lambda: _refs("album_id=3933168466257051660"), [])
case("合集发现:多合集各留一条", lambda: len(_refs(
    'a?__biz=AAA==&album_id=1111111111111 b?__biz=BBB==&album_id=2222222222222')), 2)

# --- _album_page 解析 + fetch_album 翻页（打桩，不联网）---
_FAKE_PAGES = {
    0: ({"base_resp": {"ret": 0}, "getalbum_resp": {
        "base_info": {"title": "学生风采"},
        "article_list": [
            {"title": "甲", "url": "http://x/1", "create_time": 1700000000, "msgid": 101, "itemidx": 1},
            {"title": "乙", "url": "http://x/2", "create_time": 1700000100, "msgid": 102, "itemidx": 1},
        ],
        "continue_flag": 1}}),
    102: ({"base_resp": {"ret": 0}, "getalbum_resp": {
        "article_list": [
            {"title": "丙", "url": "http://x/3", "create_time": 1700000200, "msgid": 103, "itemidx": 1},
        ],
        "continue_flag": 0}}),
}


def _patch(mapping):
    """按 begin_msgid 返回预置响应；返回 (原函数, 恢复函数)。"""
    orig = fw._album_json

    def fake(params, timeout=12):
        return mapping.get(int(params.get("begin_msgid") or 0), {})
    fw._album_json = fake

    def restore():
        fw._album_json = orig
    return orig, restore


_orig_album, _restore_album = _patch(_FAKE_PAGES)
_p = fw._album_page("BIZ", "AID")
_title, _arts = fw.fetch_album("BIZ", "AID", pages=3)
_restore_album()

case("合集页:解析出条目数", lambda: len(_p[0]), 2)
case("合集页:带回合集标题", lambda: _p[1], "学生风采")
case("合集页:带回 continue_flag", lambda: _p[2], True)
case("合集页:条目带 msgid 供翻页", lambda: _p[0][-1]["_msgid"], 102)
case("合集翻页:跨页收齐 3 篇", lambda: len(_arts), 3)
case("合集翻页:标题来自首页", lambda: _title, "学生风采")
case("合集翻页:顺序正确", lambda: [a["title"] for a in _arts], ["甲", "乙", "丙"])
case("合集翻页:published 已转 ISO", lambda: _arts[0]["published"][:4], "2023")

# ret != 0 必须静默返回空，不能把「错参数」当成「无文章」
_ret_bad = (lambda: (_patch({0: {"base_resp": {"ret": 10004}}}),
                     fw._album_page("BIZ", "bad"))[-1])()
case("合集页:ret=10004 返回空", lambda: (_ret_bad[0], _ret_bad[2]), ([], False))
_restore_album()

# --- crawl_albums：零配置自动发现 + 名单过滤 + 正文回填 ---
_orig_sources, _orig_fetch, _orig_bodies = fw.album_sources, fw.fetch_album, fw.fetch_bodies
fw.album_sources = lambda campus, args: [{"biz": "BIZ", "album_id": "AID",
                                          "name": "", "account": "西安交通大学机械工程学院"}]
fw.fetch_album = lambda biz, aid, timeout=12, pages=1: ("学生风采", [
    {"title": "机械学院通知", "link": "http://x/1", "published": "2026-01-01", "digest": "摘"},
    {"title": "无关他校新闻", "link": "http://x/2", "published": "2026-01-02", "digest": "摘"},
])
LONG = "正文甲" * 20   # make_item 只在正文 >=40 字时才写 dt.tx
fw.fetch_bodies = lambda urls, workers=6, timeout=15: [LONG] * len(urls)


class _A:
    seed_file = None
    workers = 1
    no_content = False
    album_pages = 1


_c_items, _c_reg = fw.crawl_albums("xjtu", _A())
fw.album_sources, fw.fetch_album, fw.fetch_bodies = _orig_sources, _orig_fetch, _orig_bodies

case("合集抓取:收录本校条目", lambda: len(_c_items), 2)
case("合集抓取:registry 按号聚合", lambda: sorted(_c_reg.keys()),
     ["wx:西安交通大学机械工程学院"])
case("合集抓取:正文已回填", lambda: _c_items[0]["dt"]["tx"][:3], "正文甲")

# no_content 时不抓正文（只用摘要）
_orig_sources2, _orig_fetch2 = fw.album_sources, fw.fetch_album
fw.album_sources = lambda campus, args: [{"biz": "B", "album_id": "A",
                                          "name": "", "account": "西安交通大学机械工程学院"}]
fw.fetch_album = lambda biz, aid, timeout=12, pages=1: ("x", [
    {"title": "机械学院通知", "link": "http://x/1", "published": "", "digest": "只有摘要" * 12},
])
_bodies_called = []


def _should_not_call(urls, workers=6, timeout=15):
    _bodies_called.append(1)
    return [""] * len(urls)


fw.fetch_bodies = _should_not_call


class _B:
    seed_file = None
    workers = 1
    no_content = True
    album_pages = 1


_nc_items, _ = fw.crawl_albums("xjtu", _B())
fw.album_sources, fw.fetch_album, fw.fetch_bodies = _orig_sources2, _orig_fetch2, fw.fetch_bodies

case("合集抓取:--no-content 不抓正文", lambda: len(_bodies_called), 0)
case("合集抓取:--no-content 用摘要兜底", lambda: _nc_items[0]["dt"]["tx"][:4], "只有摘要")

# album_sources：有清单用清单，没清单才去发现
_orig_load, _orig_disc, _orig_seeds = fw.load_albums, fw.discover_albums, fw.load_seed_links
fw.load_albums = lambda campus: [{"biz": "CFG", "album_id": "1", "name": "配置集"}]
fw.discover_albums = lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该调用发现"))
_src_cfg = fw.album_sources("xjtu", _A())
fw.load_albums = lambda campus: []
fw.load_seed_links = lambda campus, extra=None: ["http://seed/1"]
fw.discover_albums = lambda campus, links, workers=4: [{"biz": "FOUND", "album_id": "9"}]
_src_disc = fw.album_sources("xjtu", _A())
fw.load_seed_links = lambda campus, extra=None: []
_src_none = fw.album_sources("xjtu", _A())
fw.load_albums, fw.discover_albums, fw.load_seed_links = _orig_load, _orig_disc, _orig_seeds

case("合集来源:有配置就不去发现", lambda: [a["name"] for a in _src_cfg], ["配置集"])
case("合集来源:无配置则自动发现", lambda: _src_disc[0]["biz"], "FOUND")
case("合集来源:无配置无种子返回空", lambda: _src_none, [])

# --------------------------------------------------------------------------
failed = 0
for name, fn, expect in cases:
    try:
        got = fn()
    except Exception as exc:  # noqa: BLE001
        got = "RAISED %s: %s" % (type(exc).__name__, exc)
    ok = got == expect
    failed += 0 if ok else 1
    print(("  OK   " if ok else "  FAIL ") + name + "  → " + repr(got)[:80])

print("\n通过 %d/%d" % (len(cases) - failed, len(cases)))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if failed else 0)
