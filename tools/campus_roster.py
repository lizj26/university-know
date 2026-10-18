#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校区登记表 —— 全站唯一的「有哪些学校」真相来源。

每行：(key, 中文全名, 页面显示名, 英文名, 上游接口前缀, 外链地址)
* key          目录与 data/<key>.json 的文件名，也是 --campus 的值
* 页面显示名   顶栏切换 / 来源分组标题上的名字，约定「简称 + 知道」
* 上游前缀     refresh.py 走 pkuknow.cn 聚合接口时的路径；None = 该校区由
               fetch_campus.py 直接抓官网（不依赖上游）
* 外链地址     非空时，该校区在顶栏只作为「跳转到外部站点」的入口：本站不抓取、
              不内嵌它的内容，点击直接在新标签打开该地址。

当前收录 4 栏：
* thu  清华知道 —— 外链到 https://pkuknow.cn/thu（本站不再复制清华内容）
* zju  浙大知道 / sjtu 上交知道 / xjtu 西交知道 —— 由 fetch_campus.py 直接抓官网

口径：只收「公开官网新闻」的普通高校，涉国防/军事、有保密要求的院校**一律不收录**
（北大、人大亦按用户要求不收）。曾批量接入的其余 985 高校已于本轮清理，
原数据归档在 `_bak_1014/`。

改完这个文件后跑 `python tools/register_campus.py`，会自动把校区写进
refresh.py / template.html / cloud_update.py / .github/workflows/update.yml /
tools/refocus_data.py，不必手工同步五处。
"""

CAMPUSES = [
    # key     中文全名          页面显示名    英文名        上游前缀  外链地址
    ("thu",  "清华大学",       "清华知道",   "THU Know",   None,   "https://pkuknow.cn/thu"),
    ("zju",  "浙江大学",       "浙大知道",   "ZJU Know",   None,   None),
    ("sjtu", "上海交通大学",   "上交知道",   "SJTU Know",  None,   None),
    ("xjtu", "西安交通大学",   "西交知道",   "XJTU Know",  None,   None),
]


def by_key():
    return {row[0]: row for row in CAMPUSES}
