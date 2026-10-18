#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「XX知道」多校区信息源抓取器 —— 各高校官网 + 职能部门（不抓公众号）。

与 refresh.py 的分工:
    refresh.py       走 pkuknow.cn 聚合接口(清华),并负责把 data/*.json 生成 index.html
    fetch_campus.py  直接从各高校(浙大/上交/西交)的官网与职能部门抓内容,
                     产出 data/<校区>.json。支持两种来源:
                       kind=list  HTML 列表页(绝大多数站点)
                       kind=json  自带搜索接口的整站前端渲染站点(如港大)

用法示例
--------
    python fetch_campus.py                      # 抓全部校区,写入 data/ 并重新生成页面
    python fetch_campus.py --campus zju         # 只抓浙大
    python fetch_campus.py --max-per-source 10  # 每个来源最多取 10 条(默认按各来源的 max)
    python fetch_campus.py --no-details         # 不抓正文,只取标题/时间/链接(快很多)
    python fetch_campus.py --no-import          # 只抓取并输出到 _work/,不写 data/
    python fetch_campus.py --import-only        # 不联网,把 _work/ 里上次的结果写入 data/ 并生成页面

设计要点
--------
* 只用标准库,与 refresh.py 保持一致(无需 pip install)。
* 只抓公开的网站内容:每所学校的新闻网/通知公告列表 + 各职能部门的列表页。
* **只增不减、按 id 合并**:每轮重爬列表页并合并进 data/，新发布的内容自动入库；
  历史条目保留，超过保留期的由 refresh.py 生成页面时统一清除。
* **加一所学校不用改代码逻辑**，只要在下面的 SOURCES 里加一组来源、并在
  refresh.py / template.html 里登记校区名即可（完整步骤见 docs/添加新大学.md）；
  要临时补/停一个来源，写 `_work/sources-<校区>.json` 即可（同 id 覆盖、enabled=false 停用）。
* 每个主机 1.2 秒一次请求,失败重试 2 次,失败不写坏数据(refresh.py 那一套写入保护照旧生效)。
"""

import argparse
import concurrent.futures as cf
import datetime
import gzip
from html import unescape as html_unescape
import importlib.util
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
WORK_DIR = os.path.join(HERE, "_work")

# 统一用北京时间：不能用 time.strftime / datetime.now() —— 它们取运行机器的本地时区，
# GitHub Actions 运行器是 UTC，会把 UTC 时间贴上 +08:00 标签（页面显示差 8 小时）。
CST = datetime.timezone(datetime.timedelta(hours=8))    # 中国无夏令时，固定 +08:00


def now_iso_cst():
    """当前北京时间，形如 2026-10-11T12:34:56+08:00。"""
    return datetime.datetime.now(CST).isoformat(timespec="seconds")

READ_LIMIT = 6 * 1024 * 1024     # 单页读取上限(超过则截断,gzip 用 decompressobj 容错)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

WX_HOST = "mp.weixin.qq.com"
WX_RE = re.compile(r"https?://mp\.weixin\.qq\.com/s[/?][\w\-?=&%.:]+")

# --------------------------------------------------------------------------
# 信息源清单：一所学校一组，只收「官网 + 各校职能部门」。
#   - kind=list 抓列表页。kind=wechat 已全部移除：不再摘取任何公众号文章，
#     只抓网页内容（需求：不抓公众号内容，只抓网站内容）。
#   - 原先逐院系铺开的学院站点已移出：信息噪音大，且与「招生 / 教务 / 就业 /
#     图书馆 / 学工 / 国际交流」这类更常见的信息需求重合度低。
#   - link_re：现代框架站点(通知详情是 /post/detail/xxx、?id=123 这种，没有
#     .htm + 数字路径)用自定义正则挑文章链接；配置了 link_re 就只认它。
#   - kind=json：整站前端渲染、HTML 里没有任何文章链接的站点，可直接读它自己的
#     搜索接口返回的 JSON（json_source_items() 即为此保留；当前 3 校不涉及）。
#   - 想临时补回某个院系，不用改代码：写进 _work/sources-<校区>.json 即可
#     （格式见 _work/sources.example.json，同 id 覆盖、enabled=false 停用）。
#   - 各站逐个实测：能稳定解析出「标题+链接」的才收；纯 JS 单页且无公开接口的
#     站点(zju 就业服务平台、sjtu/xjtu 图书馆、xjtu 就业创业网)，未收。
# --------------------------------------------------------------------------
SOURCES = {
    "zju": [
        # ---- 官网新闻 ----
        {"id": "zju_news", "name": "浙江大学求是新闻网", "group": "official",
         "url": "http://www.news.zju.edu.cn/", "kind": "list", "max": 18},
        {"id": "zju_news_yw", "name": "浙大求是新闻网·要闻", "group": "official",
         "url": "http://www.news.zju.edu.cn/24344/list.htm", "kind": "list", "max": 12},
        {"id": "zju_news_kxtt", "name": "浙大求是新闻网·科学头条", "group": "official",
         "url": "http://www.news.zju.edu.cn/kxtt/list.htm", "kind": "list", "max": 12},
        # ---- 职能部门 ----
        {"id": "zju_grs", "name": "浙江大学研究生院", "group": "official",
         "url": "http://www.grs.zju.edu.cn/", "kind": "list", "max": 12},
        {"id": "zju_zdzsc", "name": "浙江大学本科招生", "group": "official",
         "url": "https://zdzsc.zju.edu.cn/", "kind": "list", "max": 10},
        {"id": "zju_lib", "name": "浙江大学图书馆", "group": "official",
         "url": "https://libweb.zju.edu.cn/", "kind": "list", "max": 10},
    ],
    "sjtu": [
        # ---- 官网新闻 / 通知 ----
        {"id": "sjtu_tg", "name": "上海交通大学通知公告", "group": "official",
         "url": "https://www.sjtu.edu.cn/tg/", "kind": "list", "max": 15},
        {"id": "sjtu_jdyw", "name": "上海交大新闻网·交大要闻", "group": "official",
         "url": "https://news.sjtu.edu.cn/jdyw/", "kind": "list", "max": 12},
        {"id": "sjtu_zhxw", "name": "上海交大新闻网·综合新闻", "group": "official",
         "url": "https://news.sjtu.edu.cn/zhxw/", "kind": "list", "max": 12},
        # ---- 职能部门 ----
        {"id": "sjtu_jwc", "name": "上海交大教务处", "group": "official",
         "url": "https://jwc.sjtu.edu.cn/", "kind": "list", "max": 12},
        {"id": "sjtu_gs", "name": "上海交大研究生院", "group": "official",
         "url": "https://gs.sjtu.edu.cn/announcement/work", "kind": "list", "max": 10,
         "link_re": r"/post/detail/[A-Za-z0-9=_-]+"},
        {"id": "sjtu_zsb", "name": "上海交大本科招生", "group": "official",
         "url": "https://admissions.sjtu.edu.cn/", "kind": "list", "max": 10,
         "link_re": r"/newDetails\?contentsID=\d+"},
        {"id": "sjtu_affairs", "name": "上海交大学生事务中心", "group": "official",
         "url": "https://affairs.sjtu.edu.cn/", "kind": "list", "max": 10,
         "link_re": r"mid=26&v=informationview&id=\d+"},
        {"id": "sjtu_global", "name": "上海交大国际合作与交流处", "group": "official",
         "url": "https://global.sjtu.edu.cn/announcement", "kind": "list", "max": 10,
         "link_re": r"/announcement/view/\d+"},
    ],
    "xjtu": [
        # ---- 官网新闻 ----
        {"id": "xjtu_news", "name": "西安交大新闻网·主页新闻", "group": "official",
         "url": "https://news.xjtu.edu.cn/zyxw.htm", "kind": "list", "max": 15},
        # ---- 职能部门 ----
        {"id": "xjtu_jwc", "name": "西安交大教务处", "group": "official",
         "url": "https://jwc.xjtu.edu.cn/", "kind": "list", "max": 12},
        {"id": "xjtu_gs", "name": "西安交大研究生院", "group": "official",
         "url": "https://gs.xjtu.edu.cn/", "kind": "list", "max": 12},
        {"id": "xjtu_yz", "name": "西安交大研究生招生", "group": "official",
         "url": "https://yz.xjtu.edu.cn/", "kind": "list", "max": 10},
        {"id": "xjtu_zs", "name": "西安交大本科招生", "group": "official",
         "url": "https://zs.xjtu.edu.cn/", "kind": "list", "max": 10},
        {"id": "xjtu_xsc", "name": "西安交大学生工作部（处）", "group": "official",
         "url": "https://xsc.xjtu.edu.cn/", "kind": "list", "max": 10},
    ],
}

# 公众号内容已整体停用（需求：只抓网站内容）。
# 下面两个常量保留为空表，是为了「以后要恢复某校公众号」时只填配置、不改代码；
# 抓取器也只在 kind=wechat 的来源上才会去摘 mp.weixin.qq.com 链接（现无此类来源）。
# 默认抓取哪些校区：内置三校（浙大/上交/西交）。清华知道已改为外链，本站不再抓取。
DEFAULT_CAMPUSES = "zju,sjtu,xjtu"

WECHAT_ACCOUNT_FILTER = {}
WECHAT_ACCOUNTS = {"zju": [], "sjtu": [], "xjtu": []}

# --------------------------------------------------------------------------
# 批量扩展的高校信息源（sources_more.py，由 tools/discover_sources.py 自动发现）
# 与内置 SOURCES 同构：同 id 覆盖、enabled=false 停用。放在单独文件里是为了
# 让这份「几十所学校」的长配置不把主文件撑爆，也方便批量重新生成。
# --------------------------------------------------------------------------
def _load_sources_more():
    path = os.path.join(HERE, "sources_more.py")
    if not os.path.exists(path):
        return {}
    spec = importlib.util.spec_from_file_location("sources_more", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! sources_more.py 读不出来，已忽略：{exc}")
        return {}
    extra = getattr(mod, "SOURCES_EXTRA", None)
    return extra if isinstance(extra, dict) else {}


for _campus, _rows in _load_sources_more().items():
    if not isinstance(_rows, list):
        continue
    _base = SOURCES.setdefault(_campus, [])
    _have = {str(s.get("id")) for s in _base if isinstance(s, dict)}
    for _row in _rows:
        if isinstance(_row, dict) and str(_row.get("id")) not in _have:
            _base.append(_row)
# --------------------------------------------------------------------------
# 分类规则:命中即止,顺序有意义(越具体的越靠前)
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

DATE_FULL = re.compile(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")
DATE_YM = re.compile(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]?(?!\d)")
DATE_DMY = re.compile(r"(?<!\d)(\d{1,2})\s*[.]\s*(\d{1,2})\s+(20\d{2})(?!\d)")   # 西交机械用「日.月 年」
DATE_MD = re.compile(r"(?<!\d)(\d{1,2})\s*[-/.]\s*(\d{1,2})(?!\d)")
YEAR_ANY = re.compile(r"(20\d{2})")
GAP = r"[^0-9\n]{0,12}"      # 日期和时间之间允许夹「（周一）」这类修饰,但不许跨行
TIME_RANGE = re.compile(
    r"(20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*日?" + GAP +
    r"(\d{1,2})\s*[:：]\s*(\d{2})\s*[-–—~～至到]\s*(\d{1,2})\s*[:：]\s*(\d{2})")
TIME_RANGE_MD = re.compile(
    r"(\d{1,2})\s*月\s*(\d{1,2})\s*日" + GAP +
    r"(\d{1,2})\s*[:：]\s*(\d{2})\s*[-–—~～至到]\s*(\d{1,2})\s*[:：]\s*(\d{2})")
# 只有开始时间(没有结束时间)也收:正文里必须带「时间：」这类标签才认,避免误抓
NOT_A_TIME_LABEL = r"(?<!发布)(?<!更新)(?<!截止)(?<!报名)(?<!答辩)(?<!考试)(?<!开课)(?<!公示)(?<!提交)"
START_FULL = re.compile(
    NOT_A_TIME_LABEL + r"(?:活动时间|讲座时间|报告时间|会议时间|举办时间|开始时间|时间)\s*[:：]\s*"
    r"(20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*日?" + GAP +
    r"(\d{1,2})\s*[:：]\s*(\d{2})")
START_MD = re.compile(
    NOT_A_TIME_LABEL + r"(?:活动时间|讲座时间|报告时间|会议时间|举办时间|开始时间|时间)\s*[:：]\s*"
    r"(\d{1,2})\s*月\s*(\d{1,2})\s*日" + GAP + r"(\d{1,2})\s*[:：]\s*(\d{2})")
# 从已经抽到的「活动时间」文本反解开始/结束时间
EV_TM_FULL = re.compile(
    r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?" + GAP +
    r"(\d{1,2})\s*[:：]\s*(\d{2})\s*[-–—~～至到]\s*(\d{1,2})\s*[:：]\s*(\d{2})")
EV_TM_ONE = re.compile(
    r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?" + GAP + r"(\d{1,2})\s*[:：]\s*(\d{2})")
EV_TM_MD = re.compile(
    r"(\d{1,2})\s*月\s*(\d{1,2})\s*日" + GAP + r"(\d{1,2})\s*[:：]\s*(\d{2})")
EV_TM_DATE = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?")

_LAST_HIT = {}
_HOST_LOCK = threading.Lock()
RETRY_COUNT = 2      # 单页失败重试次数,可由 --retries 覆盖(见 fetch)
DOMAIN_DELAY = 0.0   # 同一注册域名(如 zju.edu.cn)聚合请求的最小间隔,>0 才生效(--domain-delay)
HTTP_TIMEOUT = 25    # 单次请求超时(秒),可由 --timeout 覆盖
# 超时是「站点被墙/挂起」时最大的时间黑洞:一个卡住的来源最坏要
# timeout × (retries+1) 秒(默认 25×3=75s)。几十所学校规模下,把 --timeout
# 压到 10~12s + --retries 1,最坏值就降到 20~24s,一轮能省几十分钟。


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def host_of(url):
    return urllib.parse.urlparse(url).netloc.lower()


def rate_domain(host):
    """把主机名归并到注册域名,用于**跨子域**聚合限速。

    背景(2026-10-10 实测):zju.edu.cn 站群的 WAF 按来源 IP 对**整个域名**累计请求数,
    超过阈值就在一个时间窗口内对所有子域返回 403。学院站都是独立子域
    (cs.zju.edu.cn / math.zju.edu.cn …),只按主机限速时子域之间零间隔,
    聚合速率照样触发封锁。edu.cn/ac.cn 这类三级结构取末三段,其余按主机名原样返回。
    """
    parts = host.lower().split(".")
    if len(parts) >= 3 and parts[-2] in ("edu", "ac", "gov") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return host


def polite_wait(url, delay):
    """同一主机之间至少间隔 delay 秒;DOMAIN_DELAY>0 时,同一注册域名下所有
    子域加起来也至少间隔 DOMAIN_DELAY 秒(见 rate_domain 的背景)。

    多线程下也能保证限速:先在锁内「预订」下一个可用时刻,再在锁外睡,
    这样不同主机的等待可以并行,同主机/同域名仍然严格排队。
    """
    host = host_of(url)
    keys = [host]
    if DOMAIN_DELAY > 0:
        dom = rate_domain(host)
        if dom != host and dom not in keys:
            keys.append(dom)
    with _HOST_LOCK:
        now = time.time()
        wait = 0.0
        for key in keys:
            gap = delay if key == host else DOMAIN_DELAY
            last = _LAST_HIT.get(key)
            if last is not None:
                wait = max(wait, gap - (now - last))
        wait = max(wait, 0.0)
        for key in keys:
            _LAST_HIT[key] = now + wait
    if wait > 0:
        time.sleep(wait)


REQUESTS = 0            # 本轮真正发出的 HTTP 请求数(含重试)，用于量化「更新一次」的代价


def fetch(url, delay=1.2, retries=None, timeout=None):
    """抓一个页面。http:// 的地址会先试同名 https —— 不少学校站点已上 https,
    而明文 http 可能被网关/代理拦成 403(2026-10 实测:多个学院站 http 403、https 正常)。

    retries=None 时用全局 RETRY_COUNT(可由 --retries 调整):失败源在被封的网络环境里
    每次重试都要等退避,云端可调小以压短单轮耗时。
    """
    global REQUESTS
    if retries is None:
        retries = RETRY_COUNT
    if timeout is None:
        timeout = HTTP_TIMEOUT
    targets = [url]
    if url.startswith("http://"):
        targets = [url.replace("http://", "https://", 1), url]
    last = None
    for target in targets:
        for attempt in range(retries + 1):
            polite_wait(target, delay)
            REQUESTS += 1
            try:
                req = urllib.request.Request(target, headers={
                    "User-Agent": UA,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Accept-Encoding": "gzip, deflate",
                })
                with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
                    raw = resp.read(READ_LIMIT)
                    if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                        raw = gunzip(raw)
                    return decode(resp.headers.get("Content-Type", ""), raw), resp.url
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"请求失败 {url}: {last}")


def gunzip(raw):
    """容忍被读取上限截断的 gzip 流(直接 gzip.decompress 会抛 EOFError)。"""
    try:
        return gzip.decompress(raw)
    except (OSError, EOFError, EOFError):
        try:
            return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw)
        except zlib.error:
            return raw


def decode(content_type, raw):
    """按 Content-Type → <meta charset> → utf-8 的顺序解码。

    老站常把 charset 写错(声明 utf-8 实际 GBK),所以解出替换字符时再换一种中文编码
    试一遍,哪个乱码少用哪个 —— 否则抓到的标题就是一串 �。
    """
    enc = "utf-8"
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if m:
        enc = m.group(1)
    else:
        m2 = re.search(r'charset=["\']?([\w-]+)', raw[:4096].decode("latin-1", "replace"), re.I)
        if m2:
            enc = m2.group(1)
    if enc.lower() in ("gb2312", "gbk", "gb18030"):
        enc = "gb18030"
    try:
        text = raw.decode(enc, "replace")
    except LookupError:
        enc, text = "utf-8", raw.decode("utf-8", "replace")
    if "\ufffd" not in text:
        return text
    for alt in ("gb18030", "utf-8", "big5"):
        if alt == enc:
            continue
        try:
            cand = raw.decode(alt, "replace")
        except LookupError:
            continue
        if cand.count("\ufffd") < text.count("\ufffd"):
            text, enc = cand, alt
    return text


# --------------------------------------------------------------------------
# HTML 小工具(不引第三方库)
# --------------------------------------------------------------------------
def strip_tags(fragment):
    fragment = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", fragment)
    fragment = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>|</tr>|</h\d>", "\n", fragment)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    fragment = (fragment.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
                .replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">"))
    fragment = re.sub(r"[ \t\u3000\u00a0]+", " ", fragment)
    return "\n".join(line.strip() for line in fragment.split("\n") if line.strip())


def page_title(html):
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    return strip_tags(m.group(1)).replace("\n", " ").strip() if m else ""


def anchors(html, base):
    """返回 [(绝对链接, 锚文本, 在原文中的位置)]"""
    out = []
    for m in re.finditer(r'(?is)<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html):
        # HTML 实体要先还原(&amp; -> &),否则带查询串的链接(如 &mid=26&id=488)会取错参
        href = urllib.parse.urljoin(base, html_unescape(m.group(1).strip()))
        text = strip_tags(m.group(2)).replace("\n", " ").strip()
        out.append((href, text, m.start()))
    return out


def is_article_link(href, base_host, host_allow=None):
    parts = urllib.parse.urlparse(href)
    if parts.scheme not in ("http", "https"):
        return False
    host = parts.netloc.lower()
    if host_allow:
        if host not in host_allow:
            return False
    elif host != base_host:
        return False
    if not re.search(r"\.(?:s?html?)$", parts.path, re.I):
        return False
    if not re.search(r"\d{3,}", parts.path):
        return False
    name = parts.path.rsplit("/", 1)[-1].lower()
    if name in ("index.htm", "index.html", "index.shtml", "list.htm", "list.html",
                "more.htm", "more.html", "search.htm", "search.html", "default.htm"):
        return False
    return True


# 正文容器:命中一个就用它的内容,不再全页乱翻。
# 顺序是有讲究的 —— 越靠前越"像正文",前面的命中了就不看后面的。
BODY_CONTAINERS = (
    r'<div[^>]+class=["\'][^"\']*\bwp_articlecontent\b[^"\']*["\'][^>]*>(.*?)</div>',
    r'<div[^>]+id=["\']?vsb_content["\']?[^>]*>(.*?)</div>',
    r'<div[^>]+class=["\'][^"\']*\bv_news_content\b[^"\']*["\'][^>]*>(.*?)</div>',
    r'<div[^>]+class=["\'][^"\']*\bTRS_Editor\b[^"\']*["\'][^>]*>(.*?)</div>',
    r'<div[^>]+class=["\'][^"\']*\barticle[-_]?content\b[^"\']*["\'][^>]*>(.*?)</div>',
    r'<div[^>]+id=["\']js_content["\'][^>]*>(.*?)</div>',
    r'<div[^>]+class=["\'][^"\']*\bcontent_main\b[^"\']*["\'][^>]*>(.*?)</div>',
    r'<div[^>]+class=["\'][^"\']*\bcontent[-_]?text\b[^"\']*["\'][^>]*>(.*?)</div>',
)

# 导航/侧栏/面包屑容器:整块扔掉,不然它们里面的短句会被当正文抓走
CHROME_BLOCK = re.compile(
    r"(?is)<(script|style|noscript|nav|header|footer)\b[^>]*>.*?</\1>")
CHROME_DIV = re.compile(
    r"(?is)<div[^>]+class=[\"'][^\"']*\b(?:nav|menu|header|footer|aside|sidebar|crumb|"
    r"breadcrumb|position|sitelist|link[-_]?list|friendly[-_]?link|share|qrcode)\b"
    r"[^\"']*[\"'][^>]*>.*?</div>")

# 栏目词:一块文本里这些词扎堆出现,基本可以断定是导航菜单而不是正文
NAV_WORDS = ("首页", "概况", "总览", "组织机构", "师资队伍", "教师名录", "人才培养", "科学研究",
             "招生", "就业", "学生工作", "学工动态", "团学风采", "志愿服务", "就业指导",
             "学生科技", "资料下载", "文件表格", "友情链接", "快速链接", "English", "联系我们",
             "院内链接", "友情提示", "办事指南", "规章制度", "继续教育", "当前位置", "您的位置")


def looks_like_nav(text):
    """判断一段文字是不是导航菜单:栏目词密集、且几乎没有成句的中文。"""
    text = re.sub(r"\s+", "", text or "")
    if not text:
        return False
    hits = sum(1 for w in NAV_WORDS if w in text)
    if hits < 4:
        return False
    # 真正文有标点、有长句;导航菜单基本没标点
    punctuation = len(re.findall(r"[，。；：！？、“”（）]", text))
    return punctuation < len(text) / 80


def _innermost_container(html, pattern):
    """在页面里找 pattern 对应的容器,返回它的内部 HTML(取最靠后/最内层一个)。

    这套 CMS 的文章模板里,正文容器常常是**最后一个**匹配项(前面可能被导航栏的
    同名 class 命中),所以从后往前找第一个能抽出像样文字的。
    """
    for m in reversed(list(re.finditer(pattern, html, re.I | re.S))):
        yield m.group(1)


def extract_body(html, cap=1200):
    """抽正文。

    顺序:先按常见 CMS 正文容器精取(最可靠)→ 不行再从整页 <p> 里挑。
    无论哪条路,最后都做一次「导航噪声」检查 —— 抓到菜单宁可不给摘录,
    也不能把「学工动态 学生科技 就业指导」当成正文展示给用户。
    """
    html = html or ""
    page = CHROME_BLOCK.sub(" ", html)
    page = CHROME_DIV.sub(" ", page)

    # 1) 正文容器(最可靠)
    for pattern in BODY_CONTAINERS:
        for inner in _innermost_container(page, pattern):
            text = strip_tags(inner)
            text = _strip_crumb(text)
            if len(text) >= 40 and not looks_like_nav(text):
                return text[:cap]

    # 2) 退而求其次:去掉导航容器后,按 <p> 抽段落
    paras = []
    for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", page):
        one = strip_tags(p).replace("\n", " ")
        if len(one) >= 15:
            paras.append(one)
    text = _strip_crumb("\n".join(paras))
    if len(text) >= 150 and not looks_like_nav(text):
        return text[:cap]

    # 3) 兜底:整页文本,但要先剔掉导航噪声 —— 抽出来的若是菜单,就干脆不给摘录
    whole = _strip_crumb(strip_tags(page))
    if looks_like_nav(whole):
        return ""
    return whole[:cap]


def _strip_crumb(text):
    """去掉正文开头的面包屑(「您的位置 : 首页 新闻动态」这种)。"""
    text = re.sub(r"^\s*(?:您|你)?\s*的位置\s*[:：][^\n]*\n?", "", text or "")
    text = re.sub(r"^\s*当前位置\s*[:：][^\n]*\n?", "", text)
    return text.strip()


def meta_value(html, names):
    for name in names:
        m = re.search(r'(?is)<meta[^>]+(?:name|property)=["\']%s["\'][^>]*content=["\']([^"\']+)' % name, html)
        if m:
            return m.group(1).strip()
    return ""


# 发布日期在页面里的常见写法:有的站放 <meta>,有的站放一个「发布日期：」标签块
DATE_LABEL = re.compile(
    r"(?:发布(?:时间|日期)|时间|日期|更新时间|publish(?:ed)?[_\s-]?(?:date|time))"
    r"\s*[:：]\s*(?:<[^>]+>\s*)*([^<\n]{6,40})", re.I)
DATE_CLASS = re.compile(
    r"(?is)<(?:div|span|p|li)[^>]+class=[\"'][^\"']*"
    r"(?:cont_tit|pub(?:lish)?[-_]?(?:date|time)|article[-_]?(?:date|time)|news[-_]?(?:date|time)|date)"
    r"[^\"']*[\"'][^>]*>(.*?)</(?:div|span|p|li)>")
URL_DATE = re.compile(r"/(20\d{2})/(\d{2})(\d{2})/")


def publish_date(html, url=""):
    """从详情页里取发布日期。

    优先级:页面标签(发布日期：2026-01-15)→ <meta> → 带 pub/date class 的块 →
    URL 路径里的 /YYYY/MMDD/(这套 CMS 的详情 URL 天生带日期,是可靠的兜底)。
    """
    html = html or ""
    # 1) 「发布日期：2026-01-15」这类正文区块里的显式标签
    for m in DATE_LABEL.finditer(html):
        found = parse_date(strip_tags(m.group(1)), publish_context=True)
        if found:
            return found
    # 2) meta
    found = parse_date(meta_value(html, ["pubdate", "PubDate", "publishdate", "publishDate",
                                         "article:published_time", "date", "DC.date",
                                         "sdate", "pub_date"]), publish_context=True)
    if found:
        return found
    # 3) class 里带 date/pub 的块
    for m in DATE_CLASS.finditer(html):
        found = parse_date(strip_tags(m.group(1)), publish_context=True)
        if found:
            return found
    # 4) URL 里的 /YYYY/MMDD/
    m = URL_DATE.search(url or "")
    if m:
        value = iso(m.group(1), m.group(2), m.group(3))
        if plausible(value):
            return value
    return ""


# --------------------------------------------------------------------------
# 日期
# --------------------------------------------------------------------------
def iso(y, m, d):
    try:
        dt = datetime.datetime(int(y), int(m), int(d), 9, 0)
    except ValueError:
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def plausible(iso_date, days_ahead=45, earliest=2015):
    """只接受合理的日期:太早(站点归档)和过于离谱的都要挡掉。"""
    if not iso_date:
        return False
    try:
        day = datetime.datetime.strptime(iso_date[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    today = datetime.date.today()
    return datetime.date(earliest, 1, 1) <= day <= today + datetime.timedelta(days=days_ahead)


def parse_date(text, default_year=None, publish_context=True):
    """从一段文本里挑出第一个「像发布日期」的日期;没有合理的就返回空。

    publish_context=True(默认)时不允许未来日期 —— 列表窗口/正文里先出现的日期
    往往是**活动进行日期**(讲座、讨论班),不是发布日期;宁可留空交给「时间待定」,
    也不能把活动日期当成发布时间展示。活动时间走 parse_event_* 那一族,不在这里。
    """
    if not text:
        return ""
    # 西交机械列表把「09.20」和「2026」连在一起,先补个空格
    text = re.sub(r"(?<=\d)(20\d{2})(?=\D|$)", r" \1", text)
    today = datetime.date.today()
    limit = today.strftime("%Y-%m-%d") if publish_context else None
    full_seen = False
    for m in DATE_FULL.finditer(text):
        full_seen = True
        value = iso(m.group(1), m.group(2), m.group(3))
        if plausible(value) and (limit is None or value[:10] <= limit):
            return value
    for m in DATE_DMY.finditer(text):
        full_seen = True
        value = iso(m.group(3), m.group(2), m.group(1))
        if plausible(value) and (limit is None or value[:10] <= limit):
            return value
    if full_seen and publish_context:
        # 文本里有完整日期却被判为未来 —— 那是活动进行日期,别再退回「年月/月日」
        # 粗粒度解析(否则「2026年10月20日」会被截成 10 月 1 日混进发布时间)
        return ""
    m = DATE_YM.search(text)
    if m:
        value = iso(m.group(1), m.group(2), 1)
        if plausible(value) and (limit is None or value[:10] <= limit):
            return value
    year = default_year or (YEAR_ANY.search(text).group(1) if YEAR_ANY.search(text) else None)
    for m in DATE_MD.finditer(text):
        value = iso(year or datetime.date.today().year, m.group(1), m.group(2))
        if plausible(value) and (limit is None or value[:10] <= limit):
            return value
    return ""


# 英文月份（港大等英文站点的日期形如「11 Oct 2026」）
MONTHS_EN = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
             "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def parse_en_date(text):
    """解析英文日期：'11 Oct 2026' / 'October 11, 2026' / '2026-10-11' / ISO。

    先试英文月份写法，再退回 parse_date（中文/数字写法）。
    """
    if not text:
        return ""
    t = str(text).strip()
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})", t)      # 11 Oct 2026
    if m:
        mon = MONTHS_EN.get(m.group(2)[:3].lower())
        if mon:
            return iso(m.group(3), mon, m.group(1))
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", t)      # October 11, 2026
    if m:
        mon = MONTHS_EN.get(m.group(1)[:3].lower())
        if mon:
            return iso(m.group(3), mon, m.group(2))
    return parse_date(t)


def parse_event_range(text, year_hint=None):
    text = text or ""
    m = TIME_RANGE.search(text)
    if m:
        y, mo, d, h1, mi1, h2, mi2 = (int(x) for x in m.groups())
        return _range(y, mo, d, h1, mi1, h2, mi2)
    m = TIME_RANGE_MD.search(text)
    if m:
        mo, d, h1, mi1, h2, mi2 = (int(x) for x in m.groups())
        year = int(year_hint) if year_hint else datetime.date.today().year
        start = _range(year, mo, d, h1, mi1, h2, mi2)
        if start[0] and year_hint:
            # 跨年活动:月份比发布月靠前很多时按下一年的同一天算
            try:
                guess = datetime.date(year, mo, d)
                published = datetime.date(int(year_hint), 1, 1)
                if (published - guess).days > 300:
                    return _range(year + 1, mo, d, h1, mi1, h2, mi2)
            except ValueError:
                pass
        return start
    return [None, None]


def parse_event_from_tm(tm_text, year_hint=None):
    """把「2026年10月12日（周一）12:30-13:30」这类活动时间文本反解成 [开始, 结束]。"""
    text = re.sub(r"\s+", " ", tm_text or "")
    if not text:
        return [None, None]
    m = EV_TM_FULL.search(text)
    if m:
        y, mo, d, h1, mi1, h2, mi2 = (int(x) for x in m.groups())
        return _range(y, mo, d, h1, mi1, h2, mi2)
    m = EV_TM_ONE.search(text)
    if m:
        y, mo, d, hh, mm = (int(x) for x in m.groups())
        start = _one(y, mo, d, hh, mm, year_hint)
        return [start or None, None]
    m = EV_TM_MD.search(text)
    if m:
        mo, d, hh, mm = (int(x) for x in m.groups())
        year = int(year_hint) if year_hint else datetime.date.today().year
        start = _one(year, mo, d, hh, mm, year_hint) or _one(year + 1, mo, d, hh, mm, "9999")
        return [start or None, None]
    m = EV_TM_DATE.search(text)
    if m:
        value = iso(m.group(1), m.group(2), m.group(3))
        if plausible(value, days_ahead=400):
            return [value, None]
    return [None, None]


def _range(y, mo, d, h1, mi1, h2, mi2):
    try:
        start = datetime.datetime(y, mo, d, h1, mi1)
        end = datetime.datetime(y, mo, d, h2, mi2)
    except ValueError:
        return [None, None]
    if end <= start:
        return [None, None]
    return [start.strftime("%Y-%m-%dT%H:%M:%S+08:00"), end.strftime("%Y-%m-%dT%H:%M:%S+08:00")]


def parse_event_start(text, year_hint=None):
    """只有开始时间的情况:返回 ISO 字符串,时间要在发布日前后合理范围内。"""
    if not text:
        return ""
    m = START_FULL.search(text)
    if m:
        y, mo, d, hh, mm = (int(x) for x in m.groups())
        return _one(y, mo, d, hh, mm, year_hint)
    m = START_MD.search(text)
    if m:
        mo, d, hh, mm = (int(x) for x in m.groups())
        year = int(year_hint) if year_hint else datetime.date.today().year
        value = _one(year, mo, d, hh, mm, year_hint)
        if value:
            return value
        return _one(year + 1, mo, d, hh, mm, "9999")
    return ""


def _one(y, mo, d, hh, mm, year_hint):
    try:
        when = datetime.datetime(y, mo, d, hh, mm)
    except ValueError:
        return ""
    if year_hint and str(year_hint).isdigit():
        try:
            published = datetime.datetime(int(year_hint), 1, 1)
        except ValueError:
            published = datetime.datetime.now(CST)
        if not (-60 <= (when - published).days <= 400):
            return ""
    return when.strftime("%Y-%m-%dT%H:%M:%S+08:00")


# --------------------------------------------------------------------------
# 分类与字段拼装
# --------------------------------------------------------------------------
def classify(text):
    text = text or ""
    for name, pattern in CATEGORY_RULES:
        if re.search(pattern, text, re.I):
            return name
    return "校园动态"


def summarize(text, limit=240):
    text = re.sub(r"\s+", " ", text or "").strip()
    return (text[:limit].rstrip() + "…") if len(text) > limit else text


def excerpt(text, limit=520):
    text = re.sub(r"\s+", " ", text or "").strip()
    return (text[:limit].rstrip() + "…") if len(text) > limit else text


SITE_WORDS = ("大学", "学院", "学部", "新闻网", "新闻", "要闻", "综合新闻", "动态", "专题",
              "研究院", "研究生院", "教务处", "中心", "校友会", "中学", "附中")
SUBTITLE_HINTS = ("在", "关于", "为", "让", "对", "从", "与", "致", "给", "写", "记", "暨")
SITE_NAME_ONLY = re.compile(r"^[\u4e00-\u9fa5A-Za-z]{2,14}(大学|学院|学部|新闻网|中心|招生网|就业网|教务处|研究生院|研究生招生网)$")
# 详情页元信息栏(被某些站误做成 <h1>):「信息来源： 发布日期: … 浏览次数: …」
META_BAR_JUNK = re.compile(r"信息来源|发布日期\s*[:：]|浏览次数|阅读次数|点击次数|_showDynClicks|点击数[:：]")


def looks_like_site_label(tail):
    """判断分隔符后面那一小段是「站点/栏目名」而不是副标题。"""
    if not (2 <= len(tail) <= 16):
        return False
    if re.search(r"[：，。！？；、,]", tail):
        return False
    if tail.startswith(SUBTITLE_HINTS):
        return False
    return any(word in tail for word in SITE_WORDS)


def _strip_one_suffix(text):
    """剥掉一层站点/栏目后缀,没有可剥的就原样返回。"""
    for sep in (" - ", " — ", " _ ", " | ", "-", "_", "|", "｜"):
        idx = text.rfind(sep)
        if idx <= 5:
            continue
        if looks_like_site_label(text[idx + len(sep):].strip()):
            return text[:idx].strip()
    return text


def strip_site_suffix(text):
    """把「标题 - 上海交通大学」「标题_交大要闻」这类站点/栏目后缀去掉,
    但不误伤标题里的连字符(如 2026-2027 学年)和「主标题 | 副标题」。

    有的页面标题挂了两层后缀(「标题_综合新闻 - 上海交通大学新闻网」),所以要反复剥。
    """
    text = re.sub(r"\s+", " ", text or "").strip()
    for _ in range(4):
        stripped = _strip_one_suffix(text)
        if stripped == text:
            break
        text = stripped
    return text


def strip_site_prefix(text):
    """上交的页面标题形如「上海交通大学中文主页_真正的标题_」,把站点前缀和尾巴去掉。"""
    text = re.sub(r"\s+", " ", text or "").strip()
    for _ in range(2):
        changed = False
        for sep in ("_", "|", "｜", " - "):
            idx = text.find(sep)
            if 2 <= idx <= 16 and looks_like_site_label(text[:idx].strip()):
                text = text[idx + len(sep):].strip()
                changed = True
                break
        if not changed:
            break
    return text.strip(" _-|｜·、")


def pick_title(anchor, detail_html):
    """标题优先级:h1 → <title> → 列表页锚文本;都不合适就截断锚文本。

    注意:有些站(如同济研招网 yz.tongji.edu.cn)把元信息栏做成了 <h1>
    「信息来源： 发布日期: … 浏览次数: …」,真正的标题在 <h2>。这类
    元信息栏文本要用 META_BAR_JUNK 剔除,否则会被当成标题入库。
    """
    h1 = ""
    m = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", detail_html or "")
    if m:
        h1 = re.sub(r"\s+", " ", strip_tags(m.group(1))).strip()
    raw = [h1, page_title(detail_html or ""), anchor or ""]
    candidates = [strip_site_prefix(strip_site_suffix(re.sub(r"\s+", " ", c).strip())) for c in raw]
    for cand in candidates:
        if not cand or SITE_NAME_ONLY.match(cand) or META_BAR_JUNK.search(cand):
            continue
        if 6 <= len(cand) <= 80:
            return cand
    for cand in candidates:
        if cand and not SITE_NAME_ONLY.match(cand) and not META_BAR_JUNK.search(cand):
            return cand[:60]
    # 全是元信息栏时,兜底从 <h2> 或正文首行找,再不行就空着(页面会显示「查看原文」)
    m = re.search(r"(?is)<h[23][^>]*>(.*?)</h[23]>", detail_html or "")
    if m:
        cand = strip_site_prefix(strip_site_suffix(re.sub(r"\s+", " ", strip_tags(m.group(1))).strip()))
        if cand and not META_BAR_JUNK.search(cand):
            return cand[:80]
    return (anchor or "")[:60]


def pick_fields(body, title, source_name, year_hint=None):
    dt = {}
    text = body or ""
    # 活动时间:排除「发布时间/更新时间/截止时间」这类,并检查取到的值确实像时间
    m = re.search(NOT_A_TIME_LABEL + r"(活动时间|讲座时间|报告时间|会议时间|论坛时间|演出时间|培训时间|举办时间"
                  r"|时间)\s*[:：]\s*([^\n]{4,60})", text)
    if m and not re.search(r"来源|作者|点击|发布", m.group(2)):
        value = re.sub(r"\s+", " ", m.group(2)).strip()
        if re.search(r"\d", value) and re.search(r"[月日周点时分]|\d\s*[:：]\s*\d", value):
            dt["tm"] = summarize(value, 60)
    m = re.search(r"(活动地点|讲座地点|报告地点|会议地点|地点|会议室)\s*[:：]\s*([^\n]{2,60})", text)
    if m and not re.search(r"来源|作者|点击", m.group(2)):
        dt["lo"] = summarize(m.group(2), 60)
    m = re.search(r"(主办单位|主办方|主办|承办单位|承办)\s*[:：]\s*([^\n]{2,50})", text)
    if m:
        dt["og"] = summarize(m.group(2), 50)
    else:
        dt["og"] = source_name
    dt["ev"] = parse_event_range(text[:800], year_hint) or parse_event_range(title, year_hint)
    if not dt["ev"][0]:
        start = parse_event_start(text[:800], year_hint) or parse_event_start(title, year_hint)
        if start:
            dt["ev"] = [start, None]
    if not dt["ev"][0] and dt.get("tm"):
        # 已经识别出「活动时间：…」,直接按它反解(那些文本格式比正文更规整)
        dt["ev"] = parse_event_from_tm(dt["tm"], year_hint)
    dt["sg"] = None      # 由调用方填
    return dt


def make_item(url, title, published, source, group, body):
    category = classify(title + " " + (body or "")[:120])
    intent = "activity" if category in ACTIVITY_CATEGORIES else "information"
    year_hint = (published or "")[:4] or None
    item = {
        "i": url,
        "t": summarize(title, 200) or url,
        "s": source,
        "si": None,
        "d": published or "",
        "u": url,
        "c": category,
        "g": intent,
        "en": "",
        "ens": "",
    }
    detail = {}
    if body:
        # 只放正文摘录:摘要若与摘录同源会在弹窗里重复出现
        detail["tx"] = excerpt(body, 520)
    fields = pick_fields(body or "", title, source, year_hint)
    for key in ("tm", "lo", "og"):
        if fields.get(key):
            detail[key] = fields[key]
    if fields.get("ev") and fields["ev"][0]:
        detail["ev"] = fields["ev"]
    detail["sg"] = group
    if detail:
        item["dt"] = detail
    return item


# --------------------------------------------------------------------------
# 微信公众号文章
# --------------------------------------------------------------------------
def wechat_var(html, name):
    m = (re.search(r'var\s+%s\s*=\s*(?:htmlDecode\()?"([^"]*)"' % re.escape(name), html)
         or re.search(r"var\s+%s\s*=\s*(?:htmlDecode\()?'([^']*)'" % re.escape(name), html))
    if not m:
        return ""
    value = m.group(1)
    return (value.replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&")
                 .replace("&lt;", "<").replace("&gt;", ">").strip())


def wechat_account(html):
    """公众号名:变量里有就用变量,否则退回页面里的 js_name。"""
    for name in ("nickname", "user_name", "author", "profile_nickname"):
        value = wechat_var(html, name)
        if value:
            return value
    m = re.search(r'id=["\']js_name["\'][^>]*>\s*([^<]{2,40})', html)
    return m.group(1).strip() if m else ""


def fetch_wechat(url, delay):
    try:
        html, _ = fetch(url, delay=delay)
    except Exception:  # noqa: BLE001
        return None
    title = wechat_var(html, "msg_title") or page_title(html)
    account = wechat_account(html)
    ct = wechat_var(html, "ct")
    published = ""
    if ct.isdigit():
        published = datetime.datetime.fromtimestamp(int(ct), CST).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    m = re.search(r'(?is)id=["\']js_content["\'][^>]*>(.{0,300000})', html)
    body = ""
    if m:
        chunk = m.group(1)
        end = re.search(r'(?is)<script|<div class="rich_media_tool', chunk)
        if end:      # 没找到结束标记说明页面被截断了,那就用到手的部分
            chunk = chunk[:end.start()]
        body = strip_tags(chunk)
    if not title or len(body) < 60:
        return None
    return {"title": title, "account": account or "微信公众号",
            "published": published, "body": body.replace("\n", " ")}


# --------------------------------------------------------------------------
# 抓一个校区
# --------------------------------------------------------------------------
def title_key(text):
    """标题指纹:去掉空白与标点,用于跨栏目去重(同一篇稿子常挂在多个栏目下)。"""
    return re.sub(r"[\s\W_]+", "", text or "")[:48].lower()


def registry_path(campus):
    return os.path.join(WORK_DIR, f"registry-{campus}.json")


def load_registry(campus):
    try:
        with open(registry_path(campus), encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:  # noqa: BLE001
        return {}


def save_registry(campus, registry):
    os.makedirs(WORK_DIR, exist_ok=True)
    with open(registry_path(campus), "w", encoding="utf-8") as fh:
        json.dump(registry, fh, ensure_ascii=False, indent=1, sort_keys=True)


def previous_ids(campus):
    """data/<校区>.json 里已有的条目 id —— 用来判断这一轮哪些是「新发布」。"""
    path = os.path.join(DATA_DIR, f"{campus}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return {it.get("i") for it in (json.load(fh) or {}).get("items") or [] if it.get("i")}
    except Exception:  # noqa: BLE001
        return set()


def source_status(new_count, failed=False):
    """来源状态:failed 抓取失败;ok 有新增;no_recent 抓到了但没有新内容。"""
    if failed:
        return "failed"
    return "ok" if new_count else "no_recent"


def load_extra_sources(campus):
    """从 _work/sources-<校区>.json 读补充信息源 —— 新增/停用信息源不必改这个文件的代码。

    文件是一份 JSON 数组,每项与 SOURCES 里的写法一致:
        [{"id": "zju_xxx", "name": "某某学院", "url": "https://...", "kind": "list", "max": 6}]
    同 id 会覆盖内置配置("enabled": false 可临时停用某个来源,不抓但保留注册表记录);
    只认 id/name/url 齐全的条目,坏条目跳过并提示,不会让整轮抓取失败。
    """
    path = os.path.join(WORK_DIR, f"sources-{campus}.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! {path} 读不出来，已忽略：{exc}")
        return []
    rows = raw.get("sources") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        print(f"  ! {path} 应为数组（或 {{\"sources\": [...]}}），已忽略")
        return []
    out = []
    for item in rows:
        if not isinstance(item, dict) or not (item.get("id") and item.get("url")):
            print(f"  ! 跳过缺少 id/url 的来源：{item!r}")
            continue
        entry = dict(item)
        entry.setdefault("name", entry["id"])
        entry.setdefault("group", "official")
        entry.setdefault("kind", "list")
        out.append(entry)
    return out


def merge_sources(campus):
    """内置 SOURCES + _work/sources-<校区>.json 的补充(同 id 覆盖、enabled=false 停用)。"""
    merged = []
    index = {}
    for src in list(SOURCES.get(campus) or []) + load_extra_sources(campus):
        if not isinstance(src, dict) or not src.get("id") or not src.get("url"):
            continue
        key = str(src["id"])
        if key in index:                       # 同 id:后者覆盖前者
            merged[index[key]] = src
        else:
            index[key] = len(merged)
            merged.append(src)
    return [s for s in merged if s.get("enabled", True) is not False]


def json_source_items(src, delay, timeout=None):
    """抓取 kind=json 的来源（返回 JSON 的接口），产出 [(url, title, published, summary)]。

    专门给「整站前端渲染、没有静态文章链接」的站点用（如港大新版官网 newsroom）。
    可配置字段（都带默认值，最少只写 url + items_path 即可）：
      items_path    条目数组在 JSON 里的路径，点号分层，默认 "items"（如 "result.items"）
      title_field   标题字段名，默认 "title"
      link_field    链接字段名，默认 "url"（相对路径会用 base 补全）
      date_field    日期字段名，默认 "date"（支持英文月份写法）
      summary_field 摘要字段名，默认 "summary"（用作正文摘录）
      base          相对链接前缀，默认取 url 的 scheme://host
      api           真正的接口地址；给了它就抓 api，而 url 只作为页面展示用的来源地址
    """
    endpoint = src.get("api") or src["url"]
    timeout = timeout if timeout is not None else src.get("timeout")
    text, _final = fetch(endpoint, delay=delay, timeout=timeout)
    data = json.loads(text)
    node = data
    for part in str(src.get("items_path", "items")).split("."):
        if not part:
            continue
        node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            break
    if not isinstance(node, list):
        return []
    parsed = urllib.parse.urlparse(endpoint)
    base = src.get("base") or f"{parsed.scheme}://{parsed.netloc}"
    title_field = src.get("title_field", "title")
    link_field = src.get("link_field", "url")
    date_field = src.get("date_field", "date")
    summary_field = src.get("summary_field", "summary")
    out = []
    for row in node:
        if not isinstance(row, dict):
            continue
        title = str(row.get(title_field) or "").strip()
        link = str(row.get(link_field) or "").strip()
        if not title or not link:
            continue
        url = urllib.parse.urljoin(base, link)
        published = parse_en_date(row.get(date_field)) if row.get(date_field) else ""
        summary = str(row.get(summary_field) or "").strip()
        out.append((url, title, published, summary))
    return out


def crawl_campus(campus, args):
    sources = merge_sources(campus)
    if not sources:
        print(f"✗ 没有为 {campus} 配置信息源")
        return []
    items, seen, seen_titles = [], set(), set()
    stats = {}
    known = previous_ids(campus)
    refresh_known = bool(getattr(args, "refresh_known", False))
    registry = load_registry(campus)
    now = now_iso_cst()
    print(f"· [{campus}] 本轮信息源 {len(sources)} 个"
          f"（内置 {len(SOURCES.get(campus) or [])} + 补充 "
          f"{len(load_extra_sources(campus))}）")

    if getattr(args, "rotate", False) and len(sources) > 1:
        # 站群 WAF 按时间窗口封 IP 时,被挡的总是「抓取顺序中段」那批来源
        # (2026-10-10 实测:zju 两轮失败的都是配置顺序中段的同一批学院)。
        # 每轮按一年中的第几天轮换抓取起点,让封锁窗口每轮落在不同来源上,
        # 即使仍触发 WAF,所有来源也能在几天内轮流抓到,不会饿死固定一批。
        shift = datetime.datetime.now(CST).timetuple().tm_yday % len(sources)
        if shift:
            sources = sources[shift:] + sources[:shift]
        print(f"· [{campus}] 来源顺序已轮换（起点 +{shift}，规避 WAF 窗口固定打在同一批来源上）")

    def record(key, name, group, new_count, count, status, url="", source_id="", kind="list"):
        old = registry.get(key) or {}
        registry[key] = {
            "name": name, "group": group, "kind": kind, "url": url, "source_id": source_id,
            "status": status, "count": count, "new": new_count,
            "last_checked": now,
            "first_seen": old.get("first_seen") or now,
            "last_new": now if (status == "ok") else old.get("last_new", ""),
        }

    def crawl_source(src):
        """抓一个来源。**不写任何共享状态**,把结果打包返回给主线程按顺序合并,
        这样多个来源可以并行(不同主机),同主机靠 polite_wait 限速。
        """
        log = []
        tmo = src.get("timeout")          # 来源级超时覆盖（个别站特别慢时用）
        limit = args.max_per_source or src.get("max") or 15
        log.append(f"\n· [{campus}] {src['name']}  ({src['url']})")
        out = {"src": src, "items": [], "rows": [], "picked": 0, "log": log}

        # ---- JSON 接口型来源（前端整站渲染、没有静态列表页的站点，如港大）----
        if src.get("kind") == "json":
            try:
                rows = json_source_items(src, args.delay)
            except Exception as exc:  # noqa: BLE001
                log.append(f"    ✗ 接口失败：{exc}")
                old = registry.get(src["id"]) or {}
                out["rows"].append(dict(
                    key=src["id"], name=src["name"], group=src.get("group", "official"),
                    new=0, count_raw=old.get("count", 0), accumulate=False,
                    status="failed", url=src["url"], source_id=src["id"], kind="json"))
                return out
            rows.sort(key=lambda r: r[2] or "", reverse=True)
            picked, picked_ids, local_titles = 0, [], set()
            skipped_known = 0
            for url, title, published, summary in rows:
                if picked >= limit:
                    break
                if title_key(title) in local_titles:
                    continue
                # 增量：已在库的条目不再抓详情（同官网来源的处理，见下方注释）
                if url in known and not refresh_known:
                    local_titles.add(title_key(title))
                    picked_ids.append(url)
                    picked += 1
                    skipped_known += 1
                    continue
                body = summary
                if not args.no_details and src.get("details", True) is not False:
                    try:
                        detail_html, _ = fetch(url, delay=args.delay, timeout=tmo)
                        full = extract_body(detail_html)
                        if full:
                            body = full
                        if not published:
                            published = publish_date(detail_html, url)
                        title = pick_title(title, detail_html)
                    except Exception as exc:  # noqa: BLE001
                        log.append(f"    ! 详情失败 {url[:60]}：{exc}")
                key = title_key(title)
                if key in local_titles:
                    continue
                item = make_item(url, title, published, src["name"], "official", body)
                item["si"] = src["id"]
                out["items"].append(item)
                local_titles.add(key)
                picked += 1
                picked_ids.append(url)
            src_new = len([i for i in picked_ids if i not in known])
            out["rows"].append(dict(
                key=src["id"], name=src["name"], group=src.get("group", "official"),
                new=src_new, count_raw=picked, accumulate=False, status=source_status(src_new),
                url=src["url"], source_id=src["id"], kind="json"))
            out["picked"] = picked
            log.append(f"    ✓ 文章 {picked} 条" + (f"  [新增 {src_new}]" if src_new else "  [无新增]"))
            return out

        try:
            html, final = fetch(src["url"], delay=args.delay, timeout=tmo)
        except Exception as exc:  # noqa: BLE001
            log.append(f"    ✗ 列表页失败：{exc}")
            old = registry.get(src["id"]) or {}
            out["rows"].append(dict(
                key=src["id"], name=src["name"], group=src.get("group", "official"),
                new=0, count_raw=old.get("count", 0), accumulate=False,
                status="failed", url=src["url"], source_id=src["id"],
                kind=src.get("kind", "list")))
            return out

        base_host = host_of(final)
        # link_re：现代框架站点(通知详情是 /post/detail/xxx、?id=123 这种，没有
        # .htm + 数字路径)用自定义正则挑出文章链接；配置了 link_re 就只认它。
        link_re = src.get("link_re")
        if link_re:
            link_re = re.compile(link_re)
        found, seen_here = [], set()
        for href, text, pos in anchors(html, final):
            if link_re:
                if host_of(href) != base_host or not link_re.search(href):
                    continue
            elif not is_article_link(href, base_host):
                continue
            if href in seen_here:
                continue
            if len(re.sub(r"\s", "", text)) < 6:
                continue
            window = strip_tags(html[max(0, pos - 300):pos + 300]).replace("\n", " ")
            seen_here.add(href)
            found.append((href, text, window))

        # 只抓网站内容：不再从网页里摘 mp.weixin.qq.com 链接。
        # 保留这段逻辑并用 kind 判定，是为了「以后要恢复某个公众号来源」时只需
        # 把该来源的 kind 改成 wechat，不用再改代码。当前 SOURCES 无 kind=wechat。
        wx_links = []
        if src.get("kind") == "wechat":
            for href in WX_RE.findall(html):
                href = href.split("&scene=")[0].split("#")[0]
                if href not in wx_links:
                    wx_links.append(href)
            found = []

        picked = 0
        picked_ids = []
        local_titles = set()      # 来源内去重(跨来源去重交给主线程按顺序做)
        skipped_known = 0         # 已在库、跳过正文抓取的条数
        # 列表页顺序不一定按时间倒序(有的站栏目混排),先按窗口里的发布日期从新到旧
        # 排一遍再截断,保证「刚发布的条目」一定落在 limit 之内,不会被旧稿挤掉。
        dated = []
        for idx, (href, text, window) in enumerate(found):
            dated.append((parse_date(window) or "", -idx, href, text, window))
        dated.sort(reverse=True)      # 有日期的排前面且新→旧,同日按页面原有顺序
        for published_hint, _, href, text, window in dated:
            if picked >= limit:
                break
            key = title_key(text)
            if key in local_titles:
                continue
            # ---- 增量优化：这条 URL 已经在 data/ 里 → 不抓正文、不重复入库 ----
            # 每轮列表页里 90% 以上是旧稿，它们的历史条目 refresh.py 会按 id 保留，
            # 再抓一次正文只是白花请求。只有新稿才需要详情页(标题/日期/正文)。
            if href in known and not refresh_known:
                local_titles.add(key)
                picked_ids.append(href)
                picked += 1
                skipped_known += 1
                continue
            published = published_hint
            body = ""
            if not args.no_details and src.get("details", True) is not False:
                try:
                    detail_html, _ = fetch(href, delay=args.delay, timeout=tmo)
                    body = extract_body(detail_html)
                    if not published:
                        # 列表窗口里没日期 → 从详情页取:显式标签 → meta → URL 路径
                        published = publish_date(detail_html, href)
                    text = pick_title(text, detail_html)
                except Exception as exc:  # noqa: BLE001
                    log.append(f"    ! 详情失败 {href[:60]}：{exc}")
            key = title_key(text)
            if key in local_titles:      # 详情页标题暴露出的重复
                continue
            item = make_item(href, text, published, src["name"], "official", body)
            item["si"] = src["id"]
            out["items"].append(item)
            local_titles.add(key)
            picked += 1
            picked_ids.append(href)
        out["skipped_known"] = skipped_known

        # 页面里链接的官方公众号文章
        wx_picked = 0
        wx_skipped = 0
        wx_by_account = {}
        registered = WECHAT_ACCOUNTS.get(campus) or []
        parts = [re.escape(name) for name in registered]
        if WECHAT_ACCOUNT_FILTER.get(campus):
            parts.append(WECHAT_ACCOUNT_FILTER[campus])
        pattern = "|".join(parts) if parts else None
        for href in wx_links[: (args.max_wechat or src.get("max") or 8)]:
            got = fetch_wechat(href, args.delay)
            if not got:
                continue
            if pattern and not re.search(pattern, got["account"]):
                wx_skipped += 1
                continue
            key = title_key(got["title"])
            if key in local_titles:
                continue
            item = make_item(href, got["title"], got["published"], got["account"], "wechat", got["body"])
            item["si"] = "wx:" + got["account"]
            out["items"].append(item)
            local_titles.add(key)
            wx_picked += 1
            picked_ids.append(href)
            wx_by_account.setdefault(got["account"], []).append(href)

        # 记录来源状态:官网来源一条,公众号按号各一条
        src_new = len([i for i in picked_ids if i not in known])
        out["rows"].append(dict(
            key=src["id"], name=src["name"], group=src.get("group", "official"),
            new=src_new, count_raw=picked, accumulate=False,
            status=source_status(src_new), url=src["url"], source_id=src["id"],
            kind=src.get("kind", "list")))
        for account, hrefs in wx_by_account.items():
            acc_new = len([i for i in hrefs if i not in known])
            out["rows"].append(dict(
                key="wx:" + account, name=account, group="wechat",
                new=acc_new, count_raw=len(hrefs), accumulate=True,
                status=source_status(acc_new), url="https://mp.weixin.qq.com/",
                source_id="", kind="wechat"))

        out["picked"] = picked
        extra = ""
        if wx_picked:
            extra += f" + 公众号 {wx_picked} 条"
        if wx_skipped:
            extra += f"（{wx_skipped} 篇非本校账号已跳过）"
        extra += f"  [新增 {src_new}]" if src_new else "  [无新增]"
        log.append(f"    ✓ 文章 {picked} 条" + extra)
        return out

    # ---- 按来源并行抓取(不同主机之间并行;同主机由 polite_wait 串行) ----
    workers = max(1, int(getattr(args, "workers", 1) or 1))
    if workers > 1 and len(sources) > 1:
        print(f"· [{campus}] 并发 {workers} 路（同一主机仍保持 {args.delay}s 间隔）")
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(crawl_source, sources))
    else:
        results = [crawl_source(src) for src in sources]

    # ---- 按来源配置顺序合并:日志按序打印,条目跨来源去重(先到先得) ----
    seen_ids = set()
    skipped_total = 0
    for res in results:
        for line in res["log"]:
            print(line)
        skipped_total += res.get("skipped_known", 0)
        for row in res["rows"]:
            count = row["count_raw"]
            if row["accumulate"] and row["new"] == 0:
                count += (registry.get(row["key"]) or {}).get("count", 0)
            record(row["key"], row["name"], row["group"], row["new"], count,
                   row["status"], row["url"], row["source_id"], row["kind"])
        for it in res["items"]:
            if it["i"] in seen_ids:
                continue
            key = title_key(it["t"])
            if key in seen_titles:
                continue
            seen_ids.add(it["i"])
            seen_titles.add(key)
            seen.add(it["u"])
            items.append(it)
        stats[res["src"]["id"]] = res["picked"]

    # 只抓网站内容：不再自动读取 _work/wechat/wechat-<校区>.txt 里的公众号清单。
    # 真要单独补几篇公众号文章，显式传 --wechat-file 路径（默认不抓公众号）。
    manual_files = []
    if args.wechat_file:
        manual_files.append(args.wechat_file)

    for manual_path in manual_files:
        if not os.path.exists(manual_path):
            print(f"    ! 找不到清单 {manual_path}")
            continue
        with open(manual_path, encoding="utf-8") as fh:
            manual = [line.strip() for line in fh
                      if line.strip() and not line.strip().startswith("#")]
        got_any = 0
        for href in manual:
            if href in seen or WX_HOST not in href:
                continue
            got = fetch_wechat(href, args.delay)
            if not got:
                continue
            key = title_key(got["title"])
            if key in seen_titles:
                continue
            item = make_item(href, got["title"], got["published"], got["account"], "wechat", got["body"])
            item["si"] = "wx:" + got["account"]
            items.append(item)
            seen.add(href)
            seen_titles.add(key)
            got_any += 1
            new_count = 0 if href in known else 1
            old = registry.get("wx:" + got["account"]) or {}
            record("wx:" + got["account"], got["account"], "wechat", new_count,
                   old.get("count", 0) + 1, source_status(new_count),
                   "https://mp.weixin.qq.com/", "", "wechat-list")
        print(f"    ✓ 清单 {os.path.basename(manual_path)} 收录 {got_any} 篇"
              f"（共 {len(manual)} 行）")

    registered = WECHAT_ACCOUNTS.get(campus) or []
    if registered:
        have = {it["s"] for it in items if (it.get("dt") or {}).get("sg") == "wechat"}
        got = [name for name in registered if name in have]
        for name in registered:
            if name in have:
                continue
            old = registry.get("wx:" + name) or {}
            if not old:
                # 已登记但从未取到内容
                record("wx:" + name, name, "wechat", 0, 0, "pending",
                       "https://mp.weixin.qq.com/", "", "wechat")
        print(f"· 已登记公众号 {len(registered)} 个：有内容 {len(got)} 个"
              + (f"（{'、'.join(got)}）" if got else "")
              + f"，待发现 {len(registered) - len(got)} 个")

    # 来源注册表与当前配置对齐：SOURCES 里已经删掉的来源，其历史状态一并清掉，
    # 否则页面「信息来源」会一直残留早已下线的栏目（只增不减只对"还在配置里的来源"生效）。
    valid_ids = {s["id"] for s in sources}
    dropped = [k for k in registry if k not in valid_ids]
    for k in dropped:
        registry.pop(k, None)
    if dropped:
        print(f"· [{campus}] 清理已下线来源 {len(dropped)} 个："
              + "、".join(dropped[:8]) + (" …" if len(dropped) > 8 else ""))

    save_registry(campus, registry)
    items.sort(key=lambda x: (x.get("d") or "", x["i"]), reverse=True)
    if skipped_total:
        print(f"· [{campus}] 增量：{skipped_total} 条已在库，跳过正文抓取"
              f"（省下约 {skipped_total} 次请求）")
    print(f"\n· [{campus}] 合计 {len(items)} 条；注册表 {len(registry)} 个来源"
          f"（{sum(1 for v in registry.values() if v.get('status') == 'ok')} 个本轮有新增）")
    return items


# --------------------------------------------------------------------------
def write_items(campus, items):
    os.makedirs(WORK_DIR, exist_ok=True)
    path = os.path.join(WORK_DIR, f"items-{campus}.json")
    registry = load_registry(campus)
    payload = {"campus": campus, "items": items, "source_registry": registry, "append_only": True}
    if WECHAT_ACCOUNTS.get(campus):
        # 带上登记名单,页面才能显示「已登记、暂无内容」的公众号
        payload["wechat_accounts"] = WECHAT_ACCOUNTS[campus]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    print(f"✓ {path}  ({len(items)} 条, {os.path.getsize(path)/1024:.0f} KB)")
    return path



def do_import(campus, path):
    """把抓到的条目交给 refresh.py 的写入保护流程,再重新生成页面。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("refresh", os.path.join(HERE, "refresh.py"))
    refresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(refresh)
    return refresh.import_items(campus, path)


def main():
    ap = argparse.ArgumentParser(description="抓取各高校(浙大/上交/西交)的官网与职能部门")
    ap.add_argument("--campus", default=DEFAULT_CAMPUSES,
                    help="校区，逗号分隔：" + " / ".join(SOURCES))
    ap.add_argument("--max-per-source", type=int, default=0,
                    help="每个来源最多取多少条；0(默认)表示按每个来源自身配置的 max 取值")
    ap.add_argument("--max-wechat", type=int, default=8, help="每个来源最多抓多少篇公众号文章")
    ap.add_argument("--delay", type=float, default=1.2, help="同一主机两次请求的最小间隔(秒)")
    ap.add_argument("--domain-delay", type=float, default=0.0,
                    help="同一注册域名(如 zju.edu.cn)下所有子域聚合请求的最小间隔(秒);"
                         "默认 0 不启用。浙大站群 WAF 按 IP 对整个域名限流,抓 zju 时建议 1.0~2.0")
    ap.add_argument("--rotate", action="store_true",
                    help="按日期轮换来源抓取起点,让 WAF 封锁窗口每轮落在不同来源上(zju 建议开启)")
    ap.add_argument("--workers", type=int, default=4,
                    help="并发抓取的来源数(不同主机之间并行,同一主机仍按 --delay 限速;"
                         "默认 4,设 1 为纯串行)")
    ap.add_argument("--retries", type=int, default=2,
                    help="单个页面失败后的重试次数(默认 2;网络受限的云端环境可设 1 或 0 以压短耗时)")
    ap.add_argument("--timeout", type=float, default=25,
                    help="单次请求超时秒数(默认 25)。几十所学校规模下建议 10~12："
                         "卡住的来源最坏耗时 = timeout ×(retries+1)")
    ap.add_argument("--no-details", action="store_true", help="不抓正文,只取标题/时间/链接")
    ap.add_argument("--refresh-known", action="store_true",
                    help="连已在库的条目也重抓详情（默认跳过：这类占每轮请求的 90%% 以上）。"
                         "只有在需要修补旧条目的标题/正文时才用")
    ap.add_argument("--no-import", action="store_true", help="只抓取并输出到 _work/,不写 data/")
    ap.add_argument("--import-only", action="store_true", help="不联网,把 _work/ 上次的结果写入 data/")
    ap.add_argument("--wechat-file", help="额外的公众号文章链接清单(一行一个,含 # 的行忽略)")
    ap.add_argument("--no-build", action="store_true",
                    help="抓取后不重新生成 index.html(云端并行任务里由统一的 build 步骤生成)")
    args = ap.parse_args()

    global RETRY_COUNT, DOMAIN_DELAY, HTTP_TIMEOUT
    if args.retries is not None and args.retries >= 0:
        RETRY_COUNT = int(args.retries)
    if args.domain_delay and args.domain_delay > 0:
        DOMAIN_DELAY = float(args.domain_delay)
    if args.timeout and args.timeout > 0:
        HTTP_TIMEOUT = float(args.timeout)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    campuses = [c.strip() for c in args.campus.split(",") if c.strip()]
    written = []
    t_all = time.time()
    for campus in campuses:
        if campus not in SOURCES:
            print(f"✗ 未知校区 {campus}，可选：{', '.join(SOURCES)}")
            continue
        if args.import_only:
            path = os.path.join(WORK_DIR, f"items-{campus}.json")
            if not os.path.exists(path):
                print(f"✗ 找不到 {path}，请先不带 --import-only 跑一次")
                continue
            print(f"· [{campus}] 用上次抓到的 {path}")
        else:
            t0 = time.time()
            req0 = REQUESTS
            items = crawl_campus(campus, args)
            print(f"⏱ [{campus}] 抓取用时 {time.time() - t0:.1f}s，"
                  f"请求 {REQUESTS - req0} 次")
            if not items and not previous_ids(campus):
                # 一条都没抓到、且库里本来也没有 → 真异常，别拿空数据覆盖
                print(f"✗ [{campus}] 一条都没抓到，保留现有数据")
                continue
            if not items:
                # 增量模式的正常结果：所有条目都已在库，没有新稿要写。
                # 仍然要走一次导入，把来源状态(上次检查时间/是否有新增)刷新上页面。
                print(f"· [{campus}] 本轮无新增，只刷新来源状态")
            path = write_items(campus, items)
        written.append((campus, path))

    print(f"\n⏱ 抓取阶段合计 {time.time() - t_all:.1f}s，请求 {REQUESTS} 次"
          f"（{len(written)} 个校区）")

    if args.no_import or not written:
        print("\n(未写入 data/：--no-import)")
        return

    for campus, path in written:
        do_import(campus, path)

    if args.no_build:
        print("\n(未重建页面：--no-build；由外部统一 build)")
        return

    import importlib.util
    spec = importlib.util.spec_from_file_location("refresh", os.path.join(HERE, "refresh.py"))
    refresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(refresh)
    refresh.build()


if __name__ == "__main__":
    main()
