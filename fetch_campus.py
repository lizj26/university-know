#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「XX知道」多校区信息源抓取器 —— 学院官网 + 官方微信公众号。

与 refresh.py 的分工:
    refresh.py       走 pkuknow.cn 聚合接口(清华),并负责把 data/*.json 生成 index.html
    fetch_campus.py  直接从浙大/上交/西交的学院官网与官方公众号抓内容,产出 data/<校区>.json

用法示例
--------
    python fetch_campus.py                      # 抓三个校区,写入 data/ 并重新生成页面
    python fetch_campus.py --campus zju         # 只抓浙大
    python fetch_campus.py --max-per-source 10  # 每个来源最多取 10 条(默认 15)
    python fetch_campus.py --no-details         # 不抓正文,只取标题/时间/链接(快很多)
    python fetch_campus.py --no-import          # 只抓取并输出到 _work/,不写 data/
    python fetch_campus.py --import-only        # 不联网,把 _work/ 里上次的结果写入 data/ 并生成页面

设计要点
--------
* 只用标准库,与 refresh.py 保持一致(无需 pip install)。
* 只抓公开页面:每所学校的新闻网/通知公告列表 + 页面里出现的官方微信公众号文章。
* **可持续**:信息源是配置数据(SOURCES),每轮重爬列表页并按 id 合并进 data/,
  新发布的内容自动入库,不需要改代码;要临时加一个来源,写
  `_work/sources-<校区>.json` 即可(不必动这个文件);列表页候选还会按发布日期
  从新到旧排序后再按 max 截断,保证新条目一定被抓到。
* 公众号没有公开的"文章列表"接口,所以做法是:①跟着学校官网里链接的
  mp.weixin.qq.com 文章走(官网转载/引用的官微内容,可自动发现);②用 --wechat-file
  补一份手工维护的文章链接清单(一行一个)。两者都会把公众号名当来源名入库。
* 每个主机 1.2 秒一次请求,失败重试 2 次,失败不写坏数据(refresh.py 那一套写入保护照旧生效)。
"""

import argparse
import concurrent.futures as cf
import datetime
import gzip
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
# 信息源清单:一所学校一组。kind=list 抓列表页;kind=wechat 只摘页面里的公众号文章
# --------------------------------------------------------------------------
SOURCES = {
    "zju": [
        {"id": "zju_news", "name": "浙江大学求是新闻网", "group": "official",
         "url": "http://www.news.zju.edu.cn/", "kind": "list", "max": 18},
        {"id": "zju_news_yw", "name": "浙大求是新闻网·要闻", "group": "official",
         "url": "http://www.news.zju.edu.cn/24344/list.htm", "kind": "list", "max": 12},
        {"id": "zju_news_kxtt", "name": "浙大求是新闻网·科学头条", "group": "official",
         "url": "http://www.news.zju.edu.cn/kxtt/list.htm", "kind": "list", "max": 12},
        {"id": "zju_ckc", "name": "浙江大学竺可桢学院", "group": "official",
         "url": "http://ckc.zju.edu.cn/", "kind": "list", "max": 10},
        {"id": "zju_qsxy", "name": "浙江大学求是学院", "group": "official",
         "url": "http://qsxy.zju.edu.cn/", "kind": "list", "max": 10},
        {"id": "zju_lib", "name": "浙江大学图书馆", "group": "official",
         "url": "https://libweb.zju.edu.cn/", "kind": "list", "max": 10},
        {"id": "zju_cse", "name": "浙江大学控制科学与工程学院", "group": "official",
         "url": "http://www.cse.zju.edu.cn/", "kind": "list", "max": 10},
        {"id": "zju_som", "name": "浙江大学管理学院", "group": "official",
         "url": "http://www.som.zju.edu.cn/", "kind": "list", "max": 10},
        {"id": "zju_physics", "name": "浙江大学物理学院", "group": "official",
         "url": "https://physics.zju.edu.cn/", "kind": "list", "max": 10},
        {"id": "zju_zdzsc", "name": "浙江大学本科招生", "group": "official",
         "url": "https://zdzsc.zju.edu.cn/", "kind": "list", "max": 10},
        {"id": "zju_mse", "name": "浙江大学材料科学与工程学院", "group": "official",
         "url": "https://mse.zju.edu.cn/", "kind": "list", "max": 10},
        {"id": "zju_isee", "name": "浙江大学信息与电子工程学院", "group": "official",
         "url": "http://www.isee.zju.edu.cn/", "kind": "list", "max": 10},
        {"id": "zju_cs", "name": "浙江大学计算机科学与技术学院", "group": "official",
         "url": "http://www.cs.zju.edu.cn/csen/", "kind": "list", "max": 10},
        {"id": "zju_math", "name": "浙江大学数学科学学院", "group": "official",
         "url": "http://www.math.zju.edu.cn/", "kind": "list", "max": 10},
        {"id": "zju_portal", "name": "浙江大学主页", "group": "official",
         "url": "https://www.zju.edu.cn/", "kind": "wechat", "max": 12},
        # ---- 以下为各学院官网(URL 取自 zju.edu.cn/599 官方院系清单,2026-10 逐站验证) ----
        {"id": "zju_lit", "name": "浙江大学文学院", "group": "official",
         "url": "http://www.lit.zju.edu.cn/", "kind": "list", "max": 6},
        {"id": "zju_ls", "name": "浙江大学历史学院", "group": "official",
         "url": "https://ls.zju.edu.cn/main.htm", "kind": "list", "max": 6},
        {"id": "zju_philosophy", "name": "浙江大学哲学学院", "group": "official",
         "url": "http://www.philosophy.zju.edu.cn/", "kind": "list", "max": 6},
        {"id": "zju_cmic", "name": "浙江大学传媒与国际文化学院", "group": "official",
         "url": "http://www.cmic.zju.edu.cn/", "kind": "list", "max": 6},
        {"id": "zju_soaa", "name": "浙江大学艺术与考古学院", "group": "official",
         "url": "http://www.soaa.zju.edu.cn/", "kind": "list", "max": 6},
        {"id": "zju_cec", "name": "浙江大学经济学院", "group": "official",
         "url": "http://www.cec.zju.edu.cn/", "kind": "list", "max": 6},
        {"id": "zju_ghls", "name": "浙江大学光华法学院", "group": "official",
         "url": "http://www.ghls.zju.edu.cn/ghlscn/", "kind": "list", "max": 6},
        {"id": "zju_ced", "name": "浙江大学教育学院", "group": "official",
         "url": "http://www.ced.zju.edu.cn/", "kind": "list", "max": 6},
        {"id": "zju_spa", "name": "浙江大学公共管理学院", "group": "official",
         "url": "http://www.spa.zju.edu.cn/spachinese/", "kind": "list", "max": 6},
        {"id": "zju_chem", "name": "浙江大学化学系", "group": "official",
         "url": "http://www.chem.zju.edu.cn/chemcn/", "kind": "list", "max": 6},
        {"id": "zju_gs", "name": "浙江大学地球科学学院", "group": "official",
         "url": "http://gs.zju.edu.cn/", "kind": "list", "max": 6},
        {"id": "zju_psych", "name": "浙江大学心理与行为科学系", "group": "official",
         "url": "http://www.psych.zju.edu.cn", "kind": "list", "max": 6},
        {"id": "zju_me", "name": "浙江大学机械工程学院", "group": "official",
         "url": "http://me.zju.edu.cn/mecn/main.htm", "kind": "list", "max": 6},
        {"id": "zju_doe", "name": "浙江大学能源工程学院", "group": "official",
         "url": "http://www.doe.zju.edu.cn/main.htm", "kind": "list", "max": 6},
        {"id": "zju_oc", "name": "浙江大学海洋学院", "group": "official",
         "url": "http://oc.zju.edu.cn/", "kind": "list", "max": 6},
        {"id": "zju_saa", "name": "浙江大学航空航天学院", "group": "official",
         "url": "http://saa.zju.edu.cn/", "kind": "list", "max": 6},
        {"id": "zju_polymer", "name": "浙江大学高分子科学与工程学系", "group": "official",
         "url": "https://polymer.zju.edu.cn/main.htm", "kind": "list", "max": 6},
        {"id": "zju_opt", "name": "浙江大学光电科学与工程学院", "group": "official",
         "url": "http://opt.zju.edu.cn/", "kind": "list", "max": 6},
        {"id": "zju_icsr", "name": "浙江大学网络空间安全学院", "group": "official",
         "url": "https://icsr.zju.edu.cn/", "kind": "list", "max": 6},
        {"id": "zju_cst", "name": "浙江大学软件学院", "group": "official",
         "url": "http://www.cst.zju.edu.cn/", "kind": "list", "max": 6},
        {"id": "zju_cbeis", "name": "浙江大学生物医学工程与仪器科学学院", "group": "official",
         "url": "http://www.cbeis.zju.edu.cn/", "kind": "list", "max": 6},
        {"id": "zju_ic", "name": "浙江大学集成电路学院", "group": "official",
         "url": "https://ic.zju.edu.cn/", "kind": "list", "max": 6},
        {"id": "zju_cls", "name": "浙江大学生命科学学院", "group": "official",
         "url": "http://www.cls.zju.edu.cn/clscn/", "kind": "list", "max": 6},
        {"id": "zju_caefs", "name": "浙江大学生物系统工程与食品科学学院", "group": "official",
         "url": "http://www.caefs.zju.edu.cn/", "kind": "list", "max": 6},
        {"id": "zju_cers", "name": "浙江大学环境与资源学院", "group": "official",
         "url": "http://www.cers.zju.edu.cn/cercn/", "kind": "list", "max": 6},
        {"id": "zju_cab", "name": "浙江大学农业与生物技术学院", "group": "official",
         "url": "http://www.cab.zju.edu.cn/chinese/", "kind": "list", "max": 6},
        {"id": "zju_cmm", "name": "浙江大学医学院", "group": "official",
         "url": "http://www.cmm.zju.edu.cn/", "kind": "list", "max": 6},
        {"id": "zju_phs", "name": "浙江大学公共卫生学院", "group": "official",
         "url": "http://www.phs.zju.edu.cn/", "kind": "list", "max": 6},
        {"id": "zju_cps", "name": "浙江大学药学院", "group": "official",
         "url": "http://www.cps.zju.edu.cn/", "kind": "list", "max": 6},
    ],
    "sjtu": [
        {"id": "sjtu_tg", "name": "上海交通大学通知公告", "group": "official",
         "url": "https://www.sjtu.edu.cn/tg/", "kind": "list", "max": 15},
        {"id": "sjtu_jdyw", "name": "上海交大新闻网·交大要闻", "group": "official",
         "url": "https://news.sjtu.edu.cn/jdyw/", "kind": "list", "max": 12},
        {"id": "sjtu_zhxw", "name": "上海交大新闻网·综合新闻", "group": "official",
         "url": "https://news.sjtu.edu.cn/zhxw/", "kind": "list", "max": 12},
        {"id": "sjtu_seiee", "name": "上海交大电子信息与电气工程学院", "group": "official",
         "url": "https://www.seiee.sjtu.edu.cn/xwdt.html", "kind": "list", "max": 12},
        {"id": "sjtu_me", "name": "上海交大机械与动力工程学院", "group": "official",
         "url": "https://me.sjtu.edu.cn/tzgg.html", "kind": "list", "max": 12},
        {"id": "sjtu_acem", "name": "上海交大安泰经济与管理学院", "group": "official",
         "url": "https://acem.sjtu.edu.cn/news_center/news.html", "kind": "list", "max": 12},
        {"id": "sjtu_cs", "name": "上海交大计算机学院", "group": "official",
         "url": "https://www.cs.sjtu.edu.cn/", "kind": "list", "max": 10},
        {"id": "sjtu_scce", "name": "上海交大化学化工学院", "group": "official",
         "url": "https://scce.sjtu.edu.cn/", "kind": "list", "max": 10},
        {"id": "sjtu_jwc", "name": "上海交大教务处", "group": "official",
         "url": "https://jwc.sjtu.edu.cn/", "kind": "list", "max": 10},
        {"id": "sjtu_smse", "name": "上海交大材料科学与工程学院", "group": "official",
         "url": "https://smse.sjtu.edu.cn/", "kind": "list", "max": 8},
        {"id": "sjtu_pharm", "name": "上海交大药学院", "group": "official",
         "url": "https://pharm.sjtu.edu.cn/", "kind": "list", "max": 8},
        {"id": "sjtu_sfl", "name": "上海交大外国语学院", "group": "official",
         "url": "https://sfl.sjtu.edu.cn/", "kind": "list", "max": 8},
        {"id": "sjtu_shss", "name": "上海交大人文学院", "group": "official",
         "url": "https://shss.sjtu.edu.cn/", "kind": "list", "max": 8},
        # ---- 以下学院官网来自 sjtu.edu.cn/yxsz 官方院系页,2026-10 逐站验证 ----
        {"id": "sjtu_naoce", "name": "上海交大船舶海洋与建筑工程学院", "group": "official",
         "url": "http://naoce.sjtu.edu.cn/", "kind": "list", "max": 6},
        {"id": "sjtu_see", "name": "上海交大电气工程学院", "group": "official",
         "url": "https://see.sjtu.edu.cn/", "kind": "list", "max": 6},
        {"id": "sjtu_sais", "name": "上海交大自动化与感知学院", "group": "official",
         "url": "https://sais.sjtu.edu.cn/", "kind": "list", "max": 6},
        {"id": "sjtu_icisee", "name": "上海交大集成电路学院", "group": "official",
         "url": "https://icisee.sjtu.edu.cn/", "kind": "list", "max": 6},
        {"id": "sjtu_physics", "name": "上海交大物理与天文学院", "group": "official",
         "url": "http://www.physics.sjtu.edu.cn/", "kind": "list", "max": 6},
        {"id": "sjtu_soo", "name": "上海交大海洋学院", "group": "official",
         "url": "http://soo.sjtu.edu.cn/", "kind": "list", "max": 6},
        {"id": "sjtu_law", "name": "上海交大凯原法学院", "group": "official",
         "url": "http://law.sjtu.edu.cn/", "kind": "list", "max": 6},
        {"id": "sjtu_ma", "name": "上海交大马克思主义学院", "group": "official",
         "url": "http://ma.sjtu.edu.cn/", "kind": "list", "max": 6},
        {"id": "sjtu_senergy", "name": "上海交大智慧能源创新学院", "group": "official",
         "url": "http://www.senergy.sjtu.edu.cn/", "kind": "list", "max": 6},
        {"id": "sjtu_psych", "name": "上海交大心理学院", "group": "official",
         "url": "https://psychology.sjtu.edu.cn/", "kind": "list", "max": 6},
        {"id": "sjtu_zhiyuan", "name": "上海交大致远学院", "group": "official",
         "url": "https://zhiyuan.sjtu.edu.cn/hongshan/", "kind": "list", "max": 6},
        {"id": "sjtu_soe", "name": "上海交大教育学院", "group": "official",
         "url": "https://soe.sjtu.edu.cn/Web/NewsThree/40", "kind": "list", "max": 6},
        {"id": "sjtu_bme", "name": "上海交大生物医学工程学院", "group": "official",
         "url": "https://bme.sjtu.edu.cn/Web/List/299", "kind": "list", "max": 6},
    ],
    "xjtu": [
        {"id": "xjtu_dice_tzgg", "name": "西安交大信息与通信工程学院·通知公告", "group": "official",
         "url": "https://dice.xjtu.edu.cn/xwgg/tzgg1.htm", "kind": "list", "max": 15},
        {"id": "xjtu_dice_xsjz", "name": "西安交大信息与通信工程学院·学术讲座", "group": "official",
         "url": "https://dice.xjtu.edu.cn/xwgg/xsjz1.htm", "kind": "list", "max": 12},
        {"id": "xjtu_ee", "name": "西安交大电气工程学院·通知公告", "group": "official",
         "url": "https://ee.xjtu.edu.cn/jzxx.htm", "kind": "list", "max": 15},
        {"id": "xjtu_mec", "name": "西安交大机械工程学院", "group": "official",
         "url": "https://mec.xjtu.edu.cn/", "kind": "list", "max": 15},
        {"id": "xjtu_news", "name": "西安交大新闻网·主页新闻", "group": "official",
         "url": "https://news.xjtu.edu.cn/zyxw.htm", "kind": "list", "max": 15},
        {"id": "xjtu_portal", "name": "西安交通大学主页", "group": "official",
         "url": "https://www.xjtu.edu.cn/", "kind": "wechat", "max": 10},
        {"id": "xjtu_math", "name": "西安交大数学与统计学院", "group": "official",
         "url": "https://math.xjtu.edu.cn/", "kind": "list", "max": 10},
        {"id": "xjtu_som", "name": "西安交大管理学院", "group": "official",
         "url": "https://som.xjtu.edu.cn/", "kind": "list", "max": 10},
        {"id": "xjtu_mse", "name": "西安交大材料科学与工程学院", "group": "official",
         "url": "https://mse.xjtu.edu.cn/", "kind": "list", "max": 10},
        {"id": "xjtu_clet", "name": "西安交大化学工程与技术学院", "group": "official",
         "url": "https://clet.xjtu.edu.cn/", "kind": "list", "max": 10},
        {"id": "xjtu_epe", "name": "西安交大能源与动力工程学院", "group": "official",
         "url": "https://epe.xjtu.edu.cn/", "kind": "list", "max": 12},
        {"id": "xjtu_rwxy", "name": "西安交大人文社会科学学院", "group": "official",
         "url": "https://rwxy.xjtu.edu.cn/", "kind": "list", "max": 10},
        {"id": "xjtu_jwc", "name": "西安交大教务处", "group": "official",
         "url": "https://jwc.xjtu.edu.cn/", "kind": "list", "max": 10},
        {"id": "xjtu_gs", "name": "西安交大研究生院", "group": "official",
         "url": "https://gs.xjtu.edu.cn/", "kind": "list", "max": 10},
        # ---- 以下学院官网 2026-10 逐站验证(域名来自各学院官方页面互链) ----
        {"id": "xjtu_cs", "name": "西安交大计算机科学与技术学院", "group": "official",
         "url": "http://www.cs.xjtu.edu.cn/", "kind": "list", "max": 6},
        {"id": "xjtu_se", "name": "西安交大软件学院", "group": "official",
         "url": "http://se.xjtu.edu.cn/", "kind": "list", "max": 6},
        {"id": "xjtu_esteie", "name": "西安交大电子科学与工程学院", "group": "official",
         "url": "http://esteie.xjtu.edu.cn/", "kind": "list", "max": 6},
        {"id": "xjtu_automation", "name": "西安交大自动化科学与工程学院", "group": "official",
         "url": "http://automation.xjtu.edu.cn/", "kind": "list", "max": 6},
        {"id": "xjtu_ele", "name": "西安交大微电子学院", "group": "official",
         "url": "http://ele.xjtu.edu.cn/", "kind": "list", "max": 6},
        {"id": "xjtu_cybersec", "name": "西安交大网络空间安全学院", "group": "official",
         "url": "http://cybersec.xjtu.edu.cn/", "kind": "list", "max": 6},
        {"id": "xjtu_sef", "name": "西安交大经济与金融学院", "group": "official",
         "url": "https://sef.xjtu.edu.cn/", "kind": "list", "max": 6},
        {"id": "xjtu_law", "name": "西安交大法学院", "group": "official",
         "url": "https://fxy.xjtu.edu.cn/", "kind": "list", "max": 6},
        {"id": "xjtu_phys", "name": "西安交大物理学院", "group": "official",
         "url": "https://phys.xjtu.edu.cn/", "kind": "list", "max": 6},
        {"id": "xjtu_chem", "name": "西安交大化学学院", "group": "official",
         "url": "https://chem.xjtu.edu.cn/", "kind": "list", "max": 6},
        {"id": "xjtu_sfs", "name": "西安交大外国语学院", "group": "official",
         "url": "https://sfs.xjtu.edu.cn/", "kind": "list", "max": 6},
    ],
}

# 只要本校自己的公众号(含院系/学部/校友会号);校媒转载的人民日报、科普中国之类不收录
WECHAT_ACCOUNT_FILTER = {
    "zju": r"浙江大学|浙大",
    "sjtu": r"上海交大|上海交通大学|交大",
    "xjtu": r"西安交大|西安交通大学|交大",
}

# 已登记的官方公众号名片。公众号没有文章列表接口,所以这份名单的作用是:
#   ① 让名字里不含「浙大 / 浙江大学」的号(如 ZJUPES、竺院团学汇)也能通过账号过滤;
#   ② 在页面的「信息来源」里显示「已登记、暂无内容」,方便知道还缺哪些号。
WECHAT_ACCOUNTS = {
    "zju": [
        "浙大灵韵音乐剧社", "竺院团学汇", "ZJU星弦幻协", "ZJUPES", "浙里等宁",
        "浙大竺院人", "浙大魔协", "浙大体育与艺术", "浙江大学本科招生",
        "浙江大学CC98论坛", "浙江大学物理学院", "浙江大学学生会",
        "浙江大学校友总会", "浙浙小喇叭",
    ],
    "sjtu": [],
    "xjtu": [],
}

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


def fetch(url, delay=1.2, retries=None, timeout=25):
    """抓一个页面。http:// 的地址会先试同名 https —— 不少学校站点已上 https,
    而明文 http 可能被网关/代理拦成 403(2026-10 实测:多个学院站 http 403、https 正常)。

    retries=None 时用全局 RETRY_COUNT(可由 --retries 调整):失败源在被封的网络环境里
    每次重试都要等退避,云端可调小以压短单轮耗时。
    """
    if retries is None:
        retries = RETRY_COUNT
    targets = [url]
    if url.startswith("http://"):
        targets = [url.replace("http://", "https://", 1), url]
    last = None
    for target in targets:
        for attempt in range(retries + 1):
            polite_wait(target, delay)
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
        href = urllib.parse.urljoin(base, m.group(1).strip())
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
SITE_NAME_ONLY = re.compile(r"^[\u4e00-\u9fa5A-Za-z]{2,14}(大学|学院|学部|新闻网|中心)$")


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
    """标题优先级:h1 → <title> → 列表页锚文本;都不合适就截断锚文本。"""
    h1 = ""
    m = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", detail_html or "")
    if m:
        h1 = re.sub(r"\s+", " ", strip_tags(m.group(1))).strip()
    raw = [h1, page_title(detail_html or ""), anchor or ""]
    candidates = [strip_site_prefix(strip_site_suffix(re.sub(r"\s+", " ", c).strip())) for c in raw]
    for cand in candidates:
        if not cand or SITE_NAME_ONLY.match(cand):
            continue
        if 6 <= len(cand) <= 80:
            return cand
    for cand in candidates:
        if cand and not SITE_NAME_ONLY.match(cand):
            return cand[:60]
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


def crawl_campus(campus, args):
    sources = merge_sources(campus)
    if not sources:
        print(f"✗ 没有为 {campus} 配置信息源")
        return []
    items, seen, seen_titles = [], set(), set()
    stats = {}
    known = previous_ids(campus)
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
        limit = args.max_per_source or src.get("max") or 15
        log.append(f"\n· [{campus}] {src['name']}  ({src['url']})")
        out = {"src": src, "items": [], "rows": [], "picked": 0, "log": log}
        try:
            html, final = fetch(src["url"], delay=args.delay)
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
        found, seen_here = [], set()
        for href, text, pos in anchors(html, final):
            if not is_article_link(href, base_host):
                continue
            if href in seen_here:
                continue
            if len(re.sub(r"\s", "", text)) < 6:
                continue
            window = strip_tags(html[max(0, pos - 300):pos + 300]).replace("\n", " ")
            seen_here.add(href)
            found.append((href, text, window))

        wx_links = []
        for href in WX_RE.findall(html):
            href = href.split("&scene=")[0].split("#")[0]
            if href not in wx_links:
                wx_links.append(href)

        if src.get("kind") == "wechat":
            found = []

        picked = 0
        picked_ids = []
        local_titles = set()      # 来源内去重(跨来源去重交给主线程按顺序做)
        # 列表页顺序不一定按时间倒序(有的站栏目混排),先按窗口里的发布日期从新到旧
        # 排一遍再截断,保证「刚发布的条目」一定落在 limit 之内,不会被旧稿挤掉。
        dated = []
        for idx, (href, text, window) in enumerate(found):
            dated.append((parse_date(window) or "", -idx, href, text, window))
        dated.sort(reverse=True)      # 有日期的排前面且新→旧,同日按页面原有顺序
        for published_hint, _, href, text, window in dated:
            if picked >= limit:
                break
            if title_key(text) in local_titles:
                continue
            published = published_hint
            body = ""
            if not args.no_details:
                try:
                    detail_html, _ = fetch(href, delay=args.delay)
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
    for res in results:
        for line in res["log"]:
            print(line)
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

    # 手工维护的公众号文章清单:--wechat-file 指定的,加上 _work/wechat/wechat-<校区>.txt
    # (旧布局放在 _work/wechat-<校区>.txt,这里也回退读一次)
    manual_files = []
    if args.wechat_file:
        manual_files.append(args.wechat_file)
    for auto_file in (os.path.join(WORK_DIR, "wechat", f"wechat-{campus}.txt"),
                      os.path.join(WORK_DIR, f"wechat-{campus}.txt")):
        if os.path.exists(auto_file) and auto_file not in manual_files:
            manual_files.append(auto_file)
            break

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

    save_registry(campus, registry)
    items.sort(key=lambda x: (x.get("d") or "", x["i"]), reverse=True)
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
    ap = argparse.ArgumentParser(description="抓取浙大/上交/西交的学院官网与官方公众号")
    ap.add_argument("--campus", default="zju,sjtu,xjtu", help="校区，逗号分隔")
    ap.add_argument("--max-per-source", type=int, default=15, help="每个来源最多取多少条")
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
    ap.add_argument("--no-details", action="store_true", help="不抓正文,只取标题/时间/链接")
    ap.add_argument("--no-import", action="store_true", help="只抓取并输出到 _work/,不写 data/")
    ap.add_argument("--import-only", action="store_true", help="不联网,把 _work/ 上次的结果写入 data/")
    ap.add_argument("--wechat-file", help="额外的公众号文章链接清单(一行一个,含 # 的行忽略)")
    args = ap.parse_args()

    global RETRY_COUNT, DOMAIN_DELAY
    if args.retries is not None and args.retries >= 0:
        RETRY_COUNT = int(args.retries)
    if args.domain_delay and args.domain_delay > 0:
        DOMAIN_DELAY = float(args.domain_delay)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    campuses = [c.strip() for c in args.campus.split(",") if c.strip()]
    written = []
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
            items = crawl_campus(campus, args)
            if not items:
                print(f"✗ [{campus}] 一条都没抓到，保留现有数据")
                continue
            path = write_items(campus, items)
        written.append((campus, path))

    if args.no_import or not written:
        print("\n(未写入 data/：--no-import)")
        return

    for campus, path in written:
        do_import(campus, path)

    import importlib.util
    spec = importlib.util.spec_from_file_location("refresh", os.path.join(HERE, "refresh.py"))
    refresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(refresh)
    refresh.build()


if __name__ == "__main__":
    main()
