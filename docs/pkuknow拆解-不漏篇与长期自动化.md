# 「清华知道」(pkuknow.cn/thu) 拆解：它凭什么做到"不漏一篇 + 长期自动"

> 观察时间：**2026-10-24 15:50–16:00**（本机实测）。
> 观察方式：只读公开页面 + 站点自己发给访客的 guest cookie 接口；未绕过鉴权、未高频请求。
> 站点在「API / RSS」面板里写明：**"接入范围、调用额度、服务周期和价格请与开发者确认"**
> —— 对外是付费服务，不是开源项目。本文只做架构还原，不搬运其数据。

---

## 0. 结论（先看这段）

1. **它是自建管道，不是某个开源方案的成品。** 前端 Next.js，后面是"采集 → 正文 → AI 加工 → 服务"四层，每层都能在公开数据里看到痕迹。
2. **"不漏一篇"不是靠更强的爬虫，而是靠三个工程机制**：第一页增量轮询（10 条缓冲）、去重账本、缺口补齐。这三件事**我们完全能抄**。
3. **"长期自动"靠的是一个不需要人肉重扫的微信数据通道。** 145 个号、分钟级同步 ≈ 每秒 0.5 次请求；这个量级用单个 `wap_sid2` 必被 `200013`（我们 10-10 实测 3 个号就触发）。作者原话也说得很直白：**"抓取微信的数据需要消耗挺多资金"**。
4. 所以真正的护城河是**通道**，不是代码。我们能复刻它的管道，复刻不了它的通道——除非付费，或自己维护号池/设备。

---

## 1. 站点画像（实测数字）

| 项 | 实测值 | 怎么得到的 |
|---|---|---|
| 技术栈 | Next.js App Router（RSC flight 数据）+ nginx + Caddy | 响应头 `Vary: rsc,...`、`Via: 2.0 Caddy`、`self.__next_f.push` |
| 校区 | `pku` / `thu` / `ruc` 三套，路径前缀即 basePath | `/api/stats` 返回 `campuses` |
| 来源 | **195 个**：微信 **145** + 官网 **50** | 列表接口 `sources` 数组 |
| 条目量 | **3175 条**，时间跨度约 **120 天**（第 100 页已到 2026-06-27） | 分页抽样 |
| 分页 | `page_size=30`，`view=list&page=N` | 接口回显 |
| 同步 | `last_sync` 每 **约 62 秒**推进一次；字段 `interval_minutes=5` | 连点 3 次，时间戳 15:53:19 → 15:54:21 → 15:55:23 |
| 质量账本 | `translation_stats`: complete 3374 / total 3381 / **pending 7 / failed 0** | 列表接口 |
| 来源健康 | `status`: **ok 179 / no_recent 16** | `sources` 数组 |
| 分类 | 13 个类目；意图 2 类（activity 1709 / information 1466） | `category_totals` / `intent_totals` |
| 微信条目形态 | 238 条样本里 `idx` 分布 **1..8**；**100%** 带 `__biz/mid/idx/sn/chksm` | 抽样 8 页 |
| 详情完备度 | 10 条抽样：9 条全文、1 条 `metadata_only`；正文中位 **4458 字**；图片 0–27 张 | `/api/notices/{id}` |
| AI 加工 | `ai_summary` 10/10、`subjects` 学科标签、`english.title` 双语、活动要素抽取 | 详情字段 |
| 媒体 | 全部转 webp 本地托管，`/thu/media/<sha256>.webp` | 列表 `cover_image` |
| 对外服务 | 公开 JSON API（访客 cookie）+ RSS 专属链接 + PWA + iOS TestFlight | 页面按钮与文案 |

---

## 2. 它到底存了什么（数据字典）

### 2.1 列表 `GET /thu/api/notices?view=list&page=N`（需 `__Host-pku_read_guest` cookie，站点自动下发，24h）

```json
{
  "items": [{
    "id": "wx_9cdf6f91…",              // wx_ + md5，跨源统一 ID
    "source_id": "wa_92b4f5ce…",        // 来源 ID（wa_ = wechat account）
    "source_name": "清华大学建筑学院",
    "title": "预告 I “栋梁之承——清华建筑学科八十周年发展大会”即将召开",
    "url": "https://mp.weixin.qq.com/s?__biz=…&mid=…&idx=1&sn=…&chksm=…#rd",
    "published_at": "2026-10-24T15:00:00+08:00",
    "collected_on": "2026-10-24",
    "category": "讲座活动",
    "intent_group": "activity",
    "cover_image": {"url": "/thu/media/<sha256>.webp", "thumbnail_url": "…", "width": 960, "height": 409},
    "english": {"title": "…"}
  }],
  "total": 3175, "page": 1, "page_size": 30,
  "sources": [{"id": "wa_…", "name": "清华大学数学科学系", "count": 7, "status": "ok", "source_group": "wechat", "source_section": "school"}],
  "groups": [{"id": "wechat", "sources": 145, "count": 2703}, {"id": "official", "sources": 50, "count": 483}],
  "category_totals": [...], "intent_totals": [...],
  "last_sync": "2026-10-24T07:52:15Z", "last_attempt": "…", "interval_minutes": 5
}
```

### 2.2 详情 `GET /thu/api/notices/{id}`

| 类别 | 字段 | 说明 |
|---|---|---|
| 原始 | `title` / `url` / `published_at` / `source_*` / `channel` | `channel=wechat`，原文链接保留 `chksm` |
| 正文 | `document_markdown` / `reading_images` / `reading_layout` / `attachments` | 正文转 MD，图片全部下载重托管，保留排版块 |
| AI 结构 | `ai_summary` / `ai_is_event` / `ai_event_time` / `ai_event_location` / `event_start` / `event_end` / `time_text` / `location` / `organizer` / `deadline` / `subjects` / `category` / `intent_group` | 事件类信息被抽成结构化字段，直接驱动「活动日历」 |
| 双语 | `english.title` / `english.source_name` | 机翻 + 校对 |
| 质量 | `detail_status`(complete/metadata_only) / `content_kind`(full/summary) / `access_scope`(public) | **不完整的也保留并标记**，不静默丢弃 |

> 一个细节很说明问题：列表最后一页有 **25 条 `published_at=null`** 的条目。
> 他们的选择是"**保留 + 标空**"，而不是扔掉。这就是账本思维：**宁可留残缺，也不丢记录**。

---

## 3. 架构还原（五层）

```text
① 采集层   145 微信 + 50 官网，约 1 分钟一次 tick（interval_minutes=5 为单源 TTL）
           每个源有 status：ok / no_recent；每次运行产出 complete/pending/failed 账本
              ↓  只拿"列表"（标题/链接/时间/idx）
② 正文层   打开原文 → 清洗 → document_markdown + 图片下载转 webp（sha256 命名）+ 附件
           失败则标 metadata_only，进 pending 队列，之后重试（实测 pending=7）
              ↓
③ 理解层   LLM：摘要、13 类分类、活动/信息意图、学科标签、活动要素抽取（时间/地点/主办/截止）
           中英标题翻译（translation_stats 单独计账，complete 3374 / total 3381）
              ↓
④ 存储层   统一 ID（wx_/wa_ + md5）、去重键、按校区/来源/分类建索引
              ↓
⑤ 服务层   Next.js RSC + 访客只读会话 + 公开 API（配额制付费）+ RSS 专属链接 + PWA + iOS App
```

每一层都有可验证的痕迹：`document_markdown`（②）、`ai_summary`（③）、`groups/sources`（①）、
`detail_status`（质量门）、`/api/stats`（⑤ 的访问统计）。

---

## 4. "不漏一篇"的四个机制（**这四条可以直接抄**）

| 机制 | 它怎么做的 | 为什么有效 | 我们的对应实现 |
|---|---|---|---|
| **第一页增量轮询** | 高频只拉第 1 页（最新 10 次群发），历史靠回填 | 轮询周期 T 远小于"10 次群发所需时间"时，**漏掉的轮次会被下一页自然补上** | 新增 `history --watch`（每轮 1 次请求） |
| **去重账本** | `id = wx_+md5`，入库幂等 | 重复拉同一页不产生重复条目 | `history-seen-<biz>.json` + `diff_new()` |
| **缺口补齐** | `pending / failed / metadata_only` 三类状态，失败进队列重试 | 抓取失败不等于放弃；下一轮把它捞回来 | 待补：把 `history` 结果落库并标状态 |
| **冷启动回填** | 接入新号时把 120 天历史翻完（100+ 页） | 轮询只保证"新"，回填保证"全" | `history --since` 已具备 |

**频率安全线**（这是他们和我们最大的差别，见 §5）：

| 轮询周期 | 40 个号的日请求量 | 风险判断 |
|---|---|---|
| 5 分钟 | 11,520 | 单凭据必死（≈8 次/分钟） |
| 30 分钟 | 1,920 | 偏激进，只适合多凭据轮换 |
| **2 小时** | **480**（20 次/小时） | **单凭据可承受，推荐起点** |
| 6 小时 | 160 | 最保守；靠 10 条缓冲仍不漏常见号 |

> 关键算式：**漏不漏取决于"10 次群发的平均耗时"是否大于轮询周期**。
> 院系号通常 1–3 天一发，2 小时轮询有几十倍余量；清华官方号一天 2–3 推，也够。

---

## 5. "长期自动化"靠什么：通道，不是代码

三条硬证据：

1. **量级**：145 个号 × 约 1 分钟 tick ≈ 每秒 0.5 次、每天 4 万+ 次列表请求。
   我们的实测：单个 `wap_sid2` 在 10-10 那次，**3 个号就撞 `200013`**（详见 10.13）。
2. **作者原话**："抓取微信的数据需要消耗挺多资金的"（站点赞助文案，`1722…js` 字符串表）。
3. **成品形态**：对外 API/RSS 明码"联系开发者确认额度与价格"——说明其上游本身是有成本的资源。

外部能确定的只有"它有一条稳定、可分钟级拉全量的通道"；**具体是哪一条，公开信息无法确证**。
三种可能，按概率排：

| 假设 | 特征是否吻合 | 判据（若想进一步验证） |
|---|---|---|
| **付费第三方公众号数据 API**（新榜/清博/极致了等） | 吻合：分钟级、全量、有成本；无需设备 | 数据里通常带阅读量/点赞；他们没有 → 可能被剥离，也可能选的是不返回阅读量的供应商 |
| **自建协议号池**（iPad/Mac 协议、Wechaty puppet、wechat2rss 类商业服务） | 吻合：全量、稳定、按号收费；"自己做的工具" | 延迟特征（协议通道通常 1–5 分钟）、是否包含 `idx` 2–8（他们**有**，说明拿的是群发级列表） |
| 设备农场（真机/模拟器 + 客户端自动化） | 部分吻合：全量，但 145 号 × 分钟级难以扩展 | 会出现明显的时间抖动与整点批量 |

我们能确认的是**数据形态**：238 条微信样本 **100% 带 `chksm`**、`idx` 覆盖 **1–8** ——
这只可能来自"**群发级列表**"（`getmsg` / `appmsgpublish` 那一类），而不是搜狗/合集/主页。
换句话说：**它拿到的正是我们 `history` 想要的那种数据，只是通道比我们稳。**

---

## 6. 我们照抄的差距表

| 能力 | 清华知道 | 我们（现状） | 差距性质 |
|---|---|---|---|
| 微信列表（全量、分钟级） | ✅ 145 号 | ⚠️ `history` 需抓包，单凭据 | **通道**（钱/号池） |
| 第一页增量 + 去重 | ✅ | ✅ 本轮新增 `--watch` + seen 账本 | 已补齐 |
| 冷启动回填 | ✅ 120 天 | ✅ `--since` | 无 |
| 失败/缺口账本 | ✅ pending/failed/metadata_only | ❌ | 工程（可做） |
| 来源健康度 | ✅ ok/no_recent | ❌ | 工程（可做） |
| 正文 + 图片本地化 | ✅ MD + webp | ✅ 正文 MD / ❌ 图片 | 工程（可做） |
| AI 摘要/分类/事件抽取 | ✅ | ❌ | 工程（可做） |
| 双语 | ✅ | ❌ | 工程（可做） |
| 公开 API/RSS | ✅ 付费配额 | ⚠️ 静态 JSON + Pages | 工程（可做） |
| 多校区 | ✅ 3 校 | ✅ 4 校 | 我们更广 |

**结论：除"通道"之外，其余 9 项都是工程量，不是壁垒。**

---

## 7. 三条复刻路线

| 路线 | 做法 | 频率 | 成本 | 风险 |
|---|---|---|---|---|
| **A. 免费单凭据**（推荐先做） | 抓一次 `wap_sid2` → `--watch` 轮询 40 号，2 小时一轮 | 20 次/小时 | 0 | 凭据 30 分钟~3.5 小时过期 → **实际只能覆盖一个窗口**；要持续就得反复抓包 → **不满足"长期自动"** |
| **B. 多凭据轮换** | 2–3 个副号轮流，30 分钟一轮，夜间降频 | 80 次/小时 | 0（人力） | 每个号仍需定期扫码；封号风险分摊 |
| **C. 付费通道** | 接第三方/协议服务，分钟级 | 不限 | ¥（按号/按调用） | 合规与稳定性取决于供应商 |

**残酷的算术**：A 路线能"不漏"，但**不能"长期"**——凭据寿命决定了它只能是一次性全量。
这正是 pkuknow 要花钱的原因，也是"长期自动化 + 不漏"在免费条件下**结构性不可兼得**的地方。

---

## 8. 已落地的原型（本轮代码）

```bash
# 每 5 分钟拉一次第一页，只输出新增，落 JSONL + 状态文件
python tools/wxmp_spider.py history --name 浙江大学 \
    --watch --interval 300 -o _work/wechat/zju-new.jsonl

# 只跑一轮（自测/定时任务模式：交给 cron / 任务计划程序，每次跑 1 轮更稳）
python tools/wxmp_spider.py history --name 浙江大学 --watch --cycles 1 -o _work/wechat/zju-new.jsonl
```

- 每轮只发 **1 次** `getmsg`（`max_pages=1`），40 个号一轮 = 40 次；
- 去重靠 `_work/wechat/history-seen-<biz>.json`，重启不重复；
- `--cycles 1` + 系统调度器，比常驻进程更抗崩（这是"长期自动"的正确姿势）；
- 新增 6 例离线测试，`test_wxmp_spider.py` 96 例全绿。

---

## 9. 建议的下一步（按性价比排序）

1. **先做账本**：`history` 输出加 `status` 字段（ok/metadata_only/pending），失败进队列重试。
   —— 这是"不漏"的兜底，比提高频率更有效。
2. **来源健康页**：给每个号记 `last_ok / last_attempt / count`，`no_recent` 单独标出来。
   —— 照抄它的 `status: ok/no_recent`。
3. **图片本地化**：我们已有 `html_to_markdown`，把 `<img>` 下载成 `assets/<sha1>.webp` 即可。
   —— 解决外链失效/mmbiz 防盗链，站点离线也能看。
4. **AI 结构化**：先用现成模型抽 `is_event / event_time / location / deadline`，
   直接喂给站点的日历视图——这是 pkuknow 最"值钱"的部分，且**不依赖微信通道**。
5. **通道决策**：只有在"必须分钟级 + 长期"时才考虑 C；否则 A+调度器足够覆盖 90 天需求。

---

## 10. 边界与合规

- **无法确证**其微信数据来源（外部只有数据形态与作者口述），本文按"通道有成本"处理。
- **不要照搬频率**：5 分钟 × 145 号会立刻触发风控；我们的安全起点是 2 小时 × 40 号。
- **只存必要字段**：标题、来源、发布时间、链接；正文按需抓，图片本地化只做封面与正文图，
  不整站镜像。
- 本文所有数据来自公开接口的少量抽样（总计 < 80 次请求），未做批量导出。
