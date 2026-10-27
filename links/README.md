# links/ —— 公开链接抓取（把"一篇"扩成"一批"）

**一句话**：从公开可读的文章链接出发，汇总出「三个校区到底能拿到多少篇、都是谁发的」。
零凭据、零第三方库，不 import `albums` / `fetch_wechat` / `fetch_campus` 的代码。

```bash
python -m links harvest --all                  # 汇总渠道，更新台账
python -m links report  --all --top 15         # 有多少篇、发布者都有谁
python -m links show --campus zju --account 浙大  # 抽查某号具体有哪些条目
```

---

## 一、它和 albums/ 的分工

| | `albums/` | **`links/`** |
|---|---|---|
| 入口 | 合集**接口**（JSON） | **文章页**（HTML）+ 搜狗搜索 |
| 一个入口给多少 | 一个合集几十~几百篇，全带永久链接 | 一篇文章页只给 1 个自己 + 1~4 个关联 |
| 强项 | 批量、稳定、字段全 | 能**发现新号**、能把别处的文章捞回来 |
| 弱项 | 只知道"有合集的号" | 单篇成本高（3.5 MB/页） |

两者通过**数据文件**交接，不互相 import：

```
links harvest  ──写──▶  _work/albums/discovered-<校区>.json  ──读──▶  albums fetch
albums fetch   ──写──▶  _work/albums/albums-<校区>.json      ──读──▶  links harvest
```

---

## 二、实测：四个渠道分别给得到什么（2026-10-27，别再凭印象改）

| 渠道 | 能给出 | 永久链接 | 单次成本 |
|---|---|---|---|
| 合集接口（读 albums 产物） | 标题/号/日期/链接 | ✅ | 0 |
| 种子文章页 | 同上 + 关联链接 | ✅ | 3.5 MB/页 |
| 搜狗搜索 | 标题/号/日期 | ❌ 只有分钟级签名链 | 1 请求/页结果 |
| 页内关联链接 | 同号其它文章 | 4 条里约 1 条带 chksm | 3.5 MB/页 |

### 四条链接形态，只有一种抓不到

| 形态 | 例 | 能抓 |
|---|---|---|
| 短链 | `/s/XLsEsQOyo4qwtekckfDT2A` | ✅ |
| 搜狗签名链 | `/s?src=11&timestamp=..&signature=..` | ✅（但它几分钟就失效） |
| 长链带 chksm | `/s?__biz=..&mid=..&sn=..&chksm=..` | ✅ |
| **长链缺 chksm** | `/s?__biz=..&mid=..&sn=..` | ❌ 17 KB 验证码墙 |

⚠ **唯一真正的坑**：文章页里的关联链接有一半是"缺 chksm"的形态，抓不动；
另外它们被 **JSON 转义**过（`\x3ca href=\x22http://...\x26amp;mid=`），
不去转义就只能匹配到 `__biz=` 就被反斜杠截断 —— 会得出"页内 0 条链接"的错误结论
（一开始就这么错了）。还有 `${window.biz}` 这类模板占位符必须排除，否则会拼出假链接。

---

## 三、两档台账（这是本包最重要的设计）

```
A 档（可入库）  有永久链接：点得开、能抓正文、能进站点
B 档（仅盘点）  只有标题/号/日期：能证明"这篇存在、这个号发过"，但给不了可长期用的链接
```

搜狗命中的文章全部落在 B 档 —— **因为没有永久链接**。`var msg_link` 在搜狗来源的页面上
是被剥掉的，`biz+mid` 直拼（`?__biz=..&mid=..&idx=1&scene=1`）实测全是 31 KB 验证墙。
所以报告里两档必须分开列，不然会把"只是看见了"算成"已经拿到了"。

### B 档怎么转成 A 档

搜狗来源的页面上虽然没 `msg_link`，但**如果这篇属于某个合集，页面上会印着 `album_id`**。
于是有一条实测有效的路径：

```
搜狗命中 → 打开文章页 → 挖到 album_id → 写进 albums 的发现清单
        → python -m albums fetch → 该合集全部文章带 chksm → A 档
```

实测产出（每 20~24 个开页）：

| 校区 | 开页 | 挖到合集 | 带来的 A 档文章 |
|---|---|---|---|
| 浙大 | 20 | 3 个 | +142（校友动态 135、科幻 7） |
| 浙大（第二轮） | 24 | 1 个 | +8 |
| 上交 | 14 | 3 个 | +244（招生宣传 210、通知 20、交大新闻 14） |
| 上交（第二轮） | 24 | 2 个 | +244（通知公告 155、学术之星 89） |
| 西交 | 20 | 0 个 | 0 |

结论：**这是一个高杠杆但会衰减的动作**。前 50 个开页把上交从 18 篇拉到 506 篇（28 倍），
再往后基本归零。西交的号不用合集，所以开页纯亏 —— 这也是为什么 `--probe-albums` 默认关。

---

## 四点五、60 天门槛会传到这里

合集侧有「只看最近 60 天内有更新的合集」（`albums/albums.json` 的 `max_age_days`）。
过期合集在 `_work/albums/albums-<校区>.json` 里已经不存在了，但**台账是累计的**——
上一轮抓的文章还会赖着。所以 `harvest_albums()` 收完新条目后会做一次清理：

```
只删「来源全是 album、且当前合集产物里已经没有」的条目
```

双来源（合集 + 种子）的条目会留下：它另有出处，不该因为合集过期就丢。

## 四点六、接进站点

```bash
python -m links export --all              # 只导出 _work/links/site-<校区>.json
python -m links export --all --merge      # 同时并入 data/<校区>.json（写前留 .bak）
python refresh.py --build-only            # 重建 index.html + 分片
```

合并口径（`merge_into_data`）：

* 按 `i`（文章链接）去重，**只追加不改历史**；
* **只对公众号来源套 90 天保留期**：官网源的生命周期归 `fetch_campus.py` / `refresh.py` 管，
  顺手删人家的条目就越界了（第一版这么干过，剔掉了 1 条官网旧新闻）；
* 顺带在 `source_registry` 里给每个号登记一条，页面「信息来源」直接能看到；
* B 档（只有元数据）**不入站**——没有链接就点不开。

## 四、命令

```bash
# 常规：合集 + 种子 + 搜狗元数据（几分钟）
python -m links harvest --campus zju

# 加上搜狗按号补查（默认 8 个"还没永久链接的号"，按名字搜）
python -m links harvest --campus zju --account-queries 12

# 高杠杆：开 N 篇搜狗命中的页面挖合集（3.5MB/篇，衰减快，逐步加量）
python -m links harvest --campus sjtu --probe-albums 20
python -m albums fetch --campus sjtu --all-pages     # ← 必须紧跟这一步才转成 A 档
python -m links harvest --campus sjtu --no-sogou     # ← 再跑一次把新合集并进台账

# 只挖关联链接（低价比：平均一页约 1 条新链接，默认关）
python -m links harvest --campus zju --expand 40

# 出报告 / 导出
python -m links report --all --top 20
python -m links report --campus zju --csv _work/links/publishers-zju.csv

# 云端定时任务只跑这一段（合集产物 → data），不碰种子与搜狗
python -m links harvest --all --no-sogou --no-seeds
python -m links export  --all --merge

# 接进站点
python -m links export --all --merge
python refresh.py --build-only
```

---

## 五、一条命令背后的判据

**「谁属于这个校区」只看发布者，不看标题。** 搜狗结果里混着大量别家媒体
（杭州日报、观察者网、培训机构），它们的标题里都有"浙大"。若按标题放行，
发布者名录立刻被污染——你要的是"谁在发"，不是"谁提到"。

所以 `relevant_publisher()` 只检查两件事：号在 `albums/albums.json` 的名单里，
或号名命中该校区关键词。账号名单**只有一份**（在 albums 的配置里），本包只读不写。

**但只有关键词会出事**：上交和西交的 filter 里都写着「交大」，于是西交二附院的文章
被算进了上交台账（实测真的发生了）。所以 `links.json` 每个校区还有一份 `exclude`
（别校特征词），判定顺序是：

```
名单精确命中 → 放行     （人工名单比正则可信，所以它有最高优先级）
命中别校特征词 → 拒绝   （挡住「交大」这类通用词）
命中本校关键词 → 放行
```

`prune_library()` 在每轮 harvest 开头用同一套判据兜一次底，把历史串号条目清出去（幂等）。

---

## 六、产物

```
_work/links/
├── library-<校区>.json        台账（A/B 两档，含 sources 溯源）
├── report-<校区>.json         报告统计（供页面/外部脚本读）
├── albums-found-<校区>.json   从文章页挖到的合集（与 albums 的发现清单同步）
└── publishers-<校区>.csv      发布者表格（report --csv 生成）
```

`_work/links/` 整个目录都在 `.gitignore` 里：它可以从公开渠道重建（几十分钟），
没必要把上千条 URL 塞进仓库；要留存就 `report --csv` 导表格。

---

## 七、失败模式

| 现象 | 含义 | 处置 |
|---|---|---|
| `搜狗触发反爬验证（antispider）` | 本机 IP 短时间请求太多，**不是封号** | 停 10~30 分钟；或把 `--sogou-pace` 调大、查询数调小 |
| 撞墙计数 > 0（种子） | 该链接是"缺 chksm"形态 | 正常损耗，换链接；不影响其它种子 |
| `开页 N 篇挖到 0 个合集` | 这批号不用合集功能 | 该校区别再加大 `--probe-albums`（西交就是这样） |
| B 档占比高 | 搜狗只能给元数据 | 想转 A 档只能靠合集；没有合集的号永远停在 B 档 |
