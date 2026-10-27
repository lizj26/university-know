# albums/ —— 公开合集抓取（独立链路）

**一句话**：把公众号「公开合集」（作者手工整理的专题）里的文章列表抓下来。
零凭据、零第三方库、不调 `appmsg` 接口，所以**不受公众号账号级限流影响**。

```bash
python -m albums fetch            # 没有合集时自动先发现，然后增量抓
python -m albums list             # 现在有哪些合集、上次抓到哪
python -m albums check            # 自检：接口还通不通、字段还在不在
python -m albums export           # 产出站点 item 结构（本包自己不写 data/）
```

---

## 一、为什么要单独成一个包

原来是塞在 `fetch_wechat.py`（2000+ 行）里的 `--albums-only` 开关。它和同文件里的
cookies 池、wxmp 登录、搜狗兜底、`getmsg` 历史、正文抓取、分类、合并、建站**共用一个进程、
一套全局变量**。后果很具体：

| 原来的问题 | 具体表现 |
|---|---|
| 入口错位 | 想跑"只用合集"，得启动一个加载 `wxmp`/`requests`/`pandas` 的脚本 |
| 依赖错位 | 合集本来零凭据，却因为同文件 import 了 wxmp 而"装不上依赖就整个跑不起来" |
| 落盘错位 | 抓完直接 `merge_into_data()` 写 `data/<校区>.json`，和官网站点产物搅在一起 |
| 账号归属靠猜 | 合集清单里没写公众号名，代码只能 `account = alb.get("name") or "微信公众号"`，然后拿校区关键词去猜，猜错就整批丢弃 |
| 每次都从头翻 | 没有水位，每轮都从第 1 页翻起；合集大的（278 篇）要翻 14 页 |
| 出错就是整轮 | 一个合集的 `ret=10004` 会把整轮变成"失败"，看不出是哪一个坏了 |

现在：`albums/` 是**独立包 + 独立命令行 + 独立产物目录**，删掉 `fetch_wechat.py`
和 `fetch_campus.py` 它照样能跑（只有一条一致性测试会 skip，见第六节）。

### 和另外两条链路的边界

| | 官网链路 | 公众号链路 | **公开合集链路** |
|---|---|---|---|
| 脚本 | `fetch_campus.py` | `fetch_wechat.py` | **`python -m albums`** |
| 凭据 | 无 | cookies / 号池 | **无** |
| 第三方库 | 无 | `wxmp`、`requests`… | **无（只用标准库）** |
| 失败粒度 | 单个站点 | **整个账号**（`ret=200013`） | **单个合集** |
| 产物 | `_work/items-*`、`data/` | `_work/wechat/`、`data/` | **`_work/albums/`（不写 `data/`）** |
| 代码依赖 | — | — | **不 import 上面任何一个文件** |

唯一"共享"的是**只读数据**：种子文章链接清单 `_work/wechat/wechat-<校区>.txt`
（也可以改用自己的 `albums/seeds-<校区>.txt`，见第五节）。读数据文件不等于代码耦合。

---

## 二、接口事实（2026-10 实测，别凭记忆改）

```
GET https://mp.weixin.qq.com/mp/appmsgalbum
    ?action=getalbum&__biz=<biz>&album_id=<id>&f=json
    &count=20&begin_msgid=<msgid>&begin_itemidx=<itemidx>
```

| 事实 | 含义 |
|---|---|
| 返回 200 + JSON，**不需要任何登录态** | 整条链路的立足点。零凭据、无账号风险 |
| 参数错时返回 `{"base_resp":{"ret":10004}}` | 接口本身是开放的，不是"换个参数就能全量"的门槛 |
| `getalbum_resp.base_info` 里有 `title`（合集名）、`nickname`（公众号名）、`article_count`（总篇数）、`username`（`gh_`） | **公众号名是接口给的，不用猜**——这是对旧实现最实质的修正 |
| `continue_flag=1` 表示还有下一页，用上一页最后一行的 `(msgid, itemidx)` 续取 | 翻页是游标式的，不是 `page=2` |
| `article_list[].url` 带 `chksm` | 正文可抓（对照实验：同一篇去掉 `chksm` 就吃验证码墙） |
| `base_info.is_reverse` 决定默认排序：`True`=新的在前，`False`=旧的在前 | 不加参数时接口按各合集的设置返回。**正序会让增量失效**（水位比的是一页里的最大 msgid，正序第一页永远最旧，每次都在第一页收工）。所以发现是正序就带 `is_reverse=1` 重取一次 |
| `is_reverse=1` 是"**翻转**默认顺序"，不是"强制倒序" | 对本来就倒序的合集反而会翻成正序。所以不能无条件下这个参数（踩过：`浙大等你` 被翻成 2022→2023） |
| 每页实际 20 条（`count` 只是上限） | 278 篇的合集 = 14 页 |

**合集从哪来**：微信把合集完整链接印在文章页里
（`mp/appmsgalbum?__biz=..&action=getalbum&album_id=..`），所以只要拿到**任意一篇**该号的
文章，就能自动发现它属于哪些合集。`discover` 做的就是这件事，不需要人工去找 album_id。

---

## 三、数据流

```
种子文章页（3.7MB/页，只用来发现合集）
    └─ discover ──→ _work/albums/discovered-<校区>.json   ← 机器写，可删可重建
                         +
    albums/albums.json（人工维护的校区归属）
                         ↓ resolve
             合集清单（biz + album_id + 公众号名）
                         ↓ fetch（游标翻页 + 增量水位）
      _work/albums/albums-<校区>.json   ← 累计产物 + 每个合集的健康状态
      _work/albums/state.json           ← 水位与已见集合
                         ↓ export（显式）
      _work/albums/site-<校区>.json     ← 站点 item 结构，供下游合并
```

**为什么不直接写 `data/`**：`data/<校区>.json` 是三条链路的合流点，也是 `refresh.py`
的输入。合集一旦往里写，就又和建站、和另外两条链路的状态机绑上了；而且合集里的文章
最老能到 2022 年（远超站点 90 天保留期），闷头合并会把 `data/` 撑爆。
要入站就显式走 `export`（默认按 90 天过滤，`--retention-days 0` 可全量）。

---

## 四、增量机制，以及它的取舍

`state.json` 给每个合集记 `last_msgid` 和已见文章 key。增量轮的做法是：
**翻到「整页都是旧文」就停**（本页最大 msgid ≤ 水位）。

不用"按日期判断顺序"来停，因为真实数据里有些文章没有 `create_time`，
按日期会误判成"顺序未知"从而多翻好几页（实测 1 页变 2 页）。

实测（浙大 4 个合集 / 637 篇）：

| 场景 | 耗时 | 翻页 |
|---|---|---|
| 首次全量 `fetch --all-pages` | ~38 s | 34 页 |
| 增量 `fetch`（无新文章） | ~3.5 s | 4 页（每集 1 页） |

### 只看最近 60 天内有更新的合集

`albums.json` 的 `max_age_days: 60`（可被 `--max-age-days` 覆盖，`0`=不限）：

* **判据**是「合集里最新一篇文章的发布日期」，不是"我们上次抓的时间"——
  后者只能说明我们勤快，前者才说明这个合集还活不活着；
* 过期合集**只取第一页就判定、不翻页、不收文章**，状态记 `stale`，
  `list` 里仍能看到（附最新日期），方便判断是不是该手工删掉；
* **历史文件也会被清**：只清本轮不写是不够的，上次抓的旧条目还赖在 `albums-<校区>.json` 里；
* 判不出来时 **fail-open**（放行并标 `fresh_unknown`）：第一页一条日期都没有，
  或者顺序没能扭转过来（那时第一页的 max 只是最老那页里最新的，拿它判过期会误杀）。

实测（2026-10-27，门槛日 2026-08-28）：15 个合集中 7 个被判过期，
文章数 1433 → 972。被砍掉的典型是「校友动态」（最新 2024-01）和
「通知公告 · 物理与天文学院」（最新 2026-06-25）。

**已知盲区（重要）**：合集的编辑可以**事后把一篇文章补进合集中间**，
这种"旧文新入"不会被水位捞到。

| 需求 | 做法 |
|---|---|
| 日常更新 | `python -m albums fetch`（秒级；正序合集已自动扭转，增量恢复正常） |
| 定期兜底不漏 | 每周跑一次 `python -m albums fetch --all-pages`（分钟级） |

`--all-pages` 会把每集翻到底，成本是一次全量。这是**唯一**能保证"合集内不漏"的方式，
因为合集不是按时间线整段追加的，它是**人编辑出来的一组文章**。

---

## 五、加一个合集 / 加一个校区

### 加合集

```bash
python -m albums add --campus zju --ref "https://mp.weixin.qq.com/mp/appmsgalbum?__biz=..&album_id=.."
# 也接受更短的写法：--ref "MzM5NDgxNTQwNQ==:2418213410132803584"
```

写进 `albums/albums.json`（手改的文件，只存事实，不存进度）。`title` / `account`
不填也行，抓取时接口会给。停用某个合集：加 `"disabled": true`。

不想手工找链接：`python -m albums discover` 从种子文章页自动发现，
结果落在 `_work/albums/discovered-<校区>.json`，**不会污染人工清单**。
发现阶段按号抽样（`# 账号名` 注释分组，每号 2 篇）—— 同组文章指向同一合集，
不抽样的话每轮要吞几十个 3.7MB 的页面。

### 加校区

`albums/albums.json` 的 `campuses` 里加一项：

```json
"nju": {
  "name": "南大知道",
  "filter": "南京大学|南大|NJU",
  "seeds": ["albums/seeds-nju.txt", "_work/wechat/wechat-nju.txt"],
  "accounts": ["南京大学", "南大招生"]
}
```

`filter` / `accounts` 只用于一件事：判断**这个公众号是不是本校的**（防跨校污染）。
返回结果里每篇文章都带 `campus_confident`，`export` 默认把 `false` 的过滤掉
（`--include-unconfirmed` 保留）。判断不了（没配任何依据）时一律放行，不猜着丢数据。

---

## 六、测试

```bash
python tests/test_albums.py      # 25 个用例，全离线
```

覆盖：接口解析（含 `ret=10004`）/ 合集链接两种顺序与 `%3D%3D` 归一 / 游标翻页 /
增量水位（首页全旧就停、首页有新的就继续）/ **游标不前进必须自己停**（真踩过死循环）/
分类规则与 `fetch_campus.py` 的一致性 / 站点 item 结构与保留期 / 原子写 / 种子分组。

一致性测试用 `ast` 从 `fetch_campus.py` 里读出 `CATEGORY_RULES` 做比对：
分类规则是**故意复制**一份的（为了本包能脱离另外两个脚本单独运行），
代价是可能漂移，所以让漂移变成一条红灯，而不是页面上一批文章莫名归错类。

---

## 七、失败模式与排查

| 现象 | 含义 | 处置 |
|---|---|---|
| `ret=10004` | `__biz`/`album_id` 不对，或合集被删/设为私密 | `list` 看是哪一条，`add` 重登或删掉；不影响其它合集 |
| `ret=0` 但 0 篇 | 合集确实是空的（作者清空了） | 正常，状态记 `empty` |
| HTML 变成验证码墙 | 抓的是**文章页**（只有 `discover` 会抓） | 换一篇种子；合集接口是 JSON，不受此影响 |
| 状态 `partial` | 翻页中途失败，本轮拿到一部分 | 增量会补；连续 `partial` 看 `--timeout` |
| `分页游标没有前进` | 接口给了重复游标 | 代码会主动停；这是保护，不是 bug |

自检用 `python -m albums check`：逐个合集取第一页，打印 `ret`、条数、总篇数，
以及 `has_url/has_time/has_key` 三个字段断言——字段哪天变了会立刻显形。
