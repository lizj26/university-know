#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文章分类规则。

**这里刻意和 fetch_campus.py / fetch_wechat.py 的规则表各存一份，而不是 import**：
本包的设计前提是「删掉另外两个脚本也能单独运行」，为此宁可复制 12 行正则。

代价是可能悄悄漂移，所以 `tests/test_albums.py` 里有一条一致性测试：
用 ast 从 fetch_campus.py 里把 CATEGORY_RULES 读出来比对，不一致就直接失败。
→ 漂移会变成一条红灯，而不是页面上一批文章莫名归类错误。
"""

from __future__ import annotations

import re

# 顺序有意义：先命中先归类（越具体的越靠前）
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

ACTIVITY_CATEGORIES = {"文体活动", "社团公益", "讲座活动", "学习成长", "实习就业", "交流访学"}
DEFAULT_CATEGORY = "校园动态"


def classify(text: str) -> str:
    for name, pattern in CATEGORY_RULES:
        if re.search(pattern, text or "", re.I):
            return name
    return DEFAULT_CATEGORY


def intent_of(category: str) -> str:
    return "activity" if category in ACTIVITY_CATEGORIES else "information"
