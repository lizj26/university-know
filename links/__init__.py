#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公开链接抓取 —— 从公开可见的文章链接出发，把「一篇」扩成「一批」。

    albums/   抓的是**合集接口**（JSON，给你一个专题里的文章列表）
    links/    抓的是**文章页之间的链接网**（HTML，一篇里印着同一号的其它文章）

两者都是零凭据的公开面，但入口完全不同，所以分成两个包。本包不 import
`albums` / `fetch_wechat` / `fetch_campus` 的代码，只**读**它们产出的数据文件。

用法见 `python -m links --help` 与 links/README.md。
"""

__all__ = ["article", "sogou", "collect", "report"]
__version__ = "1.0.0"
