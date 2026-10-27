# 「校园知道」— 单文件校园信息聚合站

一个单文件的校园信息聚合站，顶栏 **4 项**：
**清华知道**（外链，点击直达 [pkuknow.cn/thu](https://pkuknow.cn/thu)）+ **浙大知道 / 上交知道 / 西交知道**
（由 `fetch_campus.py` 直接抓各校**官网 + 职能部门**，不抓公众号）。打开默认落在**西交知道**页面。
**全部功能跑在一个 `index.html` 里**，双击即可打开，无需联网、无需装任何依赖。

> 「清华知道」不再复制清华的内容，只作外链跳转；本页展示的是浙大 / 上交 / 西交三校的公开通知。

#### 按需加载（控制首屏体积）

页面不再一次性内嵌三校全部数据，而是拆成「轻量索引 + 按需分片」：

| 用户动作 | 实际下载 |
|---|---|
| 打开页面 | `index.html`（仅轻量索引，约 156 KB）+ 默认校区全量分片（后台静默预取） |
| 点开某条通知 | 该校区详情分片（正文摘要、活动地点、主办方…），取一次后缓存复用 |
| 切到另一个校区 | 只加载那个校区的全量分片 |
| **始终不下载** | 没点开的校区数据 |

这样未访问的校区不产生任何流量；分片由 `python refresh.py --build-only` 自动写入 `data/shard/`，
部署时须与 `index.html` **一起上传**（`cloud_update.py` / 云函数 / Actions 都已自动带上）。

> 三校规模下的一轮更新耗时与瓶颈：**[docs/规模与耗时.md](docs/规模与耗时.md)**。
> 要给某个学校换信息源、或者改页面配色与布局，请看 **[docs/修改指南.md](docs/修改指南.md)**；
> **要新增一所大学，看 [docs/添加新大学.md](docs/添加新大学.md)**（单所手工加 / 批量自动发现两条路）；
> 想搞清楚「官网链路（稳定）」和「公众号链路（不稳定）」在磁盘上怎么分，请看
> **[docs/目录结构.md](docs/目录结构.md)**；
> **想弄清「这个站为什么存在、边界在哪、什么情况下不够用」，看
> [docs/站点定位与必要性.md](docs/站点定位与必要性.md)**（实测画像：18 源 / 15 域名 / 天级更新 /
> 正文 520 字截断，其中 **41% 是没人看的官样新闻**；并附「学生活动」为何覆盖不到 —— 渠道已迁到公众号）；
> **接手这个项目的人，先看 [docs/移交说明.md](docs/移交说明.md)。**

## 校区

| 校区 | key | 数据来源 | 当前数据 |
|---|---|---|---|
| 清华知道 THU Know | `thu` | **外链**：点击直达 https://pkuknow.cn/thu（本站不抓取、不内嵌） | — |
| 浙大知道 ZJU Know | `zju` | **6 个**来源：求是新闻网（含要闻 / 科学头条）+ 研究生院 + 本科招生 + 图书馆（`fetch_campus.py`） | `data/zju.json` |
| 上交知道 SJTU Know | `sjtu` | **8 个**来源：通知公告 + 新闻网（要闻 / 综合）+ 教务处 + 研究生院 + 本科招生 + 学生事务中心 + 国际合作与交流处 | `data/sjtu.json` |
| 西交知道 XJTU Know | `xjtu` | **6 个**来源：新闻网 + 教务处 + 研究生院 + 研究生招生 + 本科招生 + 学生工作部（处） | `data/xjtu.json` |

> 4 栏清单与显示名集中在 **`tools/campus_roster.py`**（其中 `thu` 带一个「外链地址」）；改完跑
> `python tools/register_campus.py` 会自动同步 `refresh.py` / `template.html` / `cloud_update.py` /
> `.github/workflows/update.yml` / `tools/refocus_data.py` 五处，不用手工对齐。
> 「外链地址」非空的校区，在顶栏就渲染成**跳转到外部站点的链接**（新标签打开），本站不抓它的数据。

**只抓「官网 + 各校职能部门」**：来源精简为官网与职能部门（招生 / 教务 / 研究生院 / 学工 / 图书馆 /
国际交流这类更常见的信息需求），原先逐院系铺开的学院官网已移出。**不再抓公众号内容**：
`fetch_campus.py` 里 `kind=wechat` 的来源已全部移除，只抓网页内容。要临时补回某个来源，不改代码：
写进 `_work/sources-<校区>.json` 即可（同 id 覆盖、`enabled=false` 停用）。纯 JS 单页应用且无公开
接口的站点（如浙大就业服务平台、上交 / 西交图书馆、西交就业创业网）抓到 0 条，未登记。

**数据只增不减，保留 90 天**：每轮抓取只把**新发布的官网内容**追加进 `data/<校区>.json`，覆盖同名条目的标题与正文；
历史条目留在库里，但**发布时间距今超过 90 天的条目会在每轮写入和生成页面时自动清除**
（发布时间未知的条目不清除）。页面上：

- 「信息来源」抽屉里每个来源都带状态徽标 —— **最近更新**（本轮有新增）/ **无新内容**（抓到了但没有新条目）/
  **采集失败**（列表页请求失败）/ **待接入**（已登记但从未取到内容）；
- 顶部摘要显示「共 N 个来源有内容 · 累计 M 条 · 最近一轮新增 X 条」；
- 「数据概览」里按状态汇总各有多少个来源，并注明最近一轮新增/更新条数。

**公众号链路默认关闭**：站点当前**不再抓取任何公众号内容**（`fetch_campus.py` 里
`WECHAT_ACCOUNTS` / `WECHAT_ACCOUNT_FILTER` 已置空，且不再自动读取
`_work/wechat/wechat-<校区>.txt` 清单）。历史原因保留的 `fetch_wechat.py` 仍可在需要时
单独运行——它用**你自己**登录的微信公众平台 cookies 调后台接口按号拉历史文章，
但该接口有**账号级限流**（`ret=200013`）。详见 [docs/wxmp公众号接入.md](docs/wxmp公众号接入.md)。

> **但这条路本身是通的，有活证明**：[pkuknow.cn](https://pkuknow.cn/)（北大知道）用的就是
> **同一套后台接口**（`searchbiz` + `appmsg`）——每天采集 **24 个学生组织 / 社团号**、只保留
> **7 天窗口**、链接带完整 `chksm`、并按「**是否需报名**」分类。差别只在「**它只抓最近几天、
> 每天一次**」，而我们此前一直在追全量历史。拆解见
> [docs/公众号聚合方案评估.md](docs/公众号聚合方案评估.md) **第 13 节**。
> 要把这条路打开：`pip install wxmp` → 扫码登录自己的订阅号后台导出 cookies →
> 名单换成院系 / 学生组织 → `--count 3 --pages 1` 每天跑一次。

> **两条链路是分开的**：官网抓取（`refresh.py` / `fetch_campus.py`）稳定，
> 中间产物全在 `_work/` 根下；公众号抓取（`fetch_wechat.py`）不稳定，cookies / 名单 /
> 缓存 / 中间产物全在 `_work/wechat/` 里。两者只往同一份 `data/<校区>.json` 里追加，
> 所以页面合一，但任何一条断了都不影响另一条。详见 [docs/目录结构.md](docs/目录结构.md)。

三个校区的页面样式与功能完全一致：每个校区都有自己的地址（`#campus=zju`）、分类导航、
来源筛选、数据概览和信息来源抽屉；某个校区没有数据时会显示「信息源待接入」的占位页，
而不是空白或者报错。

分类导航下方还有一条 **「<校区>学习资料汇总」入口**：列出该校区学生社区维护的课程攻略 /
笔记 / 往年题仓库（浙大 zju-icicles、上交 SJTU-Courses、西交 XJTU-Share 等），点开是一个
外链清单，**本站只做索引，不存储、不镜像任何资料文件**。清单单独放在
**`data/study/materials.json`**，按校区配置，构建时注入页面（`<script id="study-data">`）；
加链接只改这个 JSON 再 `--build-only`，见
[docs/修改指南.md 1.10](docs/修改指南.md)。没有配置的校区（如作为外链的清华知道）不显示入口。

**再加一个校区**：完整步骤见 **[docs/添加新大学.md](docs/添加新大学.md)**（调研来源 →
写进 `SOURCES` → 登记校区名 → 本地验证 → 接入定时任务）。最省事的下限是两步：
把数据按 `data/xjtu.json` 的字段写成 `data/<key>.json`，再运行 `python refresh.py --build-only`，
切换栏就会自动出现这个校区（`campus_name` 用作显示名）。

## 文件说明

根目录只有**四个脚本 + 一个模板 + 成品**，其余按用途分到 `tests/`、`tools/`、`docs/`、`deploy/`。

```
university-know/
├── refresh.py            生成 index.html（也保留 pkuknow.cn 上游接口与导入/写入保护能力）
├── fetch_campus.py       抓浙大/上交/西交的官网与职能部门
├── fetch_wechat.py       按公众号成批拉历史文章（不稳定，可选）
├── cloud_update.py       无人值守唯一入口（云端 / 定时任务用）
├── template.html         页面模板（含 __NOTICE_DATA__ / __STUDY_DATA__ 等占位符）
├── sources_more.py       批量发现的信息源（可留空；由 tools/discover_sources.py 生成，fetch_campus.py 自动合并）
├── index.html            成品站点（自动生成，勿手改）
├── data/<校区>.json      两条链路的共同产物
├── data/study/materials.json  各校学习资料外链清单（手工维护，构建时注入页面）
├── tests/                回归测试（7 个套件，全部离线）
├── tools/dev/            开发辅助脚本（来源侦察、截图、体检）
├── tools/oneshot/        已完成使命的一次性脚本（留档）
├── deploy/               云上部署（serverless / edgeone）
├── docs/                 文档
└── _work/                抓取中间产物 + 公众号凭据
```

其中抓取脚本分**两条链路**：

### 官网链路（稳定）

| 文件 | 作用 |
|---|---|
| `refresh.py` | 生成 `index.html`；保留 pkuknow.cn 上游接口能力，也负责导入与写入保护 |
| `fetch_campus.py` | 抓浙大/上交/西交的**官网 + 职能部门**，产出 `data/<校区>.json` |
| `_work/items-<校区>.json` | `fetch_campus.py` 的抓取中间产物（入库前可先检查） |
| `_work/registry-<校区>.json` | 来源注册表：每个来源的状态、条数、本轮新增、最近检查时间（只增不减） |
| `_work/sources-<校区>.json` | （可选，手工维护）补充信息源；**没有也能跑**。模板见 `_work/sources.example.json` |
| `_work/run.log` | `cloud_update.py` 收进来的完整抓取日志（排查用，别当数据读） |

### 公众号链路（不稳定，全部集中在 `_work/wechat/`）

| 文件 | 作用 |
|---|---|
| `fetch_wechat.py` | 用 **wxmp 库**直接调公众平台接口，按公众号成批拉历史文章（需登录 cookies；无 cookies 时自动降级为公开链接种子模式） |
| `_work/wechat/wxmp-cookie-header.txt` | **登录后的 Cookie 整行**（推荐写法）；分校区版 `wxmp-cookie-header-<校区>.txt` |
| `_work/wechat/wxmp-cookie-header-pool2.txt` | **第 2 份凭据**（可选）。账号级限流换账号用；也可把多行写进 `wxmp-cookie-header-pool.txt`，一行一份 |
| `_work/wechat/wxmp-albums.json` | **公开合集清单**（可选，**不配也行**）。走 `appmsgalbum` 接口，**零凭据、不受限流影响**；不配时脚本会从 `wechat-<校区>.txt` 的文章页**自动发现**合集 ID |
| `_work/wechat/wxmp-blocked.json` | 限流记忆：谁被限过、什么时候、**账号级还是 fakeid 级**（可安全删除） |
| `_work/wechat/wxmp-accounts-<校区>.json` | 要抓的公众号**完整名称**名单（外部维护，不改代码）；全局版 `wxmp-accounts.json` |
| `_work/wechat/wxmp-fakeid.json` | fakeid 缓存：按完整名称锁定公众号，之后不依赖搜索（可安全删除重搜）；**按凭据分文件**，第 2 份是 `wxmp-fakeid-pool2.json` |
| `_work/wechat/wechat-<校区>.txt` | 手工补的公众号文章链接（一行一条，`#` 注释）；也是公开种子模式的输入 |
| `_work/wechat/wechat-items-<校区>.json` | 公众号抓取中间产物（`--no-import` 时的落点） |

> **卡在限流（ret=200013）时怎么办**：账号级限流**等待无效**，只能换登录账号——
> 多准备一份 cookies 存成 `wxmp-cookie-header-pool2.txt`，脚本会自动轮换；
> 没有第二份凭据时，直接 `python fetch_wechat.py --albums-only` 走公开合集源：
> **零凭据、不受限流、能拿到正文**（合集返回的链接带 `chksm` 所以抓得动），
> 合集 ID 还会自动发现，不需要配白名单。实跑 10 个种子链接 → 3 个合集 → 65 篇带正文。
> 完整说明见 [docs/wxmp公众号接入.md](docs/wxmp公众号接入.md) 第 10 节。
>
> ⚠️ **不要改用「桌面自动化模拟真人操作」**（wxauto 点微信客户端 /
> 给客户端开调试端口 / Frida 注入）。评估见
> [docs/公众号聚合方案评估.md](docs/公众号聚合方案评估.md) 第 10 节：
> 本机微信 4.1.15.55 的 UI 自动化**已彻底失效**；注入类方案违反
> 《微信个人账号使用规范》1.2.6/1.2.7，后果落在**个人微信号**上。
> 独立浏览器自动化（Playwright）只能补单篇正文，**留作人工兜底，不进流水线**。
>
> ⚠️ **也不要用「让大模型看屏幕 + 点鼠标」**（Computer Use / GUI 智能体）。
> 见同一文档第 11 节：完整的百步桌面任务成功率只有 42.5%，而取一篇链接要 20~30 步；
> 2025-12 豆包手机助手自动操作微信曾导致**账号被强制下线**（它同样是"视觉理解 +
> 模拟真人操作"，不 Hook 不注入）。规范 1.2.6 禁的是**"自动化操作"这个行为**本身。
> 正确形态是**人工点两下复制链接 + 脚本监听剪贴板入库**（短链本来就能抓）。
>
> ⚠️ 更彻底的形态（**机械臂 + 摄像头**替你点鼠标敲键盘）也评估过并否掉了，见第 12 节：
> 风控检测的是**输入的熵值**而非输入的来源，"动作完全一致、不知疲倦"恰恰是它要找的特征。
>
> 早期版本把这些文件直接放在 `_work/` 根下；首次运行 `fetch_wechat.py` 会**自动迁移**到
> `_work/wechat/`，读取时也会回退旧位置，所以升级不会丢 cookies / 名单 / 缓存。

### 公共与辅助

| 文件 | 作用 |
|---|---|
| `index.html` | **成品站点**，首屏只内嵌轻量索引（清华知道为外链），双击打开 |
| `data/shard/*.json` | **按需加载分片**，由 `refresh.py --build-only` 生成；部署时必须和 `index.html` 一起上传，否则点开详情取不到正文 |
| `template.html` | 页面模板（含 `__NOTICE_DATA__` 占位符），改样式/交互改这里 |
| `cloud_update.py` | **无人值守唯一入口**：抓三校 →（可选）公众号 → 重建页面，输出只留一段摘要。**公众号默认关闭，`--with-wechat` 才跑** |
| `data/<校区>.json` | 两条链路的**共同**产物（合并后的页面数据）；覆盖前会自动留一份同名 `.bak`（下一次写入时再更新） |
| `tests/` | **回归测试**，全部离线：`unit_fetch.py` / `pipeline_test.py` / `unit_wechat.py` / `test_handler.py` / `test_oss_min.py` / `test_lazy_load.js` |
| `tools/dev/` | 开发辅助：`probe_sources.py`（来源侦察）、`probe_sources_deep.py`（深挖子页）、`inspect_items.py`、`verify.js`、`cdp.js`、`shot-modals.js` |
| `tools/oneshot/` | 一次性脚本留档：`fix_titles.py`、`doc_recipe_check.py`（已完成使命，仅供追溯） |
| `deploy/` | 云上部署：`serverless/`（阿里云 FC）、`edgeone/`（腾讯 EdgeOne Pages，见下） |
| `docs/` | `移交说明.md`（**先看这个**）、`目录结构.md`、`修改指南.md`、`wxmp公众号接入.md`、`无人值守更新部署.md` |
| `build/reports/` | 开发期截图与体检报告（纯留档，可删） |

> **发布要看**：阿里云 OSS 默认域名（`*.aliyuncs.com`）对 2017-10 后创建的桶会**强制下载** html，
> 绕不过（改元数据 / 开静态托管都没用），必须绑**已备案**域名。因此站点发布改用
> **腾讯 EdgeOne Pages**：免费额度、默认域名 `*.edgeone.app` **免备案即可直接渲染**。
> 详见 `deploy/serverless/README.md` 与 `deploy/edgeone/deploy_edgeone.py`。

`data/`、`_work/`、`build/` 都是**生成物**：删掉不会损坏项目，跑一次
`python fetch_campus.py` 就能重建。**但 `_work/` 里有三类手工文件不能删**：
`_work/sources-<校区>.json`、`_work/wechat/wechat-<校区>.txt`、
`_work/wechat/wechat-名称总表.txt`、`_work/wechat/wxmp-accounts-*.json`（以及 cookies）。
完整清单见 [docs/目录结构.md](docs/目录结构.md)。

## 测试

七个套件全部**离线**（不联网），改完代码先跑一遍再提交：

```bash
python tests/unit_fetch.py      # 标题清洗 / 日期解析 / 正文抽取  —— 44 项
python tests/pipeline_test.py   # refresh.py 写入保护 / 合并逻辑  —— 18 项
python tests/unit_wechat.py     # 公众号名单 / 缓存 / 迁移        —— 131 项
python tests/test_handler.py    # 云函数 handler 端到端（含 mock OSS）
python tests/test_oss_min.py    # 自写 OSS 客户端签名与官方 SDK 对拍
python tests/test_study.py      # 学习资料清单校验 / 注入结果     —— 27 项
node tests/test_lazy_load.js    # 页面按需加载 / 索引与分片合并  —— 15 项
```

> 这些脚本全部**自带 mock，不联网、不写真实 `data/`**（用到临时目录，跑完自动清）。
> 每个脚本末尾都会打印「通过 N/N」或「✓ 全部通过」。
> `test_lazy_load.js` 是唯一需要 Node 的套件：它读**已生成的** `index.html` 与
> `data/shard/*.json`，断言条数一律与分片对齐（不写死数字），所以数据更新后不会假失败。
> 只改了模板时，先 `python refresh.py --build-only` 再跑它。

## 常用命令

```bash
python refresh.py --build-only         # 不联网，用 data/*.json 重新生成页面（最常用）
python refresh.py --campus zju         # 上游模式：指定校区(暂无上游接口的会自动跳过并说明)
python refresh.py --limit 300          # 上游模式下只抓最近 300 条，快很多
python refresh.py --campus zju --import items.json            # 导入自建信息源(按 id 合并)
python refresh.py --campus zju --import items.json --replace  # 整校区替换
python refresh.py --no-details         # 只抓列表不抓正文，体积小约 10 倍

# —— 官网链路（稳定）——
python fetch_campus.py                 # 抓浙大/上交/西交的官网与职能部门，写入 data/ 并生成页面
python fetch_campus.py --campus zju    # 只抓一个校区
python fetch_campus.py --workers 6     # 并发抓 6 个来源（默认 4；跨主机并行、同主机仍按 1.2s 限速）
python fetch_campus.py --max-per-source 8   # 每个来源最多取 8 条（默认 15）
python fetch_campus.py --retries 1     # 单页失败重试次数（默认 2）
python fetch_campus.py --no-details    # 不抓正文，快很多
python fetch_campus.py --no-import     # 只抓取，结果落在 _work/items-<校区>.json
python fetch_campus.py --import-only   # 不联网，把 _work/ 上次的结果写入 data/

# —— 公众号链路（不稳定，装备在 _work/wechat/）——
python fetch_wechat.py                # 用 wxmp 抓三校官方公众号，写入 data/ 并生成页面
python fetch_wechat.py --campus zju   # 只抓一个校区(zju/sjtu/xjtu)
python fetch_wechat.py --list-accounts   # 只看合并后的名单与 fakeid 缓存情况，不联网
python fetch_wechat.py --dry-run      # 只验证 cookies 是否有效
python fetch_wechat.py --no-content   # 只取列表不抓正文，快很多
# 无 cookies 时会自动降级为「公开链接种子」模式(读 _work/wechat/wechat-<校区>.txt 逐篇抓正文)

# —— 无人值守 / 定时任务 ——
python cloud_update.py                # 无人值守：抓三校，只输出一段摘要（云端/定时任务用）
python cloud_update.py --with-wechat  # 再加上公众号这一步（需本机有 cookies 与 wxmp）
python cloud_update.py --wechat-only  # 只跑公众号这一步
python cloud_update.py --summarize-only   # 不抓取，只看现状摘要
```

Python 3.8+，官网链路**仅用标准库**，无需 `pip install`。**唯一例外**是 `fetch_wechat.py`
的 API 模式需要 `wxmp`（它只在真的要按号抓取时才 import；公开种子模式不需要任何依赖）。

> **关于 pkuknow.cn 上游 403**：该站现在要求**先取会话 cookie 再读数据**——直接打
> `/thu/api/notices` 会返回 `403 {"code":"READ_SESSION_REQUIRED"}`。`refresh.py` 内置了
> 握手：抓取前先访问一次站点拿到 `__Host-pku_read_guest` cookie（挂在 `cookiejar` 上），
> 会话中途失效会自动重新握手。当前四校的 `CAMPUS_PREFIX` 都是 `None`（清华是外链、三校走
> 官网抓取），所以这条上游链路平时用不到；若某校日后恢复上游接口，登记前缀即可启用。

**写入保护**：抓取不完整时默认**不覆盖** `data/*.json` —— 列表页失败、实际条数比目标少 2% 以上、
上游 `total_all` 异常、或比上次缩水 10% 以上，都会保留旧数据并在 stderr 说明原因；确实要写残缺
数据就加 `--allow-partial`。覆盖前会先留一份 `data/<campus>.json.bak`，写入是原子的（临时文件 +
替换），详情抓取失败的条目会沿用上一次的结果，不会把摘要清空。

**字段说明**：`total_all` 是上游站点总数，`fetched` 是本次实际写入的条数，`truncated` 标明
`--limit` 是否截断过。`sources` / `groups` / `category_totals` / `intent_totals` 一律按本次抓到的
`items` 现算（上游只在第 1 页给汇总，直接沿用会和正文对不上），`fetch_warnings` 记录本次所有告警。

## 已实现的功能

- **顶栏**：多校区切换（含 `#campus=<key>` 深链；**清华知道为外链，点击在新标签打开 pkuknow.cn/thu**）、
  圆角搜索框（支持标题 / 来源 / 正文全文检索）、活动日历、信息源补充、信息来源、数据概览、
  昼夜主题、交流群入口
- **吸顶分类导航**：左侧「全部」磁贴 + 「活动」「通知」两个色块组、13 个分类，
  复刻原站的 8 色 tone 体系（water / lilac / peach / amber / ochre / rose / iris / slate）
- **分区卡片网格**：每区一个色调的圆角面板 + 白色卡片，卡片显示标题、发布时间、来源；
  悬停上浮并出现左侧色条；48 小时内的通知带「最新」圆点
- **详情弹窗**：发布时间、活动时间、地点、主办方、报名截止、AI 摘要、正文摘录、
  附件列表，以及复制链接 / 阅读原文；支持 `#notice=<id>` 深链
- **筛选工具栏**：来源分组（微信公众号 / 官方网站）、时间范围、排序、每行列数、重新渲染
- **近期活动视图**：从详情里抽出带明确活动时间的通知，按开始时间正序排列
- **学习资料汇总入口**：分类导航下方常驻一条本校区资料入口，弹窗内是按校整理的课程攻略 /
  笔记 / 往年题仓库清单（浙大 zju-icicles、上交 SJTU-Courses、西交 XJTU-Share 等），
  全部为外链，站点本身不落地任何资料文件
- **空校区占位页**：某个校区还没有数据时说明「信息源待接入」并提供「先看浙大知道 / 如何接入信息源」两个入口
- **来源状态与 90 天保留期**：每轮只追加新发布的官网内容，超过 90 天的历史条目自动清除；每个来源带状态徽标，
  「数据概览」按状态汇总，并显示最近一轮新增 / 更新条数
- **其它**：移动端自适应（2 列卡片）、中英文标题切换、本地偏好与主题记忆、
  空状态引导、键盘 `/` 聚焦搜索、`Esc` 关闭弹窗

## 数据来源与版权

- **清华知道**：本站不再复制内容，顶栏该项为**外链**，直接跳转到 pkuknow.cn 的「清华知道」页面。
- **浙大 / 上交 / 西交**：由 `fetch_campus.py` 直接抓取各校**官网与职能部门**的公开通知
  （新闻网 / 教务处 / 研究生院 / 招生 / 学工 / 图书馆 / 国际交流等栏目）。

所有内容版权归原作者与发布单位所有，本页仅作展示与检索之用，请勿用于商业用途。

「学习资料汇总」入口只是指向各校学生社区仓库的链接集合（例如
[qsctech/zju-icicles](https://github.com/QSCTech/zju-icicles)、
[kxxwz/SJTU-Courses](https://github.com/kxxwz/SJTU-Courses)、
[cantjie/XJTU-Share](https://github.com/cantjie/XJTU-Share)），
本仓库不包含、不镜像其中的任何资料；资料的许可与可用性以各仓库自身说明为准。

想让数据保持新鲜：跑 `python fetch_campus.py`（或 `python cloud_update.py`）即可更新三校并重建页面。
