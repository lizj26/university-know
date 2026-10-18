#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线单测:标题清洗 / 日期合理性 / 活动时间抽取。不联网。"""
import importlib.util
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
# tests/ 在项目根下一层,所以 ROOT = 上两级
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("fc", os.path.join(ROOT, "fetch_campus.py"))
fc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fc)

cases = [
    ("上交 tg 标题(前缀+后缀)",
     lambda: fc.pick_title("通知",
                           '<html><head><title>上海交通大学中文主页_2026年上海市“中华文化国际传播”项目申报的通知_上海交通大学</title></head></html>'),
     '2026年上海市“中华文化国际传播”项目申报的通知'),
    ("西交 mec 标题(后缀)",
     lambda: fc.pick_title("09.20 2026 关于举办xx大赛的通知",
                           '<html><head><title>关于组织开展2027年第十届中国大学生工程实践与创新能力大赛相关工作的预通知-西安交通大学机械工程学院</title></head></html>'),
     "关于组织开展2027年第十届中国大学生工程实践与创新能力大赛相关工作的预通知"),
    ("站点名不能当标题",
     lambda: fc.pick_title("某篇文章的标题在这里", '<html><head><title>上海交通大学</title></head></html>'),
     "某篇文章的标题在这里"),
    ("标题:两层站点后缀都要剥",
     lambda: fc.pick_title("某某论坛举行_综合新闻 - 上海交通大学新闻网", ""), "某某论坛举行"),
    ("标题:站点前缀+后缀",
     lambda: fc.pick_title("上海交通大学中文主页_关于评选2026年度奖学金的通知_", ""), "关于评选2026年度奖学金的通知"),
    ("标题:年份连字符不能被当后缀",
     lambda: fc.pick_title("2026-2027学年第一学期选课通知", ""), "2026-2027学年第一学期选课通知"),
    ("锚文本是长摘要时截断",
     lambda: fc.pick_title("很长很长的摘要" * 20, "<html></html>"),
     None),
    ("元信息栏 h1 不能当标题(同济研招网)",
     lambda: fc.pick_title("",
                           '<html><body><h1>信息来源： 发布日期: 2026-10-08 '
                           '浏览次数: _showDynClicks("wbnews", 1, 2)</h1>'
                           '<h2>2027年全国硕士研究生招生考试同济大学报考点网报公告</h2></body></html>'),
     "2027年全国硕士研究生招生考试同济大学报考点网报公告"),
    ("元信息栏混入候选时回退到锚文本",
     lambda: fc.pick_title("关于自取及寄发2026年硕士研究生录取通知书的通知",
                           '<html><head><title>同济大学研究生招生网</title></head><body>'
                           '<h1>信息来源： 发布日期: 2026-10-08 浏览次数: 123</h1></body></html>'),
     "关于自取及寄发2026年硕士研究生录取通知书的通知"),
    ("日期:日.月 年", lambda: fc.parse_date("09.30 机械工程学院举办2026年度奖教金颁奖典礼"), "2026-09-30T09:00:00+08:00"),
    ("日期:标准", lambda: fc.parse_date("2026-10-08 10:00"), "2026-10-08T09:00:00+08:00"),
    ("日期:未来活动日不能当发布日", lambda: fc.parse_date("2027年9月22日 比赛"), ""),
    ("日期:临近的活动日也不能当发布日", lambda: fc.parse_date(
        (importlib.import_module("datetime").date.today()
         + importlib.import_module("datetime").timedelta(days=10)).strftime("%Y年%m月%d日") + " 讲座"), ""),
    ("日期:太老不能当发布日", lambda: fc.parse_date("2012年3月1日"), ""),
    ("日期:年月", lambda: fc.parse_date("2024.03"), "2024-03-01T09:00:00+08:00"),
    ("活动时间:区间",
     lambda: fc.parse_event_range("活动时间：2026年10月12日 14:00-16:00", "2026"),
     ["2026-10-12T14:00:00+08:00", "2026-10-12T16:00:00+08:00"]),
    ("活动时间:只有开始",
     lambda: fc.parse_event_start("讲座时间：2026年10月20日 19:30", "2026"),
     "2026-10-20T19:30:00+08:00"),
    ("活动时间:不能抓发布时间",
     lambda: fc.parse_event_start("发布时间：2026-09-30 10:00", "2026"),
     ""),
    ("活动时间:月日+年提示",
     lambda: fc.parse_event_start("报告时间：10月20日 19:30", "2026"),
     "2026-10-20T19:30:00+08:00"),
    ("活动时间:带星期几的区间",
     lambda: fc.parse_event_range("2026年10月12日（周一）12:30-13:30", "2026"),
     ["2026-10-12T12:30:00+08:00", "2026-10-12T13:30:00+08:00"]),
    ("tm 反解:单日多天",
     lambda: fc.parse_event_from_tm("2026年10月23日—25日", "2026"),
     ["2026-10-23T09:00:00+08:00", None]),
    ("tm 反解:带星期的区间",
     lambda: fc.parse_event_from_tm("2026年10月12日（周一）12:30-13:30", "2026"),
     ["2026-10-12T12:30:00+08:00", "2026-10-12T13:30:00+08:00"]),
    ("tm 反解:跨天区间",
     lambda: fc.parse_event_from_tm("2025年11月4日-2025年11月18日(普通招考)", "2025"),
     ["2025-11-04T09:00:00+08:00", None]),
    ("tm 反解:年月不成日期就不认",
     lambda: fc.parse_event_from_tm("2026 年 6~7 月（具体时间另行通知）。", "2026"),
     [None, None]),
    ("分类:讲座", lambda: fc.classify("第41期学术报告会：Prof. X"), "讲座活动"),
    ("分类:招聘", lambda: fc.classify("2027届毕业生校园招聘双选会"), "实习就业"),
    ("分类:默认", lambda: fc.classify("我院召开党委扩大会议"), "院系资讯"),
    ("来源状态:有新增", lambda: fc.source_status(3), "ok"),
    ("来源状态:无新增", lambda: fc.source_status(0), "no_recent"),
    ("来源状态:抓取失败", lambda: fc.source_status(0, True), "failed"),
    ("来源状态:失败优先", lambda: fc.source_status(5, True), "failed"),
    ("解码:声明 utf-8 实际 GBK", lambda: fc.decode("text/html; charset=utf-8",
                                                    "浙江大学数学学院".encode("gb18030")), "浙江大学数学学院"),
    ("解码:按 meta 识别 GBK", lambda: fc.decode("", b'<meta charset="gbk">' + "数学学院".encode("gb18030")),
     '<meta charset="gbk">数学学院'),
    ("解码:正常 UTF-8 不受影响", lambda: fc.decode("text/html; charset=utf-8",
                                                    "浙江大学".encode("utf-8")), "浙江大学"),
    ("解码:未知编码名不报错", lambda: fc.decode("text/html; charset=x-unknown", b"abc"), "abc"),

    # ---- 正文抽取:导航块要扔掉,正文容器要认准 ----
    ("正文:认 wp_articlecontent 容器",
     lambda: fc.extract_body(
         '<html><body><div class="nav"><p>学工动态 学生科技 就业指导 志愿服务 团学风采 首页 概况</p></div>'
         '<div class="content_main"><div class="wp_articlecontent">'
         '<p>为进一步发挥育人作用，学院近日开展了创新创业项目模拟路演活动，邀请了多位企业导师参与指导。</p>'
         '</div></div></body></html>'),
     "为进一步发挥育人作用，学院近日开展了创新创业项目模拟路演活动，邀请了多位企业导师参与指导。"),
    ("正文:纯图片稿不给导航菜单",
     lambda: fc.extract_body(
         '<html><body><div class="nav"><ul><li>学工动态</li><li>学生科技</li><li>就业指导</li>'
         '<li>志愿服务</li><li>团学风采</li><li>首页</li><li>概况</li><li>师资队伍</li></ul></div>'
         '<div class="content_main"><div class="wp_articlecontent"><p><img src="/a.jpg" /></p></div></div>'
         '</body></html>'),
     ""),
    ("正文:面包屑要剥掉",
     lambda: fc.extract_body(
         '<div class="wp_articlecontent"><p>您的位置 : 首页 新闻动态（首页）</p>'
         '<p>近日，学院组织召开了本科生培养方案修订专题讨论会，各系教师代表参加并提出了具体建议。</p></div>'),
     "近日，学院组织召开了本科生培养方案修订专题讨论会，各系教师代表参加并提出了具体建议。"),
    ("正文:导航噪声判定", lambda: fc.looks_like_nav(
        "首页 院情总览 概况 大事记 组织机构 师资队伍 教师名录 科学研究 招生工作 就业工作 资料下载"),
     True),
    ("正文:正常文本不算导航", lambda: fc.looks_like_nav(
        "为进一步发挥育人作用，学院近日开展了创新创业项目模拟路演活动，邀请了多位企业导师参与指导。"),
     False),

    # ---- 发布日期:显式标签 / URL 路径兜底 ----
    ("发布日:页面里的发布日期标签",
     lambda: fc.publish_date(
         '<div class="cont_tit">发布日期 ：<span>2026-01-15</span>&nbsp;阅读次数 ：<span>10</span></div>',
         "http://www.isee.zju.edu.cn/2026/0115/c21313a3128502/page.htm"),
     "2026-01-15T09:00:00+08:00"),
    ("发布日:meta 兜底",
     lambda: fc.publish_date('<meta name="pubdate" content="2026-03-02" />', "http://x.cn/a.htm"),
     "2026-03-02T09:00:00+08:00"),
    ("发布日:URL 路径兜底",
     lambda: fc.publish_date("<html><body>没有日期</body></html>",
                             "http://www.isee.zju.edu.cn/2026/1008/c80651a3211988/page.htm"),
     "2026-10-08T09:00:00+08:00"),
    ("发布日:没有线索就留空",
     lambda: fc.publish_date("<html><body>什么都没有</body></html>", "http://x.cn/a.htm"),
     ""),
]

failed = 0
for name, fn, expect in cases:
    try:
        got = fn()
    except Exception as exc:  # noqa: BLE001
        got = "RAISED %s: %s" % (type(exc).__name__, exc)
    if name.endswith("截断"):
        ok = isinstance(got, str) and 0 < len(got) <= 60
    else:
        ok = got == expect
    failed += 0 if ok else 1
    print(("  OK   " if ok else "  FAIL ") + name + "  → " + repr(got)[:88])
print("\n通过 %d/%d" % (len(cases) - failed, len(cases)))
sys.exit(1 if failed else 0)
