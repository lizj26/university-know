#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""各高校信息源（由 tools/discover_sources.py 自动发现 + 人工复核）。

fetch_campus.py 在导入时会把它合并进 SOURCES；同 id 覆盖内置配置。
口径：只收「官网 + 各校职能部门」（新闻网/教务处/研究生院/招生/就业/图书馆/
学工/国际交流…），不收学院站，也不含公众号。

目前留空：当前收录的浙大/上交/西交三校来源都手工精调在 fetch_campus.py 的
SOURCES 里。此前批量接入的其余 985 高校（含同济、港大）已于本轮清理移除，
其配置与数据归档在 `_bak_1014/`。若以后再用 `tools/discover_sources.py` 批量
探测新校区，脚本会把结果自动追加到这里。
"""

SOURCES_EXTRA = {}
