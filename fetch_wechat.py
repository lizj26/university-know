#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「XX知道」多校区抓取器 —— 官方微信公众号（基于 wxmp 库）。

与其它脚本的分工
----------------
    refresh.py        走 pkuknow.cn 接口（清华），并把 data/*.json 生成 index.html
    fetch_campus.py   抓浙大/上交/西交的**学院官网**，产出 data/<校区>.json
    fetch_wechat.py   本文件：用 wxmp 直接调微信公众平台「公众号 → 历史文章列表」接口，
                      把一个公众号最近的推文成批取回来，合并进 data/<校区>.json

为什么单独一个脚本
------------------
微信公众号没有**公开**的文章列表接口，`fetch_campus.py` 只能靠「官网里链接的公众号文章」
被动发现内容。`wxmp` 走的是**公众平台后台**接口（searchbiz / appmsg），
只要有登录后的 cookies，就能按公众号名搜索 fakeid、再按页拉取历史文章列表。
这属于「有凭据的批量补全」，因此单独成一个脚本，不与纯公开抓取混在一起。

官网链路 vs 公众号链路（磁盘上也是分开的）
------------------------------------------
    官网链路（稳定）  refresh.py / fetch_campus.py / cloud_update.py
                      → 中间产物全在 `_work/` 根下（items-*/registry-*/run.log）
    公众号链路（不稳定）本文件
                      → 所有文件在 `_work/wechat/`（cookies / token / 名单 / fakeid / wechat-items-*）

两条链路都只往同一份 `data/<校区>.json` 里**追加**（按 `i` 去重、90 天保留期），
所以页面是合一的，但任何一条断了都不会影响另一条：
官网源失败=0（20 个源长期稳定），公众号受微信**账号级限流**影响会整轮拿不到东西。
把文件分开后，「清 _work、备份、排查某一条链路」互不牵连。
旧布局（公众号文件散在 `_work/` 根下）首次运行时会自动迁移，见 migrate_legacy_files()。

依赖
----
    pip install wxmp            # 仅本脚本需要，fetch_campus.py / refresh.py 仍只用标准库
    wxmp 需要 Python >= 3.10，并依赖 pydantic / requests / pandas 等

凭据（cookies）
---------------
在浏览器登录 https://mp.weixin.qq.com/ 后，从开发者工具里复制 cookies，存成 JSON：

    _work/wechat/wxmp-cookies.json          # 全局，所有校区共用
    _work/wechat/wxmp-cookies-<校区>.json   # 可选，优先于全局

最少需要 `wxuin` 和 `pass_ticket` 两个字段（其余字段原样带上更稳）：

    {"wxuin": "xxxx", "pass_ticket": "xxxx", "appmsg_token": "xxxx", "slave_sid": "xxxx"}

没有 cookies 时，脚本会**跳过联网**、只做本地合并与重建页面（等同微信未登录状态），
以便在无凭据环境下仍然可以跑通流程。

用法示例
--------
    python fetch_wechat.py                          # 三校全抓，写入 data/ 并重建页面
    python fetch_wechat.py --campus zju             # 只抓浙大
    python fetch_wechat.py --dry-run                # 只搜索公众号、验证 cookies，不写数据
    python fetch_wechat.py --count 10               # 每个号最多取 10 篇（默认 15）
    python fetch_wechat.py --pages 2                # 每个号最多翻 2 页
    python fetch_wechat.py --no-content             # 不抓正文（快、稳，列表信息也够用）
    python fetch_wechat.py --no-import              # 只抓取，结果落在 _work/wechat/wechat-items-<校区>.json
    python fetch_wechat.py --import-only             # 不联网，用上次结果导入 data/ 并重建页面
    python fetch_wechat.py --cookies my.json        # 指定 cookies 文件

应对限流（ret=200013，详见 docs/wxmp公众号接入.md 第 10 节）
----------------------------------------------------------
    # 账号级限流**只能靠换账号解决，等待无效**。多准备几份 cookies 即可轮换：
    #   _work/wechat/wxmp-cookie-header.txt        ← 第 1 份
    #   _work/wechat/wxmp-cookie-header-pool2.txt  ← 第 2 份
    # 或者把所有行塞进同一个文件，一行一份：
    #   _work/wechat/wxmp-cookie-header-pool.txt
    python fetch_wechat.py --cred-index 1          # 手工指定用第 2 份凭据

    # 公开合集源：零凭据、不受限流影响，是限流期的兜底
    python fetch_wechat.py --albums                # 正常抓取 + 合集补充
    python fetch_wechat.py --albums-only           # 只用合集，完全不碰 cookies
    python fetch_wechat.py --albums-only --album-pages 4   # 每个合集多翻几页

    # appmsg 被限流时会**自动**改走合集源，不需要人工干预。

    # 合集**不需要人工维护白名单**：脚本会拿公开种子清单里的文章页，
    # 自动从页面里发现的合集链接取到 (__biz, album_id) 再枚举。
    # 想手工指定也行：_work/wechat/wxmp-albums.json

为什么合集源不需要 cookies
--------------------------
微信的文章页里印着这篇所属合集的完整链接：

    mp/appmsgalbum?__biz=<biz>&action=getalbum&album_id=<id>#wechat_redirect

于是链路变成：**种子文章页 → 发现合集 → 枚举文章 → 抓正文**，全程零凭据，
也完全不调用 `appmsg`，所以**不受账号级限流影响**。

⚠ 一个容易踩的坑：微信对文章页链接分三种，只有前两种能抓（2026-10-14 实测）：

    短链            /s/XLsEsQOyo4qwtekckfDT2A            ✅ 3.7 MB
    长链带 chksm    /s?__biz=..&mid=..&sn=..&chksm=..     ✅ 3.6 MB
    长链缺 chksm    /s?__biz=..&mid=..&sn=..&scene=21     ❌ 17 KB 验证码墙

同一条文章做了受控对照，差别只在 `chksm`。文末「往期回顾」的链接**没有** chksm，
所以抓不动（只能拿来展示/跳转）；而**合集接口返回的链接带 chksm，能抓正文**。

扩展一个新校区 / 新公众号
------------------------
两种方式，任选：

① **外部名单文件**（推荐，不改代码）——把完整昵称写进
   `_work/wechat/wxmp-accounts.json`（全局）或 `_work/wechat/wxmp-accounts-<校区>.json`（分校区），
   支持 `["浙江大学","竺院团学汇"]` / `{"accounts":[{"name":...,"max":...}]}` /
   `{"浙江大学":{"max":15,"fakeid":"MzA3..."}}` 三种写法。

② 改下面的 `WECHAT_CONFIG`，加一个校区/加一个号：

    "xxx": {
        "campus_name": "XX知道",
        "accounts": [ {"name": "某某大学", "max": 15}, {"name": "竺院团学汇", "max": 10} ],
        "filter": r"某某大学|某某",      # 仅兜底；名单内完整昵称一律优先收录
    }

关于名称匹配：微信公众平台**昵称全平台唯一**，所以按**完整昵称**精确匹配即可唯一命中，
命中后锁定 `fakeid` 缓存到 `_work/wechat/wxmp-fakeid.json`，之后不再依赖搜索。
因此「竺院团学汇」「浙里等宁」这类**名称里不含学校关键词**的号也能稳定收录；
`filter` 正则只在精确匹配落空时兜底，防止误收同名社会号。

合并进 data/<校区>.json 时会自动：
  * 按文章链接（i）去重，保留旧条目、覆盖同 id 条目；
  * 走 refresh.py 的 90 天保留期与写入保护；
  * 在 source_registry 里为每个公众号登记一条来源（group=wechat），
    状态为 最近更新 / 无新内容 / 采集失败 / 待接入，页面「信息来源」可直接看到。
"""

import argparse
import concurrent.futures as cf
import datetime
import importlib.util
import json
import os
import random
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
WORK_DIR = os.path.join(HERE, "_work")

# 统一用北京时间：不能用 time.strftime / datetime.now() —— 它们取运行机器的本地时区，
# 云端（UTC）机器上会把 UTC 时间贴上 +08:00 标签（页面显示差 8 小时）。
CST = datetime.timezone(datetime.timedelta(hours=8))    # 中国无夏令时，固定 +08:00


def now_iso_cst():
    """当前北京时间，形如 2026-10-11T12:34:56+08:00。"""
    return datetime.datetime.now(CST).isoformat(timespec="seconds")
# 公众号专属工作目录：官网链路（refresh.py / fetch_campus.py）的产物放在 _work/ 根下，
# 本脚本的 cookies / token / 名单 / fakeid 缓存 / 中间结果全部收在 _work/wechat/ 里。
# 这样「稳定的官网链路」与「不稳定的公众号链路」在磁盘上就是两堆文件，
# 清理、备份、排查时互不牵连（见 docs/目录结构.md）。
WECHAT_DIR = os.path.join(WORK_DIR, "wechat")
# 旧版本把公众号文件直接放在 _work/ 根下；这些名字保留用于**只读**回退，
# 首次运行时会自动迁移到 WECHAT_DIR（迁移后旧文件不再被读）。
LEGACY_WORK_DIR = WORK_DIR

# --------------------------------------------------------------------------
# 每校要抓的公众号清单
#
# 实际抓取名单以外部文件为准（推荐维护方式，见下），这里是**兜底默认值**：
#   _work/wechat/wxmp-accounts.json          # 全局，所有校区共用
#   _work/wechat/wxmp-accounts-<校区>.json   # 分校区，优先于全局
# 内置 accounts 与外部文件会**按规范化昵称去重合并**，所以外部名单里已有的号不会重复。
#   name   : 公众号**完整昵称**，交给 wxmp 搜索 fakeid
#   max    : 本次最多取回几篇（可选，默认取 --count）
#   fakeid : 已知 fakeid 时直接填，省掉一次搜索（可选）
#
# 关于「完整昵称即收录」
# --------------------
# 微信公众平台的**昵称是全平台唯一的**（改名后旧名也不会立刻释放），所以按完整昵称
# 精确匹配就能唯一命中一个号。因此本脚本的判定规则是：
#   ① 名单里给出的完整昵称，规范化（去空白/全角转半角/统一大小写）后**精确相等**即收录；
#   ② 命中后即锁定它的 fakeid 并缓存到 _work/wechat/wxmp-fakeid.json，之后不再依赖搜索；
#   ③ filter 正则**只作为兜底**：万一精确匹配没命中（搜索返回了擦边号），
#      才用 filter 判断昵称是否属于本校，避免误收同名社会号。
# 这样「竺院团学汇」「浙里等宁」这类**名称里不含学校关键词**的号也能被稳定收录。
# --------------------------------------------------------------------------
WECHAT_CONFIG = {
    "zju": {
        "campus_name": "浙大知道",
        "filter": r"浙江大学|浙大|ZJU",
        "accounts": [
            {"name": "浙江大学", "max": 15},
            {"name": "浙江大学本科招生", "max": 10},
        ],
    },
    "sjtu": {
        "campus_name": "上交知道",
        "filter": r"上海交通大学|上海交大|交大|SJTU",
        "accounts": [
            {"name": "上海交通大学", "max": 15},
        ],
    },
    "xjtu": {
        "campus_name": "西交知道",
        "filter": r"西安交通大学|西安交大|交大|XJTU",
        "accounts": [
            {"name": "西安交通大学", "max": 15},
        ],
    },
}

FAKEID_CACHE = "wxmp-fakeid.json"      # fakeid 缓存文件名（落在 _work/wechat/ 下）
# 同一份 fakeid 缓存按【凭据来源】分文件存。原因见 load_fakeid_cache()：
# fakeid 不是纯公开数据，它是「某个登录账号搜出来的结果」，跨账号复用容易触发风控。
FAKEID_SIBLING_CACHE = "wxmp-fakeid-*.json"

# 限流（ret=200013 freq control）的两种粒度，处置方式完全不同：
#   · 账号级：这份 cookies 被盯上了。换一份 cookies 就能绕开（见 load_cookies 的轮换池）。
#   · fakeid 级：单个号被限制。同一份 cookies 抓其它号仍然正常。
# 把两种情况分开记，才能在报告里给出**可执行的建议**，而不是笼统地说「等一天」。
BLOCK_FILE = "wxmp-blocked.json"       # 限流记忆：谁被限了、什么时候、什么粒度


# --------------------------------------------------------------------------
# 请求节奏：给固定间隔加**抖动**
#
# 原来每一步都是 `time.sleep(1.0)` / `sleep(1.5)` —— 固定间隔本身就是个特征：
# 一个「人」不会每隔正好 1.000 秒点一次接口，脚本才会。加了 ±35% 的随机抖动后，
# 请求间隔变成一个区间而不是一个常数，远比固定值更接近真人操作。
#
# 这不是对抗风控，是**避免自己的行为特征过于醒目**——同时也让服务端压力更平滑。
# --------------------------------------------------------------------------
PACE_JITTER = 0.35


def gentle_sleep(base=1.0, label=""):
    """睡 base 秒 ± PACE_JITTER 的随机量。传 0 或负数则直接返回（测试用）。"""
    if base <= 0:
        return 0.0
    delay = base * (1.0 + random.uniform(-PACE_JITTER, PACE_JITTER))
    delay = max(0.05, delay)
    if os.environ.get("WECHAT_PACE_VERBOSE"):
        print(f"      · 等待 {delay:.2f}s {label}")
    time.sleep(delay)
    return delay


# --------------------------------------------------------------------------
# 公众号工作目录：_work/wechat/
#   所有本脚本读写的文件（cookies / token / 名单 / fakeid / wechat-items-*）都在这里。
#   为了兼容旧布局（文件散在 _work/ 根下），提供两个只读回退：
#     find_wechat_file(name)  在 WECHAT_DIR 找不到时回退到 LEGACY_WORK_DIR（旧位置）
#     migrate_legacy_files()  首次运行时把旧文件搬到 WECHAT_DIR，不覆盖已存在的新文件
# --------------------------------------------------------------------------
def find_wechat_file(name, legacy_ok=True):
    """按 「_work/wechat/name → _work/name」 的顺序找一个文件，返回路径或 None。"""
    path = os.path.join(WECHAT_DIR, name)
    if os.path.exists(path):
        return path
    if legacy_ok:
        old = os.path.join(LEGACY_WORK_DIR, name)
        if os.path.exists(old):
            return old
    return None


def wechat_path(name):
    """公众号文件在**新位置**的标准路径（写文件一律用它）。"""
    return os.path.join(WECHAT_DIR, name)


# 旧位置里所有属于公众号链路的名字（用于一次性迁移 + 文档对照）
MIGRATE_GLOBS = (
    "wxmp-cookies.json", "wxmp-cookies-*.json",
    "wxmp-cookie-header.txt", "wxmp-cookie-header-*.txt",
    "wxmp-cookie-header-*.txt.*",          # 轮换池：wxmp-cookie-header-pool2.txt 等
    "wxmp-token.txt", "wxmp-accounts.json", "wxmp-accounts-*.json",
    FAKEID_CACHE, FAKEID_SIBLING_CACHE, "wechat-items-*.json", "wechat-*.txt",
)


def migrate_legacy_files(verbose=True):
    """把散落在 _work/ 根下的公众号文件搬进 _work/wechat/（旧布局 → 新布局）。

    规则：
      * 只搬公众号专属的名字（见 MIGRATE_GLOBS），官网产物（items-*/registry-*/run.log）不动；
      * 目标已存在时**不覆盖**（保留新目录里的版本），只把旧的删掉并提示；
      * 任何异常都不影响主流程，最坏情况就是文件留在原地的只读回退仍能读到。
    返回搬迁的文件名列表。
    """
    import glob as _glob
    moved = []
    try:
        os.makedirs(WECHAT_DIR, exist_ok=True)
    except OSError:
        return moved
    for pattern in MIGRATE_GLOBS:
        for src in _glob.glob(os.path.join(LEGACY_WORK_DIR, pattern)):
            if not os.path.isfile(src):
                continue                       # 只处理文件，跳过同名目录
            name = os.path.basename(src)
            dst = os.path.join(WECHAT_DIR, name)
            if os.path.abspath(src) == os.path.abspath(dst):
                continue
            try:
                if os.path.exists(dst):
                    os.remove(src)             # 新位置已有更新版本 → 删掉旧副本
                else:
                    shutil.move(src, dst)
                    moved.append(name)
            except OSError:
                continue
    if moved and verbose:
        print(f"· 已把 {len(moved)} 个公众号文件迁进 _work/wechat/：{', '.join(sorted(moved))}")
    return moved

# --------------------------------------------------------------------------
# 分类 / 字段：与 fetch_campus.py 保持一致（含活动识别）
# --------------------------------------------------------------------------
ACTIVITY_CATEGORIES = {"文体活动", "社团公益", "讲座活动", "学习成长", "实习就业", "交流访学"}
CATEGORY_RULES = [
    ("讲座活动", r"讲座|报告会|论坛|沙龙|学术报告|讲堂|研讨会|seminar|报告题目|学术前沿"),
    ("文体活动", r"演出|音乐会|话剧|歌剧|展映|展览|比赛|竞赛|运动会|文艺|晚会|歌手|舞蹈|球赛|健身|马拉松|摄影展"),
    ("社团公益", r"社团|志愿|公益|支教|义工|募捐|无偿献血|红十字|社会实践"),
    ("实习就业", r"招聘|就业|实习|双选会|宣讲会|求职|岗位|校园招聘|career|选调"),
    ("交流访学", r"访学|交换|留学|国际交流|海外|出国|联合培养|暑期学校|冬令营|夏令营|游学"),
    ("学习成长", r"培训|工作坊|workshop|训练营|成长|心理|辅导|技能|读书|悦读"),
    ("学业教务", r"选课|考试|教务|教学|培养方案|学分|学位|答辩|开题|转专业|推免|招生|课程|开学|注册|学籍"),
    ("奖助评优", r"奖学金|奖助|评优|助学|资助|表彰|标兵|优秀学生|国家奖学金"),
    ("学术科研", r"科研|基金|项目申报|论文|课题组|实验室|学术会议|重点研发|成果|专利"),
    ("校园服务", r"后勤|维修|班车|食堂|宿舍|医疗|医保|安全|保卫|招标|采购|停电|停水|校车|图书馆|一卡通"),
    ("院系资讯", r"学院|学部|研究院|系所|党支部|党委|教工|师生"),
    ("校园动态", r"开学|毕业|典礼|会议|调研|走访|来访|签约|合作|建设|周年|诞辰|荣获|获奖"),
]
CATEGORIES = ["讲座活动", "文体活动", "社团公益", "实习就业", "交流访学", "学习成长",
              "学业教务", "奖助评优", "学术科研", "校园服务", "院系资讯", "校园动态"]
DEFAULT_CATEGORY = "校园动态"

RETENTION_DAYS = 90


def classify(text):
    for name, pat in CATEGORY_RULES:
        if re.search(pat, text or ""):
            return name
    return DEFAULT_CATEGORY


def summarize(text, limit):
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text[:limit]


def strip_tags(html):
    html = re.sub(r"(?is)<script.*?</script>", " ", html or "")
    html = re.sub(r"(?is)<style.*?</style>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = (html.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    return re.sub(r"\s+", " ", html).strip()


# --------------------------------------------------------------------------
# wxmp 接入层：把库的返回统一成普通 dict，缺库/缺 token 时优雅降级
# --------------------------------------------------------------------------
def load_wxmp():
    try:
        from wxmp import WxMPAPI  # noqa: WPS433
        return WxMPAPI
    except Exception as exc:  # noqa: BLE001
        print(f"! 未能加载 wxmp（{exc}）——请先 `pip install wxmp`（需 Python >= 3.10）")
        return None


class MpClient:
    """直连公众平台后台接口的轻量客户端。

    为什么不用 wxmp 的 WxMPAPI.fetch_fakeid / fetch_article_list？
    因为 wxmp 2.5.0 的 pydantic 模型把结果字段硬编码成 `arr`，而当前接口
    （视账号/版本）有时返回 `list`，会让库抛 ValidationError（虽然 base_resp.ret=0 成功）。
    这里直接请求并**容错取字段**（arr / list 都认），保持稳定。
    """

    DOMAIN = "https://mp.weixin.qq.com"

    def __init__(self, cookies, token=None):
        import requests
        self.cookies = cookies
        self.token = token
        self.session = requests.Session()
        try:
            from fake_useragent import UserAgent
            ua = UserAgent().random
        except Exception:  # noqa: BLE001
            ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        self.headers = {"User-Agent": ua, "Referer": "https://mp.weixin.qq.com/"}

    def ensure_token(self, forced=None):
        """取 token：优先 forced / cookies 里的 token；否则请求首页从重定向 URL 提取。"""
        if forced:
            self.token = str(forced)
            return self.token
        try:
            res = self.session.get(self.DOMAIN, headers=self.headers,
                                   cookies=self.cookies, verify=False, timeout=15)
            m = re.findall(r"token=(\d+)", res.url)
            if m:
                self.token = m[0]
                return self.token
        except Exception:  # noqa: BLE001
            pass
        return self.token

    def _get(self, path, params, timeout=15):
        import warnings
        warnings.filterwarnings("ignore")
        params = {**params, "token": self.token, "lang": "zh_CN", "f": "json", "ajax": 1}
        res = self.session.get(self.DOMAIN + path, params=params,
                               headers=self.headers, cookies=self.cookies,
                               verify=False, timeout=timeout)
        return res.json()

    def search_biz(self, query, begin=0, count=10):
        data = self._get("/cgi-bin/searchbiz",
                         {"action": "search_biz", "begin": begin, "count": count,
                          "query": query})
        ret = (data.get("base_resp") or {}).get("ret", 0)
        rows = data.get("list") or data.get("arr") or []
        return rows, ret

    def list_articles(self, fakeid, begin=0, count=5, is_publish=False):
        data = self._get("/cgi-bin/appmsg",
                         {"action": "list_ex", "begin": begin, "count": count,
                          "fakeid": fakeid, "type": 9, "query": ""})
        ret = (data.get("base_resp") or {}).get("ret", 0)
        rows = data.get("app_msg_list") or data.get("list") or []
        return rows, ret


def parse_cookie_header(text):
    """把浏览器复制的「Cookie: k=v; k2=v2」请求头整行解析成 dict。"""
    text = re.sub(r"(?i)^\s*cookie:\s*", "", text.strip())   # 去掉「Cookie:」前缀
    pairs = {}
    for part in re.split(r";\s*", text):
        if "=" in part:
            k, _, v = part.partition("=")
            k = k.strip()
            v = v.strip()
            if k and v:
                pairs[k] = v
    return pairs


def load_cookies(campus):
    """按 校区专用 → 全局 的顺序找 cookies 文件，返回 (cookies, 来源路径)。

    支持两种文件内容：
      ① JSON dict（标准方式）；
      ② 纯文本：浏览器 F12 → Network → 请求头里复制的整行「Cookie: ...」
         （存成 _work/wechat/wxmp-cookie-header.txt 即可，脚本自动解析——推荐，零转录错误）。
    """
    # ② 原始 Cookie 头文本（最可靠，直接整行粘贴）
    for name in (f"wxmp-cookie-header-{campus}.txt", "wxmp-cookie-header.txt"):
        path = find_wechat_file(name)
        if path:
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read().strip()
                pairs = parse_cookie_header(text)
                if pairs:
                    return pairs, path
            except Exception as exc:  # noqa: BLE001
                print(f"! Cookie 头文件 {path} 读不出来：{exc}")
    # ① JSON
    for name in (f"wxmp-cookies-{campus}.json", "wxmp-cookies.json"):
        path = find_wechat_file(name)
        if path:
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict) and data:
                    return data, path
            except Exception as exc:  # noqa: BLE001
                print(f"! cookies 文件 {path} 读不出来：{exc}")
    return None, None


# --------------------------------------------------------------------------
# 凭据轮换池：缓解「账号级限流」
#
# 微信对 appmsg（历史文章列表）接口的限制里，有一类是**绑在登录账号上**的：
# 同一个「公众平台登录态」抓得多了，这个号就被盯上，之后连搜索都拿不到东西。
# 这种限制**等**是等不好的——唯一的出路是**换一个登录账号**。
#
# 所以这里支持把多份 cookies 一起丢进来轮换。命名约定（都放在 _work/wechat/ 下）：
#
#     wxmp-cookie-header.txt            ← 第 1 份（原有命名，照常生效）
#     wxmp-cookie-header-pool2.txt      ← 第 2 份
#     wxmp-cookie-header-pool3.txt      ← 第 3 份 ...
#
# 或 JSON 形式：
#     wxmp-cookies.json                 ← 第 1 份
#     wxmp-cookies-pool2.json           ← 第 2 份 ...
#
# 更省事的做法：把所有行塞进**同一个文件**，一行一份 Cookie，脚本按行拆：
#     wxmp-cookie-header-pool.txt
#
# 轮换是**确定性**的（每天选一份），不是随机的——随机会让「今天到底用的哪个号」
# 变成玄学，出问题时无法复现。
# --------------------------------------------------------------------------
def _cred_slot(path):
    """从文件名判断这是第几份凭据：pool2 → 2，poolN → N，其它 → 1。"""
    m = re.search(r"pool(\d+)", os.path.basename(path))
    return int(m.group(1)) if m else 1


def _multiline_cookie_file(path):
    """解析「一份文件里多行 Cookie」的写法，每行一份。返回 [(cookies, 描述)]。"""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
    except Exception:  # noqa: BLE001
        return []
    if len(lines) < 2:
        return []
    out = []
    for idx, ln in enumerate(lines, 1):
        pairs = parse_cookie_header(ln)
        if pairs:
            out.append((pairs, f"{os.path.basename(path)} 第 {idx} 行"))
    return out


def _scan_dirs():
    """扫描新目录 + 旧目录（后者只读回退，兼容未迁移的旧布局）。"""
    return [WECHAT_DIR, LEGACY_WORK_DIR]


def _is_cred_file(base, campus, ext):
    """判断文件名是不是「凭据文件」，并返回 (是否校区专用, slot)。

    合法形式（ext 为 txt 或 json）：
        wxmp-cookie-header.txt / wxmp-cookies.json                → slot 1，全局
        wxmp-cookie-header-zju.txt / wxmp-cookies-zju.json        → slot 1，校区专用
        wxmp-cookie-header-pool2.txt / wxmp-cookies-pool2.json    → slot 2，全局
        wxmp-cookie-header-pool2-zju.txt                          → slot 2，校区专用
    多行汇总文件（wxmp-cookie-header-pool.txt）不在这里处理，见 ①。
    """
    if not base.startswith("wxmp-"):
        return None
    if not base.endswith(ext):
        return None
    core = base[len("wxmp-"):-len(ext)]           # cookie-header / cookies / cookie-header-pool2 …
    if core in ("cookie-header", "cookies"):
        return (False, 1)
    # 带 poolN 的无校区后缀
    mo = re.fullmatch(r"(?:cookie-header|cookies)-pool(\d+)", core)
    if mo:
        return (False, int(mo.group(1)))
    # 带校区后缀：<kind>-<campus> / <kind>-poolN-<campus>
    for kind in ("cookie-header", "cookies"):
        if core.startswith(kind + "-"):
            tail = core[len(kind) + 1:]
            mo = re.fullmatch(r"pool(\d+)-(.+)", tail)
            if mo and mo.group(2) == campus:
                return (True, int(mo.group(1)))
            if tail == campus:
                return (True, 1)
    return None


def list_credential_pool(campus):
    """收集所有可用凭据，返回 [(cookies, 描述)]，按 slot 排序（第 1 份在最前）。

    优先级：校区专用 > 全局。同一 slot 里两种格式都有时，Cookie 头文本优先（转录错误少）。
    """
    import glob as _glob

    found = {}   # (slot, scoped) -> (cookies, 描述)

    def put(slot, campus_scoped, cookies, desc):
        key = (slot, campus_scoped)
        if key not in found:
            found[key] = (cookies, desc)

    # ① 多行汇总文件（最省事）：一行一份 Cookie
    multiline = (f"wxmp-cookie-header-pool-{campus}.txt", "wxmp-cookie-header-pool.txt")
    for name in multiline:
        path = find_wechat_file(name)
        if path:
            for idx, (ck, desc) in enumerate(_multiline_cookie_file(path), 1):
                put(1000 + idx, name.endswith(f"-{campus}.txt"), ck, desc)

    # ② Cookie 头文本（单份）
    # ③ JSON（单份）
    for ext, reader in ((".txt", _read_cookie_text), (".json", _read_cookie_json)):
        for base_dir in _scan_dirs():
            for path in sorted(_glob.glob(os.path.join(base_dir, "wxmp-*" + ext))):
                base = os.path.basename(path)
                # 只排除「多行汇总文件」（pool.txt / pool-<校区>.txt），
                # 不能连 poolN.txt 一起排掉——那正是第 2、3 份凭据。
                if re.fullmatch(r"wxmp-cookie-header-pool(-\w+)?\.txt", base):
                    continue
                hit = _is_cred_file(base, campus, ext)
                if not hit:
                    continue
                scoped, slot = hit
                ck = reader(path)
                if ck:
                    put(slot, scoped, ck, base)

    # 校区专用排在全局前面：同一份凭据，优先用校区专用的那个文件
    ordered = sorted(found.items(), key=lambda kv: (-kv[0][1], kv[0][0]))
    return [v for _k, v in ordered]


def _read_cookie_text(path):
    try:
        return parse_cookie_header(open(path, encoding="utf-8").read().strip())
    except Exception:  # noqa: BLE001
        return None


def _read_cookie_json(path):
    try:
        data = json.load(open(path, encoding="utf-8"))
        return data if isinstance(data, dict) and data else None
    except Exception:  # noqa: BLE001
        return None


def pick_credential(campus, index=0):
    """取轮换池里的第 index 份。池子为空时回退到 load_cookies() 的老行为。"""
    pool = list_credential_pool(campus)
    if not pool:
        return load_cookies(campus)
    if index >= len(pool):
        index = len(pool) - 1        # 轮完一圈就停在最后一份，不报错
    return pool[index]


def pool_index_for_today(campus):
    """按「日期」在池子里挑一份——同一天多次运行用同一份，行为可复现。"""
    pool = list_credential_pool(campus)
    if len(pool) <= 1:
        return 0
    return datetime.date.today().toordinal() % len(pool)


# --------------------------------------------------------------------------
# 限流记忆：区分「账号级」与「fakeid 级」
# --------------------------------------------------------------------------
def load_block_state():
    path = find_wechat_file(BLOCK_FILE)
    if not path:
        return {"credentials": {}, "targets": {}}
    try:
        data = json.load(open(path, encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("credentials", {})
            data.setdefault("targets", {})
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"credentials": {}, "targets": {}}


def save_block_state(state):
    try:
        path = os.path.join(WECHAT_DIR, BLOCK_FILE)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
    except OSError as exc:  # noqa: BLE001
        print(f"! 限流记录写不进去（不影响本轮）：{exc}")


def is_recently_blocked(kind, key, hours=6):
    """这个「凭据」/「fakeid」是不是最近才被限过？是的话本轮可以跳过，省得白跑。"""
    state = load_block_state()
    rec = (state.get(kind) or {}).get(key) or {}
    ts = rec.get("ts") or ""
    if not ts:
        return False
    try:
        when = datetime.datetime.fromisoformat(ts)
    except ValueError:
        return False
    elapsed = datetime.datetime.now(CST) - when.astimezone(CST)
    return elapsed < datetime.timedelta(hours=hours)


def note_block(kind, key, note=""):
    state = load_block_state()
    bucket = state.setdefault(kind, {})
    rec = bucket.setdefault(key, {})
    rec["ts"] = now_iso_cst_parts()
    rec["count"] = int(rec.get("count") or 0) + 1
    if note:
        rec["note"] = note
    save_block_state(state)
    return rec


def now_iso_cst_parts():
    """ISO 时间戳（带时区），供 datetime.fromisoformat 回读。"""
    return datetime.datetime.now(CST).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# 公众号名单：加载（内置 + 外部文件）、昵称规范化、精确匹配、fakeid 缓存
# --------------------------------------------------------------------------
def normalize_name(name):
    """昵称规范化：去所有空白、全角转半角、统一小写，用于「同一昵称」的稳健比对。"""
    if not name:
        return ""
    out = []
    for ch in name:
        code = ord(ch)
        if code == 0x3000:          # 全角空格
            continue
        if 0xFF01 <= code <= 0xFF5E:  # 全角 ! ~ → 半角
            ch = chr(code - 0xFEE0)
        out.append(ch)
    return re.sub(r"\s+", "", "".join(out)).lower()


def load_accounts(campus):
    """合并「内置 WECHAT_CONFIG」+「外部名单文件」，返回 accounts 列表。

    外部名单文件（推荐用这个维护完整名单，不必改脚本）：
        _work/wechat/wxmp-accounts.json          # 全局
        _work/wechat/wxmp-accounts-<校区>.json   # 分校区，优先合并

    支持两种写法：
        ["浙江大学", "浙大竺院人", "竺院团学汇"]              # 纯名称数组
        {"accounts": [{"name":"浙江大学","max":15,"fakeid":"MzA3..."}]}  # 带参数
        {"浙江大学": {"max": 15}, "竺院团学汇": {}}          # 名称 → 参数
    """
    accounts, index = [], {}

    def add(entry):
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict):
            return
        name = (entry.get("name") or "").strip()
        if not name:
            return
        key = normalize_name(name)
        max_v = entry.get("max")
        fakeid = entry.get("fakeid")
        if key in index:
            # 已经见过这个名字(通常来自内置兜底名单)。
            # 外部名单是「更新」而不只是「去重」：把外部给的 max/fakeid 补进去，
            # 否则写成 {"浙江大学": {"max": 15}} 会因为是 dict 写法而被静默丢掉最大值，
            # 一律退回 args.count —— 用户会觉得"改了没用"。
            prev = index[key]
            if max_v is not None:
                prev["max"] = max_v
            if fakeid:
                prev["fakeid"] = fakeid
            return
        index[key] = {"name": name, "max": max_v, "fakeid": fakeid}
        accounts.append(index[key])

    for entry in WECHAT_CONFIG[campus]["accounts"]:
        add(entry)

    for name in (f"wxmp-accounts-{campus}.json", "wxmp-accounts.json"):
        path = find_wechat_file(name)
        if not path:
            continue
        try:
            if path.endswith(".txt"):
                with open(path, encoding="utf-8") as fh:
                    lines = fh.read().splitlines()
                raw = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
            else:
                with open(path, encoding="utf-8") as fh:
                    raw = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            print(f"! 名单文件 {path} 读不出来：{exc}")
            continue
        if isinstance(raw, dict) and "accounts" in raw:
            raw = raw["accounts"]
        if isinstance(raw, dict):
            raw = [{"name": k, **(v or {})} for k, v in raw.items()]
        if isinstance(raw, list):
            for entry in raw:
                add(entry)
        print(f"· 合并外部名单 {os.path.basename(path)}：当前共 {len(accounts)} 个号")
    return accounts


def _fakeid_cache_name(cred_path=None):
    """按凭据来源给 fakeid 缓存分文件。

    为什么不能所有凭据共用一份缓存：fakeid 是「某个登录账号搜出来的结果」，
    不是公开常量。把 A 账号搜到的 fakeid 直接喂给 B 账号去拉文章，
    在微信看来就是「B 突然开始访问一个它从没搜过的号」——这正是风控的典型特征。
    分成 `wxmp-fakeid.json` / `wxmp-fakeid-pool2.json` 后，每份凭据只复用自己的战果。

    传 None（未指定凭据）时用默认名，保持对旧缓存的兼容。
    """
    if not cred_path:
        return FAKEID_CACHE
    slot = _cred_slot(cred_path)
    if slot <= 1:
        return FAKEID_CACHE
    return f"wxmp-fakeid-pool{slot}.json"


def load_fakeid_cache(cred_path=None):
    path = find_wechat_file(_fakeid_cache_name(cred_path))
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_fakeid_cache(cache, cred_path=None):
    path = wechat_path(_fakeid_cache_name(cred_path))
    try:
        os.makedirs(WECHAT_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=1, sort_keys=True)
    except Exception as exc:  # noqa: BLE001
        print(f"! fakeid 缓存写入失败：{exc}")


def pick_exact(rows, want_name):
    """在一堆搜索结果里挑出「昵称规范化后精确等于目标名」的那个号。

    返回 (nickname, fakeid, exact?)：
      * 精确命中 → exact=True，直接采用；
      * 没精确命中但有结果 → 返回第一个候选，exact=False（调用方再用 filter 兜底校验）；
      * 完全没结果 → (None, None, False)。
    """
    want = normalize_name(want_name)
    candidates = []
    for row in rows:
        nick = row_get(row, "nickname", "name") or ""
        fakeid = row_get(row, "fakeid", "fake_id")
        if nick and fakeid:
            candidates.append((nick, fakeid))
    for nick, fakeid in candidates:
        if normalize_name(nick) == want:
            return nick, fakeid, True
    if candidates:
        return candidates[0][0], candidates[0][1], False
    return None, None, False


ROW_KEYS = ("arr", "list", "items", "app_msg_list")


def response_rows(resp):
    """wxmp 的响应对象里，列表一般挂在 .arr / .list / .items 上，统一取出。"""
    if resp is None:
        return []
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for key in ROW_KEYS:
            if isinstance(resp.get(key), list) and resp[key]:
                return resp[key]
        return []
    for attr in ROW_KEYS:
        rows = getattr(resp, attr, None)
        if isinstance(rows, list) and rows:
            return rows
    return []


def row_get(row, *names):
    for name in names:
        if isinstance(row, dict):
            value = row.get(name)
        else:
            value = getattr(row, name, None)
        if value not in (None, ""):
            return value
    return None


def ts_to_iso(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    try:
        return datetime.datetime.fromtimestamp(value, CST).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    except (OverflowError, OSError, ValueError):
        return ""


def make_item(url, title, published, source, body):
    """产出与 fetch_campus.py 完全一致的 item 结构（group 固定为 wechat）。"""
    category = classify(title + " " + (body or "")[:120])
    intent = "activity" if category in ACTIVITY_CATEGORIES else "information"
    item = {
        "i": url, "t": summarize(title, 200) or url, "s": source, "si": None,
        "d": published or "", "u": url, "c": category, "g": intent,
        "en": "", "ens": "", "dt": {"sg": "wechat", "og": source},
    }
    if body and len(body) >= 40:
        item["dt"]["tx"] = body[:520]
    return item


def search_account(api, name, retries=2):
    """搜索完整昵称，优先返回**精确匹配**的号。

    微信公众号昵称全平台唯一，所以按完整名称搜到的结果里，规范化后精确相等的那个
    就是目标号；返回 dict 里的 `exact` 标明是否精确命中。
    """
    for attempt in range(retries + 1):
        try:
            rows, ret = api.search_biz(name, count=10)
            if ret not in (0, None):
                print(f"    · 搜索「{name}」接口返回 ret={ret}（token 可能失效）")
                return None
            nick, fakeid, exact = pick_exact(rows, name)
            if nick and fakeid:
                return {"nickname": nick, "fakeid": fakeid, "exact": exact, "query": name}
            return None
        except Exception as exc:  # noqa: BLE001
            if attempt >= retries:
                print(f"    · 搜索「{name}」失败：{exc}")
                return None
            gentle_sleep(1.5 * (attempt + 1), "（搜索退避）")
    return None


def list_articles(api, fakeid, count, pages, retries=1, want_block=False):
    """按页拉历史文章列表，返回 [{title, link, published, digest, cover}]。

    `want_block=True` 时返回 `(rows, blocked)` 二元组，让调用方能区分
    「这个号没有新文章」和「这个号被限流了」——这两者对后续处置的含义完全不同。

    注意：微信对 `appmsg`（历史文章列表）接口有**账号级频率限制**，新登录 / 久未使用的
    账号常返回 ret=200013（freq control），通常需等待数小时至 1 天才解禁。
    这种限流与参数无关，等待也无效，所以**不做长退避**，识别到就快速返回 []。
    调用方（crawl_wechat）会在连续多号被限流时中止本轮，避免空转。
    真正的解法是**换一份凭据**（见 list_credential_pool），而不是等。
    """
    out, seen, per_page = [], set(), 5
    fetched = 0
    page = 0
    blocked = False
    while fetched < count and page < pages:
        begin = page * per_page
        want = min(per_page, count - fetched)
        rows = None
        for attempt in range(retries + 1):
            try:
                rows, ret = api.list_articles(fakeid, begin=begin, count=want)
                if ret in (200013, 200003, -6):        # freq control
                    blocked = True
                    if attempt >= retries:
                        rows = []
                        break
                    gentle_sleep(2.0, "（限流退避）")
                    continue
                if ret not in (0, None):
                    if ret in (200002, -1):            # 无效 fakeid / 会话失效
                        print(f"    · 接口返回 ret={ret}（fakeid 无效或会话失效）")
                    else:
                        print(f"    · 拉取文章列表 ret={ret}")
                    rows = []
                    break
                break
            except Exception as exc:  # noqa: BLE001
                if attempt >= retries:
                    print(f"    · 拉取文章列表失败（第 {page + 1} 页）：{exc}")
                    rows = []
                    break
                gentle_sleep(1.5 * (attempt + 1), "（失败退避）")
        if not rows:
            break
        added = 0
        for row in rows:
            link = row_get(row, "link", "url", "content_url") or ""
            title = row_get(row, "title", "name") or ""
            if not link or not title or link in seen:
                continue
            seen.add(link)
            out.append({
                "title": title,
                "link": link,
                "published": ts_to_iso(row_get(row, "update_time", "create_time", "datetime")),
                "digest": row_get(row, "digest", "summary") or "",
            })
            added += 1
        fetched += added
        if added == 0:
            break
        page += 1
        gentle_sleep(1.0, "（翻页）")     # 温柔一点，别把后台接口打疼
    if blocked and not out:
        print("    ⚠ 该号的「历史文章列表」被限流（ret=200013 freq control）。")
    return (out[:count], blocked) if want_block else out[:count]


# --------------------------------------------------------------------------
# 公开「合集」接口：唯一一条**零凭据、不受限流**的补充路径
#
# ⚠ 2026-10-27 起，这条路径已独立成包：`albums/`（`python -m albums`）。
#   本文件里的实现**保留但不再演进** —— 请优先用新包，理由：
#     · 合集不需要 cookies，却要跟着本文件一起加载 wxmp/requests/pandas；
#     · 原来抓完直接 merge 进 data/<校区>.json，和官网站点产物搅在一起；
#     · 公众号名只能猜（本文件的 account 取值是 alb.name 或「微信公众号」），
#       而合集接口的 base_info.nickname 直接给真名；
#     · 没有增量水位，每轮从头翻；单集出错会把整轮记成失败。
#   新包已实测：浙大 4 个合集 637 篇全量 38s、增量 3.8s，且不写 data/。
#   这里保留是为了不破坏 `--albums-only` 的旧用法与 tests/unit_wechat.py。
#
#     GET /mp/appmsgalbum?action=getalbum&__biz=<biz>&album_id=<id>&f=json
#
# 实测：返回 200 + 合法 JSON，**不需要任何登录态**。参数不对时也只是
# `{"base_resp":{"ret":10004}}`，说明接口本身是开放的。
#
# 这条路径的价值正好补上 appmsg 的短板：
#   · appmsg 有账号级限流（200013）且凭据会过期；
#   · 合集接口没有限流、不用凭据，**限流期间依然能拿到东西**。
#
# 代价是覆盖范围小：合集里只有作者**手工整理进去**的文章，不是全量历史。
# 所以它是补充源，不是替代品。
# --------------------------------------------------------------------------
ALBUM_FILE = "wxmp-albums.json"        # 校区 → [{biz, album_id, name}]
ALBUM_API = "https://mp.weixin.qq.com/mp/appmsgalbum"


def load_albums(campus):
    """读合集清单：_work/wechat/wxmp-albums-<校区>.json 或 wxmp-albums.json。

    可写形式（都支持）：
        ["MzA5...:1234567890", ...]                       # "biz:album_id"
        {"albums": [{"biz": "...", "album_id": "...", "name": "招生简章"}]}
        {"zju": [{"biz": "...", "album_id": "..."}]}      # 按校区分组
    """
    for name in (f"wxmp-albums-{campus}.json", ALBUM_FILE):
        path = find_wechat_file(name)
        if not path:
            continue
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"! 合集清单 {path} 读不出来：{exc}")
            continue
        raw = data.get(campus) if isinstance(data, dict) and campus in data else data
        if isinstance(raw, dict):
            raw = raw.get("albums") or []
        out = []
        for entry in raw or []:
            if isinstance(entry, str) and ":" in entry:
                biz, _, album = entry.partition(":")
                out.append({"biz": biz.strip(), "album_id": album.strip(), "name": ""})
            elif isinstance(entry, dict) and entry.get("biz") and entry.get("album_id"):
                out.append({"biz": str(entry["biz"]).strip(),
                            "album_id": str(entry["album_id"]).strip(),
                            "name": entry.get("name") or ""})
        if out:
            return out
    return []


def _album_json(params, timeout=12):
    """调合集接口，返回解析后的 dict；失败返回 {}。

    **不需要 cookies** —— 这是整条链路的立足点：完全不碰 `appmsg`（历史文章列表），
    也就不受账号级限流 ret=200013 的影响。
    """
    import urllib.parse
    url = f"{ALBUM_API}?{urllib.parse.urlencode(params)}"
    headers = {
        "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"),
        "Referer": "https://mp.weixin.qq.com/",
    }
    try:
        import requests
        data = requests.get(url, timeout=timeout, headers=headers, verify=False).json()
    except ImportError:
        # 优先用 requests（与主链路一致）；**没有 requests 时退回标准库 urllib**。
        # 这条路径是限流期的兜底，绝不能因为「装不上依赖」就整个失效——
        # 它本来就不需要 cookies，也就不该需要任何第三方库。
        try:
            import ssl
            import urllib.request
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as fh:
                data = json.loads(fh.read().decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            print(f"    · 合集接口请求失败（urllib）：{exc}")
            return {}
    except Exception as exc:  # noqa: BLE001
        print(f"    · 合集接口请求失败：{exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _album_page(biz, album_id, begin_msgid=0, begin_itemidx=0, timeout=12, count=20):
    """取合集的一页。返回 (条目 list, 合集标题, 是否还有下一页)。"""
    data = _album_json({
        "action": "getalbum", "__biz": biz, "album_id": album_id, "f": "json",
        "count": count, "begin_msgid": begin_msgid, "begin_itemidx": begin_itemidx,
    }, timeout=timeout)
    if not data:
        return [], "", False
    ret = (data.get("base_resp") or {}).get("ret", 0)
    if ret not in (0, None):
        # 10004 = 参数不对（biz/album_id 写错或该合集已删除）
        print(f"    · 合集接口 ret={ret}"
              + ("（__biz / album_id 可能不对）" if ret == 10004 else ""))
        return [], "", False
    resp = data.get("getalbum_resp") or {}
    info = resp.get("base_info")
    title = (info or {}).get("title") or "" if isinstance(info, dict) else ""
    rows = []
    for row in (resp.get("article_list") or []):
        t, link = row.get("title") or "", row.get("url") or ""
        if not t or not link:
            continue
        rows.append({
            "title": t,
            "link": link,
            "published": ts_to_iso(row.get("create_time") or row.get("update_time")),
            "digest": row.get("digest") or "",
            "_msgid": row.get("msgid"),
            "_itemidx": row.get("itemidx"),
        })
    return rows, title, bool(resp.get("continue_flag"))


def fetch_album(biz, album_id, timeout=12, pages=1):
    """拉一个合集（自动翻页）。返回 (合集标题, [{title, link, published, digest}])。

    翻页实测有效：`continue_flag=1` 时带上一页末条的 `msgid`/`itemidx` 再请求即可续取，
    每页 10~20 条（`count` 只是上限，微信按自己的节奏给）。
    """
    out, seen, title = [], set(), ""
    begin_msgid, begin_itemidx = 0, 0
    for _page in range(max(1, pages)):
        rows, t, more = _album_page(biz, album_id, begin_msgid, begin_itemidx,
                                    timeout=timeout)
        if t and not title:
            title = t
        fresh = 0
        for r in rows:
            if r["link"] in seen:
                continue
            seen.add(r["link"])
            out.append({k: r[k] for k in ("title", "link", "published", "digest")})
            fresh += 1
        if not rows or not more or fresh == 0:
            break
        begin_msgid = rows[-1].get("_msgid") or 0
        begin_itemidx = rows[-1].get("_itemidx") or 0
        gentle_sleep(0.8, "（合集翻页）")
    return title, out


def fetch_album_articles(biz, album_id, timeout=12, pages=1):
    """拉一个合集的文章列表。返回 [{title, link, published, digest}]；失败返回 []。"""
    return fetch_album(biz, album_id, timeout=timeout, pages=pages)[1]


# 微信会把合集的完整链接直接印在文章页里：
#     mp/appmsgalbum?__biz=<biz>&action=getalbum&album_id=<id>#wechat_redirect
# 所以只要拿到**任意一篇**该号的文章页，就能自动发现它属于哪个合集——
# 不需要人工去找合集链接，也不需要提前维护 album_id 白名单。
#
# 匹配限定在「同一个链接/字符串内」（字符类排除引号、空白、尖括号），
# 避免页面别处的 __biz 与别处的 album_id 被误配成一对。
_ALBUM_SEP = r"[^\"'\s<>\\]{0,400}?"
ALBUM_FWD_RE = re.compile(r"__biz=([A-Za-z0-9=_%-]+)" + _ALBUM_SEP + r"album_id=(\d{6,})")
ALBUM_REV_RE = re.compile(r"album_id=(\d{6,})" + _ALBUM_SEP + r"__biz=([A-Za-z0-9=_%-]+)")


def _norm_biz(biz):
    return (biz or "").replace("%3D%3D", "==").replace("%3d%3d", "==")


def extract_album_refs(html):
    """从文章页 HTML 里挖出「这篇文章属于哪个合集」。返回 [{biz, album_id}]。"""
    html = html or ""
    out, seen = [], set()
    pairs = [(m.group(1), m.group(2)) for m in ALBUM_FWD_RE.finditer(html)]
    pairs += [(m.group(2), m.group(1)) for m in ALBUM_REV_RE.finditer(html)]
    for biz, aid in pairs:
        biz = _norm_biz(biz)
        key = (biz, aid)
        if not biz or key in seen:
            continue
        seen.add(key)
        out.append({"biz": biz, "album_id": aid})
    return out


def discover_albums(campus, links, workers=4, limit=40):
    """抓文章页并自动发现它们所属的合集。返回 [{biz, album_id, account}]。

    定位：把「人工找合集链接」这一步彻底去掉。有任意一篇该号的文章链接即可。
    """
    if not links:
        return []
    links = links[:limit]
    print(f"· [{campus}] 从 {len(links)} 篇文章页自动发现合集…")
    found, seen = [], set()

    def one(url):
        html = fetch_html(url, timeout=15)
        if not html:
            return "", []
        _t, account, _p, _b = parse_wechat_html(html)
        return account, extract_album_refs(html)

    with cf.ThreadPoolExecutor(max_workers=max(1, min(workers, len(links)))) as pool:
        for account, refs in pool.map(one, links):
            for ref in refs:
                key = (ref["biz"], ref["album_id"])
                if key in seen:
                    continue
                seen.add(key)
                found.append({"biz": ref["biz"], "album_id": ref["album_id"],
                              "name": "", "account": account or ""})
    print(f"    → 发现 {len(found)} 个合集")
    return found


def album_sources(campus, args):
    """合集来源：优先用人工清单，没有就**自动发现**（零配置）。

    自动发现的输入就是公开种子清单 `_work/wechat/wechat-<校区>.txt`：
    抓其中任意一篇的文章页，页面里就印着该号合集的完整链接。
    """
    albums = load_albums(campus)
    if albums:
        print(f"· [{campus}] 合集清单来自 wxmp-albums*.json（{len(albums)} 个）")
        return albums
    links = load_seed_links(campus, getattr(args, "seed_file", None))
    if not links:
        return []
    return discover_albums(campus, links, workers=getattr(args, "workers", 4))


def crawl_albums(campus, args):
    """按合集抓取，产出与 wxmp 模式同构的 items / registry。

    定位是**降级补充**：没有 cookies、或 cookies 被限流时，这条路径仍然可用——
    因为它完全不调用 `appmsg`（历史文章列表），所以**不受账号级限流影响**。

    实测（2026-10-14）：合集接口 `ret=0` 无需登录；返回的链接带 `chksm`，
    可以正常抓到正文（对照实验：同一篇文章去掉 `chksm` 就会吃验证码墙）。
    """
    albums = album_sources(campus, args)
    if not albums:
        print(f"· [{campus}] 没有可用合集：既没有 wxmp-albums*.json，"
              f"也没能从种子文章页自动发现")
        return [], {}

    pages = max(1, getattr(args, "album_pages", 2))
    print(f"· [{campus}] 公开合集 {len(albums)} 个（零凭据、不受限流影响，每集最多 {pages} 页）")
    filter_re = re.compile(WECHAT_CONFIG[campus].get("filter") or r".+")
    known = {normalize_name(a["name"]) for a in load_accounts(campus)}
    now = now_iso_cst()
    kept, per_biz = [], {}

    for alb in albums:
        title, arts = fetch_album(alb["biz"], alb["album_id"], pages=pages)
        label = alb.get("name") or title or alb["album_id"]
        if not arts:
            print(f"    - {label}: 取回 0 篇")
            continue
        account = alb.get("account") or alb.get("name") or "微信公众号"
        # 收录三选一：① 号在完整名单内；② 号名含本校关键词；③ 标题含本校关键词。
        # 否则跳过，避免「上海交大的文章混进浙大校区」这类跨校污染。
        mine = [a for a in arts
                if (normalize_name(account) in known
                    or filter_re.search(account) or filter_re.search(a["title"]))]
        kept.extend({"account": account, **a} for a in mine)
        per_biz[account] = per_biz.get(account, 0) + len(mine)
        print(f"    - {label}: 取回 {len(arts)} 篇，收录 {len(mine)} 篇")

    if kept and not getattr(args, "no_content", False):
        links = [k["link"] for k in kept]
        bodies = fetch_bodies(links, workers=getattr(args, "workers", 6))
        for k, body in zip(kept, bodies):
            k["body"] = body or k.get("digest") or ""

    items = [make_item(k["link"], k["title"], k["published"], k["account"],
                       k.get("body") or k.get("digest") or "")
             for k in kept]
    registry = {
        "wx:" + acc: {"name": acc, "count": n, "new": n, "status": "ok", "last_checked": now}
        for acc, n in per_biz.items()
    }
    print(f"· [{campus}] 合集合计 {len(items)} 条，来自 {len(registry)} 个号")
    return items, registry


def fetch_body(link, timeout=12):
    """取文章正文（可选）。复用 wxmp 的静态方法，失败返回空串。"""
    try:
        from wxmp import WxMPAPI
        html = WxMPAPI.fetch_article_content(link, timeout=timeout)
    except Exception:  # noqa: BLE001
        try:
            import urllib.request
            req = urllib.request.Request(link, headers={"User-Agent": "Mozilla/5.0"})
            html = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return ""
    m = re.search(r'(?is)id=["\']js_content["\'][^>]*>(.{0,300000})', html or "")
    if not m:
        return ""
    chunk = m.group(1)
    end = re.search(r'(?is)<script|rich_media_tool', chunk)
    if end:
        chunk = chunk[:end.start()]
    return strip_tags(chunk)


# --------------------------------------------------------------------------
# 公开链接种子模式（无 cookies 时的降级路径）
#   微信公众号文章正文本身是公开可读的，只有「某个号的完整文章列表」需要登录。
#   所以在没有 wxmp cookies 时，用已有的真实文章链接当种子，逐篇抓正文，
#   走与 wxmp **完全相同**的 item 构造与合并/展示链路。
#   种子来源：_work/wechat/wechat-<校区>.txt（手工维护）+ data/<校区>.json 里已有的 wechat 条目
#            + --seed-file 指定的额外清单。
#   等你补齐 _work/wechat/wxmp-cookies.json 后，wxmp 模式会自动接管并按号成批拉取。
# --------------------------------------------------------------------------
def load_seed_links(campus, extra_file=None):
    links, seen = [], set()

    def push(url):
        url = (url or "").strip()
        if url.startswith("http") and "mp.weixin.qq.com" in url and url not in seen:
            seen.add(url)
            links.append(url)

    for name in (f"wechat-{campus}.txt", extra_file):
        path = find_wechat_file(name) if name else None
        if path:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        push(line)
    data_path = os.path.join(DATA_DIR, f"{campus}.json")
    if os.path.exists(data_path):
        try:
            with open(data_path, encoding="utf-8") as fh:
                old = json.load(fh) or {}
            for it in old.get("items") or []:
                if (it.get("dt") or {}).get("sg") == "wechat":
                    push(it.get("u") or it.get("i"))
        except Exception:  # noqa: BLE001
            pass
    return links


def wechat_var(html, name):
    """从公众号文章页里取 `var name = '...'`（兼容 htmlDecode("...") 包裹）。"""
    m = (re.search(r'var\s+%s\s*=\s*(?:htmlDecode\()?"([^"]*)"' % re.escape(name), html)
         or re.search(r"var\s+%s\s*=\s*(?:htmlDecode\()?'([^']*)'" % re.escape(name), html))
    if not m:
        return ""
    return (m.group(1).replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">").replace(".html(false)", "").strip())


def parse_wechat_html(html):
    """从文章页 HTML 里抽出 (title, account, published, body)，只解析一次。"""
    title = wechat_var(html, "msg_title")
    if not title:
        m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
        title = m.group(1) if m else ""
    account = ""
    for var in ("nickname", "profile_nickname", "user_name", "author"):
        account = wechat_var(html, var)
        if account and not account.startswith("gh_"):
            break
    ct = wechat_var(html, "ct")
    published = ts_to_iso(ct) if ct.isdigit() else ""
    body = ""
    m = re.search(r'(?is)id=["\']js_content["\'][^>]*>(.{0,300000})', html)
    if m:
        chunk = m.group(1)
        end = re.search(r'(?is)<script|rich_media_tool', chunk)
        if end:
            chunk = chunk[:end.start()]
        body = strip_tags(chunk)
    return title, account, published, body


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 Chrome/120.0.0.0")


def fetch_html(url, timeout=15, timeout_retry=True):
    """抓一个微信页面的原始 HTML。失败返回空串。

    ⚠ 实测（2026-10-14）微信对**文章页链接的两种形式区别对待**：

    | 链接形式 | 例 | 能否抓到 |
    |---|---|---|
    | 短链 | `/s/XLsEsQOyo4qwtekckfDT2A` | ✅ 3.7 MB，正常 |
    | 长链带 `chksm` | `/s?__biz=..&mid=..&idx=..&sn=..&chksm=..` | ✅ 3.6 MB，正常 |
    | 长链**缺** `chksm` | `/s?__biz=..&mid=..&sn=..&scene=21` | ❌ 17 KB 验证码墙（加载 TCaptcha.js） |

    **同一条文章**的短链能抓、去掉 chksm 的长链就吃验证墙（已做受控对照）。
    所以：文末「往期回顾」里的链接抓不动（它们没有 chksm），
    而**合集接口返回的链接带 chksm，可以抓**——这是「零凭据 + 不限流」链路的关键。
    """
    try:
        import ssl
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
        return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        # 退一步：部分环境下默认证书链不可用（与合集接口的 verify=False 保持一致）
        try:
            import ssl
            import urllib.request
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as fh:
                return fh.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return ""


def fetch_article(url, timeout=15):
    """抓一篇文章页并一次性解析，返回 (title, account, published, body)。"""
    html = fetch_html(url, timeout=timeout)
    if not html:
        return "", "", "", ""
    return parse_wechat_html(html)


def wechat_meta(url, timeout=15):
    """抓一篇文章页，返回 (title, account, published)。任何一步失败都退化为空串。"""
    title, account, published, _ = fetch_article(url, timeout=timeout)
    return title, account, published


def fetch_bodies(urls, workers=6, timeout=15):
    """并发抓多篇文章正文（只取正文）。返回与 urls 同序的正文列表。

    mp.weixin.qq.com 单页约 0.3~1s 网络 RTT，串行 + sleep 会很慢；
    这里用线程池跨文章并发（默认 6），单篇失败退化为空串。
    """
    results = [""] * len(urls)
    if not urls:
        return results
    workers = max(1, min(workers, len(urls)))

    def one(idx_url):
        idx, url = idx_url
        _t, _a, _p, body = fetch_article(url, timeout=timeout)
        return idx, body

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for idx, body in pool.map(one, list(enumerate(urls))):
            results[idx] = body
    return results


def crawl_public_seed(campus, args):
    """按公开链接种子逐篇抓正文，产出与 wxmp 模式同构的 items / registry。"""
    links = load_seed_links(campus, args.seed_file)
    if not links:
        print(f"· [{campus}] 没有公开种子链接（_work/wechat/wechat-{campus}.txt 为空）")
        return [], {}
    links = links[: args.public_max]
    now = now_iso_cst()
    workers = max(1, min(getattr(args, "workers", 6), len(links) or 1))
    print(f"· [{campus}] 公开链接种子 {len(links)} 篇（无 cookies，并发 {workers} 抓正文）")

    items, per_account = [], {}
    skipped = 0
    filter_re = re.compile(WECHAT_CONFIG[campus].get("filter") or r".+")
    # 名单内完整昵称集合：这些号的昵称即使不含学校关键词也一律收录
    known = {normalize_name(a["name"]) for a in load_accounts(campus)}

    def grab(url):
        return url, fetch_article(url, timeout=15)

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        fetched = list(pool.map(grab, links))

    for url, (title, account, published, body) in fetched:
        if not title:
            title = (body[:40] + "…") if body else url
        account = account or "微信公众号"
        # 收录三选一：① 昵称在完整名单内；② 昵称含本校关键词；③ 标题含本校关键词。
        # 否则跳过，避免「上海交通大学的文章混进浙大校区」这类跨校污染。
        if (normalize_name(account) not in known
                and not filter_re.search(account) and not filter_re.search(title)):
            skipped += 1
            continue
        items.append(make_item(url, title, published, account, body))
        per_account[account] = per_account.get(account, 0) + 1
        print(f"    - {account}: {title[:36]}")
    if skipped:
        print(f"    （跳过 {skipped} 篇：既不在名单内，账号与标题也不含本校关键词）")

    registry = {
        "wx:" + acc: {"name": acc, "count": n, "new": n, "status": "ok",
                      "last_checked": now}
        for acc, n in per_account.items()
    }
    print(f"· [{campus}] 公开种子合计 {len(items)} 条，来自 {len(registry)} 个号")
    return items, registry


def seed_links_without_items(campus, items, extra_file=None, limit=60):
    """取出「公开种子里**还没入库**的文章链接」，用来在 API 模式下补漏。

    为什么要这一步：`fetch_campus.py` 也会读 `_work/wechat/wechat-<校区>.txt` 逐篇抓正文。
    如果不把种子清单里的链接也单独补一次，就会出现：API 模式跑完、种子清单里明明有内容、
    页面上却没有（尤其是名单外的号、或被限流的号）。

    返回的链接里已经剔除了本轮 items 覆盖的，所以不会重复抓已经拿到的文章。
    """
    have = set()
    for it in items:
        have.add(it.get("i"))
        have.add(it.get("u"))
    pending = [u for u in load_seed_links(campus, extra_file) if u not in have]
    return pending[:limit]


# --------------------------------------------------------------------------
# 抓一个校区的所有公众号（wxmp 模式）
# --------------------------------------------------------------------------
def crawl_wechat(campus, api, args):
    cfg = WECHAT_CONFIG[campus]
    filter_re = re.compile(cfg.get("filter") or r".+")
    items, registry = [], {}
    now = now_iso_cst()
    accounts = load_accounts(campus)
    cred_path = getattr(args, "_cred_path", None)
    # 凭据指纹：限流记忆按它归类（同一份 cookies 被抓的次数会很不一样）
    cred_key = os.path.basename(cred_path) if cred_path else "default"
    fake_cache = load_fakeid_cache(cred_path)
    consecutive_zero = 0
    blocked_creds = 0        # 这一轮里「看起来是账号级限流」的号
    ok_accounts = 0
    print(f"· [{campus}] 公众号 {len(accounts)} 个（名单内完整昵称优先收录）")

    for entry in accounts:
        name = entry["name"]
        want = entry.get("max") or args.count
        nickname = name
        # ① 先用名单里写死的 fakeid；② 再用缓存；③ 最后才联网搜索（精确匹配优先）
        fakeid = entry.get("fakeid") or fake_cache.get(normalize_name(name), {}).get("fakeid")
        exact = bool(fakeid)
        if not fakeid:
            found = search_account(api, name, retries=args.retries)
            if not found:
                print(f"    - {name}：没搜到（跳过）")
                registry["wx:" + name] = {"name": name, "count": 0, "new": 0,
                                          "status": "failed", "last_checked": now}
                continue
            nickname, fakeid, exact = found["nickname"], found["fakeid"], found["exact"]
            # 名单内完整昵称精确命中 → 直接收录；否则才用 filter 兜底防误收同名社会号
            if not exact and not filter_re.search(nickname):
                print(f"    - {name}：搜到「{nickname}」非精确匹配且不符合本校关键词（跳过）")
                registry["wx:" + nickname] = {"name": nickname, "count": 0, "new": 0,
                                              "status": "failed", "last_checked": now}
                continue
            if not exact:
                print(f"    - {name}：未精确命中，退回关键词匹配到「{nickname}」")
            fake_cache[normalize_name(name)] = {"nickname": nickname, "fakeid": fakeid}
            save_fakeid_cache(fake_cache, cred_path)
        else:
            cached = fake_cache.get(normalize_name(name), {})
            nickname = cached.get("nickname") or name

        arts, blocked = list_articles(api, fakeid, want, args.pages,
                                     retries=args.retries, want_block=True)
        got = 0
        if args.no_content:
            for art in arts:
                items.append(make_item(art["link"], art["title"], art["published"],
                                       nickname, strip_tags(art.get("digest") or "")))
                got += 1
        else:
            bodies = fetch_bodies([a["link"] for a in arts], workers=args.workers)
            for art, body in zip(arts, bodies):
                if not body:
                    body = art.get("digest") or ""
                items.append(make_item(art["link"], art["title"], art["published"],
                                       nickname, body))
                got += 1
        if got:
            ok_accounts += 1
        if blocked and got == 0:
            # 单个 fakeid 被限：记下来，下次可跳过；同时观察是不是整份凭据都废了
            blocked_creds += 1
            note_block("targets", fakeid, f"{nickname} @ {campus}")
        tag = "精确" if exact else "关键词"
        print(f"    - {nickname}（{tag}匹配）: 取回 {got} 篇" + ("（限流）" if blocked and not got else ""))
        registry["wx:" + nickname] = {"name": nickname, "count": got, "new": got,
                                      "status": "ok" if got else "no_recent", "last_checked": now,
                                      "fakeid": fakeid}
        consecutive_zero = consecutive_zero + 1 if got == 0 else 0
        if consecutive_zero >= 3:
            # 判定粒度：如果这轮里还有号是成功的 → 只坏在这几个号身上（fakeid 级）；
            # 如果从头到尾一个号都没成功 → 更像整份凭据被盯上（账号级）。
            if ok_accounts == 0:
                note_block("credentials", cred_key, f"连续 {consecutive_zero} 个号取回 0 篇")
                print("\n✗ 连续 3 个号都取回 0 篇，且本轮没有任何号成功 —— 判定为【账号级限流】。")
                print(f"  被限的凭据：{cred_key}")
                pool = list_credential_pool(campus)
                if len(pool) > 1:
                    print(f"  ✓ 检测到 {len(pool)} 份凭据可用，换一份即可，不必等：")
                    print("    存成 _work/wechat/wxmp-cookie-header-pool2.txt，重跑本脚本。")
                else:
                    print("  ✗ 只找到 1 份凭据 —— 账号级限流**只能靠换账号解决，等待无效**。")
                    print("    准备第 2 个公众平台账号：登录后把 Cookie 整行存成")
                    print("    _work/wechat/wxmp-cookie-header-pool2.txt，脚本会自动轮换。")
            else:
                note_block("targets", fakeid, f"连续取回 0 篇：{nickname}")
                print(f"\n✗ 连续 3 个号取回 0 篇，但本轮有 {ok_accounts} 个号是成功的")
                print("  —— 判定为【fakeid 级限流】，只坏在这几个号上。")
                print("  这几个号已记入限流名单，6 小时内会自动跳过，不影响其它号。")
            print("  已锁定的 fakeid 会保留，本轮中止、不写入 data/（避免把空结果当成「无新内容」）。")
            return [], {}

    if blocked_creds and ok_accounts:
        print(f"· 本轮有 {blocked_creds} 个号限流、{ok_accounts} 个号正常 —— "
              f"限流是 fakeid 级，账号本身没问题。")
    print(f"· [{campus}] 公众号合计 {len(items)} 条")
    return items, registry


def append_seed_fallback(campus, items, registry, args):
    """API 模式下把种子清单里**尚未入库**的链接再抓一次（补名单外/被限流的号）。

    用 `--no-seed-fallback` 可关掉。任何异常都不影响主流程。
    """
    if getattr(args, "no_seed_fallback", False):
        return items, registry
    pending = seed_links_without_items(campus, items, args.seed_file,
                                      limit=getattr(args, "public_max", 60) or 60)
    if not pending:
        return items, registry
    print(f"· [{campus}] 种子补漏：{len(pending)} 篇尚未入库，逐篇抓正文")
    filter_re = re.compile(WECHAT_CONFIG[campus].get("filter") or r".+")
    known = {normalize_name(a["name"]) for a in load_accounts(campus)}
    workers = max(1, min(getattr(args, "workers", 6) or 6, len(pending)))
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        fetched = list(pool.map(lambda u: (u, fetch_article(u, timeout=15)), pending))
    now = now_iso_cst()
    added = 0
    for url, (title, account, published, body) in fetched:
        if not title:
            continue
        account = account or "微信公众号"
        if (normalize_name(account) not in known
                and not filter_re.search(account) and not filter_re.search(title)):
            continue
        items.append(make_item(url, title, published, account, body))
        rec = registry.get("wx:" + account) or {
            "name": account, "count": 0, "new": 0, "status": "ok", "last_checked": now}
        rec["count"] = int(rec.get("count") or 0) + 1
        rec["new"] = int(rec.get("new") or 0) + 1
        rec["status"] = "ok"
        registry["wx:" + account] = rec
        added += 1
    print(f"    ✓ 种子补漏入库 {added} 篇")
    return items, registry


# --------------------------------------------------------------------------
# 合并进 data/<校区>.json（复用 refresh.py 的导入与写入保护）
# --------------------------------------------------------------------------
def load_refresh():
    spec = importlib.util.spec_from_file_location("refresh", os.path.join(HERE, "refresh.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_payload(campus, items, registry, source_warnings):
    path = wechat_path(f"wechat-items-{campus}.json")
    payload = {
        "campus": campus,
        "campus_name": WECHAT_CONFIG[campus]["campus_name"],
        "wechat_accounts": [e["name"] for e in WECHAT_CONFIG[campus]["accounts"]],
        "source_registry": {
            key: {"key": key, "name": rec["name"], "source_group": "wechat",
                  "count": rec.get("count", 0), "new": rec.get("new", 0),
                  "status": rec.get("status", "ok"), "last_checked": rec.get("last_checked", ""),
                  "first_seen": rec.get("last_checked", ""), "url": "https://mp.weixin.qq.com/",
                  "source_id": "", "kind": "wechat-wxmp"}
            for key, rec in registry.items()
        },
        "fetch_warnings": source_warnings,
        "items": items,
    }
    os.makedirs(WECHAT_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    return path


def merge_into_data(campus, items, registry, refresh):
    """按 i 合并进 data/<校区>.json：只追加不改历史，保持 90 天保留期与写入保护。"""
    data_path = os.path.join(DATA_DIR, f"{campus}.json")
    old = {}
    if os.path.exists(data_path):
        with open(data_path, encoding="utf-8") as fh:
            old = json.load(fh) or {}
    merged = {it["i"]: it for it in (old.get("items") or []) if it.get("i")}
    before = len(merged)
    for it in items:
        merged[it["i"]] = it
    final = sorted(merged.values(), key=lambda x: (x.get("d") or "", x.get("i") or ""), reverse=True)
    final, pruned = refresh.prune_old_items(final)

    # 来源注册表：以旧表为底，用本轮的公众号记录覆盖，并保留其它来源
    reg = dict(old.get("source_registry") or {})
    for key, rec in registry.items():
        entry = {"key": key, "name": rec["name"], "source_group": "wechat",
                 "count": rec.get("count", 0), "new": rec.get("new", 0),
                 "status": rec.get("status", "ok"), "last_checked": rec.get("last_checked", ""),
                 "first_seen": reg.get(key, {}).get("first_seen") or rec.get("last_checked", ""),
                 "url": "https://mp.weixin.qq.com/", "source_id": "", "kind": "wechat-wxmp"}
        reg[key] = entry

    accounts = sorted({a["name"] for a in WECHAT_CONFIG[campus]["accounts"]}
                      | set(old.get("wechat_accounts") or []))
    now = now_iso_cst()
    added = len(final) - before
    # last_import 与 refresh.py / fetch_campus.py 保持**同一种结构**(dict),
    # 否则页面与 cloud_update.py 的摘要读不出「新增/更新」两列(早期版本写的是字符串)。
    # 这里拿不到精确的"更新(同 id 覆盖)"计数,因为公众号的 id 就是文章 URL、
    # 内容不会变;统一按 0 记,与 fetch_campus.py 的口径一致。
    old.update({
        "campus": campus,
        "campus_name": old.get("campus_name") or WECHAT_CONFIG[campus]["campus_name"],
        "scraped_at": now,
        "import_source": "fetch_wechat.py",
        "wechat_accounts": accounts,
        "source_registry": reg,
        "items": final,
        "last_import": {
            "at": now,
            "added": max(0, added),
            "updated": 0,
            "items": len(final),
            "source": "fetch_wechat.py",
            "crawl_new": sum(int(v.get("new") or 0) for v in registry.values()
                             if isinstance(v, dict)),
        },
    })
    old["fetched"] = len(final)
    old.update(refresh.recompute_stats(final, old.get("sources") or [], reg))

    if pruned:
        print(f"  · 清除发布超过 {RETENTION_DAYS} 天的历史条目 {pruned} 条")
    refresh.backup_file(data_path)
    refresh.write_text(data_path, json.dumps(old, ensure_ascii=False, separators=(",", ":")))
    return len(final), len(final) - before


def run_search_only(campuses, args):
    """只用完整昵称搜索并缓存 fakeid（不拉文章列表、不写 data/）。

    用途：新登录账号常被「文章列表接口」限流（ret=200013），但**搜索接口不受限**。
    先跑这一步把 42 个号的 fakeid 全部锁定，等限流解禁后直接抓取即可，无需再搜。
    """
    cookies, src = (None, None)
    if args.cookies:
        try:
            cookies = json.load(open(args.cookies, encoding="utf-8"))
            src = args.cookies
        except Exception as exc:  # noqa: BLE001
            print(f"! 读不到 --cookies {args.cookies}：{exc}")
    else:
        cookies, src = load_cookies(campuses[0] if campuses else "zju")
    if not cookies:
        print("! 没有 cookies，无法搜索。先把 Cookie 存到 _work/wechat/wxmp-cookie-header.txt")
        return
    manual = cookies.get("token") or ""
    tpath = find_wechat_file("wxmp-token.txt")
    if not manual and tpath and os.path.exists(tpath):
        manual = open(tpath, encoding="utf-8").read().strip()
    api = MpClient(cookies)
    api.ensure_token(forced=manual if manual.isdigit() else None)
    if not api.token:
        print("! 取 token 失败，请检查 cookies")
        return
    print(f"· cookies：{os.path.basename(src)}，token={api.token}")

    cache = load_fakeid_cache()
    total = hit = exact_n = 0
    for campus in campuses:
        if campus not in WECHAT_CONFIG:
            continue
        print(f"\n=== {campus} ===")
        for a in load_accounts(campus):
            total += 1
            r = search_account(api, a["name"], retries=args.retries)
            if r:
                cache[normalize_name(a["name"])] = {"nickname": r["nickname"],
                                                    "fakeid": r["fakeid"]}
                hit += 1
                exact_n += 1 if r["exact"] else 0
                mark = "✓精确" if r["exact"] else "△关键词"
                print(f"  {mark} {a['name']} → {r['nickname']}")
            else:
                print(f"  ✗未命中 {a['name']}")
            gentle_sleep(1.2, "（搜索间隔）")
    save_fakeid_cache(cache)
    print(f"\n· 搜索完成：{hit}/{total} 命中（精确 {exact_n}），已缓存到 _work/{FAKEID_CACHE}")
    print("  限流解禁后直接跑 `python fetch_wechat.py` 即可按号抓取（会走缓存，不再搜索）")


def main():
    ap = argparse.ArgumentParser(description="用 wxmp 抓取官方微信公众号并合并进 data/<校区>.json")
    ap.add_argument("--campus", default="zju,sjtu,xjtu", help="逗号分隔，默认三校全抓")
    ap.add_argument("--count", type=int, default=15, help="每个号最多取几篇（默认 15）")
    ap.add_argument("--pages", type=int, default=3, help="每个号最多翻几页（每页 5 篇）")
    ap.add_argument("--retries", type=int, default=2, help="单次请求失败重试次数")
    ap.add_argument("--workers", type=int, default=6, help="并发抓正文的线程数（默认 6）")
    ap.add_argument("--cookies", help="指定 cookies JSON 文件（默认自动找 _work/wechat/wxmp-cookies*.json）")
    ap.add_argument("--cred-index", type=int, default=None,
                    help="凭据池里用第几份（从 0 开始）。默认按日期自动轮换，"
                         "同一份被限流时自动跳到下一份")
    ap.add_argument("--no-content", action="store_true", help="不抓正文，只用列表里的摘要")
    ap.add_argument("--public-seed", action="store_true",
                    help="无 cookies 时用公开文章链接种子抓取（默认在无凭据时自动启用）")
    ap.add_argument("--seed-file", help="额外的公开种子链接清单（一行一个）")
    ap.add_argument("--public-max", type=int, default=60, help="公开种子模式最多抓几篇（默认 60）")
    ap.add_argument("--no-seed-fallback", action="store_true",
                    help="API 模式下不补抓种子清单里尚未入库的链接（默认会补，避免名单外的号漏掉）")
    ap.add_argument("--albums", action="store_true",
                    help="额外抓一遍「公开合集」源（零凭据、不受限流影响，是 appmsg 被限流时的兜底）")
    ap.add_argument("--albums-only", action="store_true",
                    help="只抓公开合集源（完全不用 cookies，适合限流期维持更新）")
    ap.add_argument("--album-pages", type=int, default=2,
                    help="每个合集最多翻几页（每页 10~20 篇，默认 2；合集接口会自动给 continue_flag）")
    ap.add_argument("--dry-run", action="store_true", help="只搜索公众号验证 cookies，不写数据")
    ap.add_argument("--list-accounts", action="store_true",
                    help="只打印各校区合并后的公众号名单（含来源），不联网")
    ap.add_argument("--search-only", action="store_true",
                    help="只用完整昵称搜索并缓存 fakeid（不拉文章列表、不写 data/）")
    ap.add_argument("--no-import", action="store_true", help="只抓取，结果落在 _work/")
    ap.add_argument("--import-only", action="store_true", help="不联网，用 _work/ 上次结果导入并重建页面")
    ap.add_argument("--no-build", action="store_true", help="不重建 index.html")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

    campuses = [c.strip() for c in args.campus.split(",") if c.strip()]

    # 旧布局兼容：把散在 _work/ 根下的公众号文件搬进 _work/wechat/（幂等，只搬一次）
    migrate_legacy_files()

    if args.list_accounts:
        cache = load_fakeid_cache()
        print(f"· 工作目录：{os.path.relpath(WECHAT_DIR, HERE)}")
        for campus in campuses:
            if campus not in WECHAT_CONFIG:
                continue
            print(f"\n=== {campus}（{WECHAT_CONFIG[campus]['campus_name']}）===")
            for a in load_accounts(campus):
                key = normalize_name(a["name"])
                hit = cache.get(key, {})
                fid = a.get("fakeid") or hit.get("fakeid") or ""
                mark = "✓ 已缓存 fakeid" if fid else "· 待搜索"
                print(f"  - {a['name']}  max={a.get('max') or '-'}  {mark}"
                      + (f"  → {hit.get('nickname')}" if hit and hit.get("nickname")
                         and normalize_name(hit["nickname"]) != key else ""))
        return

    if args.search_only:
        run_search_only(campuses, args)
        return

    refresh = load_refresh()
    written = []

    for campus in campuses:
        if campus not in WECHAT_CONFIG:
            print(f"✗ 未知校区 {campus}，可选：{', '.join(WECHAT_CONFIG)}")
            continue
        print(f"\n=== {campus} ===")

        if args.albums_only:
            # 纯合集模式：不用任何凭据，因此**完全不受限流影响**。
            # 这是限流期维持页面更新的兜底路径。
            items, registry = crawl_albums(campus, args)
            if not items:
                print("· 合集源没取到内容（检查 _work/wechat/wxmp-albums*.json 是否配好）")
                continue
            write_payload(campus, items, registry, [])
            if args.no_import:
                print(f"· 结果已写入 _work/wechat/wechat-items-{campus}.json（--no-import）")
                continue
            total, added = merge_into_data(campus, items, registry, refresh)
            print(f"· [{campus}] 合并完成：新增 {added} 条，现有 {total} 条")
            written.append(campus)
            continue

        if args.import_only:
            path = find_wechat_file(f"wechat-items-{campus}.json")
            if not path:
                print(f"✗ 找不到 _work/wechat/wechat-items-{campus}.json，请先联网跑一次")
                continue
            payload = json.load(open(path, encoding="utf-8"))
            items, registry = payload["items"], {
                k: {"name": v["name"], "count": v.get("count", 0), "new": v.get("new", 0),
                    "status": v.get("status", "ok"), "last_checked": v.get("last_checked", "")}
                for k, v in (payload.get("source_registry") or {}).items()}
        else:
            WxMPAPI = load_wxmp()
            cookies, src = (None, None)
            if args.cookies:
                try:
                    cookies = json.load(open(args.cookies, encoding="utf-8"))
                    src = args.cookies
                except Exception as exc:  # noqa: BLE001
                    print(f"! 读不到 --cookies {args.cookies}：{exc}")
            else:
                # 轮换池：有多份凭据时按日期挑一份，避免单账号被抓到限流后整条链路停摆
                pool = list_credential_pool(campus)
                if len(pool) > 1:
                    idx = (args.cred_index if getattr(args, "cred_index", None) is not None
                           else pool_index_for_today(campus))
                    idx = max(0, min(idx, len(pool) - 1))
                    cookies, src = pool[idx]
                    print(f"· 凭据池共 {len(pool)} 份，本次用第 {idx + 1} 份：{src}")
                    if is_recently_blocked("credentials", os.path.basename(src)):
                        nxt = (idx + 1) % len(pool)
                        print(f"  ⚠ 这一份最近被限过，自动换第 {nxt + 1} 份：{pool[nxt][1]}")
                        cookies, src = pool[nxt]
                else:
                    cookies, src = load_cookies(campus)
            # 让 crawl_wechat 知道本次用的是哪份凭据（限流记忆 / fakeid 缓存都按它分桶）
            args._cred_path = src

            if not WxMPAPI or not cookies:
                if args.dry_run:
                    if not WxMPAPI:
                        print("! 没有 wxmp，跳过联网抓取")
                    else:
                        print("! 没有找到 cookies（_work/wechat/wxmp-cookies*.json）")
                    continue
                # 无凭据：降级到「公开链接种子」模式，链路与 wxmp 完全一致
                print("! 没有 cookies —— 降级用公开文章链接种子抓取（正文公开可读）")
                print("  API 模式：把登录后的 cookies 存成 _work/wechat/wxmp-cookies.json（至少 wxuin / pass_ticket）")
                items, registry = crawl_public_seed(campus, args)
                if not items:
                    path = find_wechat_file(f"wechat-items-{campus}.json")
                    if path:
                        print(f"· 退回上次缓存 {os.path.basename(path)}")
                        payload = json.load(open(path, encoding="utf-8"))
                        items, registry = payload["items"], {
                            k: {"name": v["name"], "count": v.get("count", 0), "new": v.get("new", 0),
                                "status": v.get("status", "ok"), "last_checked": v.get("last_checked", "")}
                            for k, v in (payload.get("source_registry") or {}).items()}
                    else:
                        print("· 无种子也无缓存，本轮不导入")
                        continue
                else:
                    write_payload(campus, items, registry, [])
                    if args.no_import:
                        print(f"· 结果已写入 _work/wechat/wechat-items-{campus}.json（--no-import）")
                        continue
            else:
                print(f"· 使用 cookies：{os.path.basename(src)}")
                manual = cookies.get("token") or ""
                tpath = find_wechat_file("wxmp-token.txt")
                if not manual and tpath and os.path.exists(tpath):
                    manual = open(tpath, encoding="utf-8").read().strip()
                api = MpClient(cookies)
                token = api.ensure_token(forced=manual if manual.isdigit() else None)
                if not token:
                    print("! 获取 token 失败（cookies 可能已过期）")
                    print("  请重新登录公众平台并更新 cookies；或把页面 URL 里的 token 数字")
                    print("  存到 _work/wechat/wxmp-token.txt 后重跑")
                    if args.dry_run:
                        continue
                    api = None
                if api is None or args.dry_run:
                    if args.dry_run:
                        print(f"· dry-run：cookies 有效，token = {token}")
                    continue
                items, registry = crawl_wechat(campus, api, args)
                items, registry = append_seed_fallback(campus, items, registry, args)
                # 合集源作为**并行补充**：它不受限流影响，即使 appmsg 被限也能带回来一些
                if args.albums:
                    album_items, album_reg = crawl_albums(campus, args)
                    if album_items:
                        have = {it.get("i") for it in items}
                        fresh = [it for it in album_items if it.get("i") not in have]
                        if fresh:
                            print(f"· [{campus}] 合集源补充 {len(fresh)} 条")
                        items = items + fresh
                        for k, v in album_reg.items():
                            registry.setdefault(k, v)
                if not items:
                    # appmsg 被限流（crawl_wechat 会返回空）时，别就此收工：
                    # 合集源零凭据、不受限流影响，是这时候唯一的出路。
                    print("· 本轮 appmsg 没抓到文章")
                    albums_cfg = load_albums(campus)
                    if albums_cfg:
                        print(f"  ↓ 自动改走公开合集源兜底（{len(albums_cfg)} 个合集，不需要 cookies）")
                        items, registry = crawl_albums(campus, args)
                    if not items:
                        print("  合集源也没取到内容，保留现有数据")
                        print("  → 若确认是账号级限流，准备第 2 个公众平台账号存成")
                        print("    _work/wechat/wxmp-cookie-header-pool2.txt，脚本会自动轮换")
                        continue
                write_payload(campus, items, registry, [])
                if args.no_import:
                    print(f"· 结果已写入 _work/wechat/wechat-items-{campus}.json（--no-import）")
                    continue

        total, added = merge_into_data(campus, items, registry, refresh)
        print(f"· [{campus}] 合并完成：新增 {added} 条，现有 {total} 条")
        written.append(campus)

    if not written and not args.no_build:
        # 仍然重建一次，保证 --import-only 之外的场景页面也是新的
        pass
    if not args.no_build:
        print("\n· 重建 index.html …")
        refresh.build()


if __name__ == "__main__":
    main()
