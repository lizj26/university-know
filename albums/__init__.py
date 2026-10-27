#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公开合集（appmsgalbum）抓取 —— 独立模块。

这条链路与项目里另外两条**完全不共享代码**：

    albums/          ← 本包：公开合集列表，零凭据、零第三方库、不受限流影响
    fetch_wechat.py  ← 复杂的公众号正文/历史列表抓取（cookies、号池、搜狗、getmsg）
    fetch_campus.py  ← 稳定的官网抓取
    refresh.py       ← 只负责把 data/*.json 渲染成 index.html

本包**不 import 上面任何一个文件**，也不写 `data/`。
产物全部落在 `_work/albums/`，由下游按需（显式）取用。

用法见 `python -m albums --help` 与 albums/README.md。
"""

__all__ = ["client", "store", "collect"]
__version__ = "1.0.0"
