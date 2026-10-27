# pkucli 公众号爬虫方案：拆解、移植与本地实测

> 目标仓库：`https://github.com/pkuinfo/pkucli`（Rust，crate `info-spider`）
> 本文结论基于 **2026-10-24** 抓取到的源码与实机测试。
> 本地移植件：`tools/wxmp_spider.py`；离线单测：`tests/test_wxmp_spider.py`。

## 1. 结论摘要

| 问题 | 结论 |
| --- | --- |
| 它是什么方案 | 不用「公开接口/合集」，而是**自己扫码登录 mp.weixin.qq.com 后台**，拿 token 后按人类点击顺序调后台 CGI |
| 能不能用 | 协议全部实测可达；**登录握手后的接口需要真实微信号扫码**（测试号即可），无扫码则只能验证到二维码与轮询 |
| 值不值得接 | 值得作为 `fetch_wechat.py` 的**登录方式补充**：省掉手工复制 cookie 这一最易出错的环节；但限流（`ret=200013`，账号级、等待无效）本质没变 |
| 本地产物 | `tools/wxmp_spider.py`（登录/搜索/列表/正文→Markdown）+ 29 个离线单测全绿 + 4 项联网实测 |

## 2. 方案拆解

### 2.1 九步链路（`docs/wechat-mp-flow.md` + `login.rs` / `search.rs` / `articles.rs`）

| 步骤 | 请求 | 作用 |
| --- | --- | --- |
| 1 | `GET /` | 拿初始 cookie（`uuid`、`_qimei_*`） |
| 2 | `POST /cgi-bin/bizlogin?action=startlogin` | 开扫码会话，`login_type=3`、`fingerprint` 32 位 hex |
| 3 | `GET /cgi-bin/scanloginqrcode?action=getqrcode` | 取二维码（**返回 JPEG**，不是 PNG） |
| 4 | 轮询 `GET .../scanloginqrcode?action=ask` | `status`：0 待扫 / 4 待确认 / 1 成功 / 5、6 过期 |
| 5 | `POST /cgi-bin/bizlogin?action=login` | 响应的 `redirect_url` 里带 `token=XXXX` |
| 6 | `GET /cgi-bin/home?...token=` | 坐实 session |
| 7 | `GET /cgi-bin/appmsg?t=media/appmsg_edit_v2&isNew=1&type=77...` | **只为拿 Referer**，不解析页面 |
| 8 | `GET /cgi-bin/searchbiz?action=search_biz` | 公众号名/微信号 → `fakeid` |
| 9 | `GET /cgi-bin/appmsgpublish?sub=list&sub_action=list_ex` | 文章列表，`begin` 翻页，页长 `count`（默认 5） |

单篇正文另走一条路：用**微信内置浏览器 UA** 直接 `GET` 文章 URL，抽 `#js_content` 转 Markdown——不需要任何凭据。

### 2.2 三个必须照抄的细节

1. **Referer 必须是编辑器页**。第 7 步那个 `appmsg_edit_v2` URL 就是第 8、9 步的 `Referer`；拿 `https://mp.weixin.qq.com/` 当 Referer 会被判权限不足。这是 Rust 实现里专门开一个 `warmup()` 的唯一原因。
2. **`fakeid` 里的 `==` 要编码成 `%3D%3D`**。`urlencoding::encode` / `quote(safe='')` 都会做，手拼字符串极易踩。
3. **响应是三层字符串化 JSON**：

   ```
   外层        {base_resp, publish_page: "<JSON 字符串>"}
   publish_page {total_count, publish_list: [{publish_info: "<JSON 字符串>"}]}
   publish_info {appmsgex: [{title, link, cover, update_time, ...}]}
   ```

   两层都要再 `json.loads` 一次；`publish_info` 解析失败要跳过而不是整页报错。

### 2.3 错误码

| ret | 含义 | 处置 |
| --- | --- | --- |
| 0 | ok | — |
| 200003 | session 失效 | 重新登录 |
| 200013 | **账号级**频率限制 | 等待无效，只能换号（pkucli README 也这么写） |

## 3. 本地移植

```
tools/wxmp_spider.py       # 单文件、零第三方 md 库（只依赖 requests + bs4）
tests/test_wxmp_spider.py  # 29 个离线用例
```

会话目录默认 `~/.config/info-spider/`（`session.json` + `cookies.json`，与 Rust 版一致），
可用 `--home` 改到仓库内：

```bash
PY="C:/Users/Kevin/.workbuddy/binaries/python/envs/default/Scripts/python.exe"

$PY tools/wxmp_spider.py --home _work/wxmp-spider login          # 扫码登录
$PY tools/wxmp_spider.py --home _work/wxmp-spider login --qr-only # 只到二维码（协议自检）
$PY tools/wxmp_spider.py status
$PY tools/wxmp_spider.py search "浙江大学" -n 3
$PY tools/wxmp_spider.py articles --name "浙江大学" --limit 10 --format json
$PY tools/wxmp_spider.py articles --fakeid MjM5NDgxNTQwNQ== --limit 5
$PY tools/wxmp_spider.py scrape https://mp.weixin.qq.com/s/xxxx -o _work/wechat/a.md
$PY tools/wxmp_spider.py import-legacy        # 复用 _work/wechat 已有的 cookie+token
```

比 Rust 版多出的两个实用开关：`--qr-only`（无手机时只验协议）、`import-legacy`（复用旧 cookie，
省一次扫码）。终端二维码用 PIL 渲染成半块字符，实测可扫。

## 4. 测试结果（2026-10-24 实机）

### 4.1 离线单测：29/29 通过

```
python tests/test_wxmp_spider.py -v
Ran 29 tests in 1.148s  OK
```

覆盖：token 提取（相对/绝对/缺失）、`ret` 码映射（0/200003/200013/未知/缺字段）、
searchbiz 请求形状（token、fingerprint、query URL 编码、XHR 头、编辑器 Referer）、
三层嵌套解析（含坏 `publish_info`、缺 `publish_page`、非法 JSON、空列表）、
`fakeid` 编码、HTML→Markdown（`#js_content` 抽取、`data-src` 懒加载图、加粗/斜体/链接/列表/引用、script 剔除）、
会话与 cookie 持久化往返（含兼容 `wxmp-cookies.json` 扁平格式）、fingerprint 形状、抖动区间。

### 4.2 联网实测

| # | 项目 | 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | 单篇正文 → Markdown | ✅ 通过 | 真实浙大文章 5339 字符，9 张图（`data-src` 命中），正文完整无页脚广告；产物 `_work/wechat/spider-test-article.md` |
| 2 | 扫码登录 → 二维码 | ✅ 通过 | `startlogin` 返回 `ret=0`，二维码 472×472 JPEG / 5735 B，`_work/wxmp-spider/login-qrcode.jpg`（人工确认是有效二维码） |
| 3 | 二维码轮询 | ✅ 通过 | `ask` 返回 `status=0`（等待扫描），`--max-polls 2` 正常退出 |
| 4 | 旧 cookie 调 `searchbiz` | ⚠️ 预期失败 | `_work/wechat` 里 10 月 10 日的 cookie 已过期 → `ret=200003`，被正确识别为 session 失效 |

### 4.3 没能测到的部分

**第 5–9 步（`bizlogin action=login` → token → searchbiz/appmsgpublish 的真实数据）需要一次真实扫码**，
本机没有可用的公众平台账号，因此只验证到「二维码 + 轮询」为止。
补测方式：`login` 后手机扫码，再跑 `search` / `articles` 各一次即可闭环。

## 5. 与现有 `fetch_wechat.py` 的关系

| 维度 | 现有（wxmp + 合集） | pkucli 方案 |
| --- | --- | --- |
| 凭据来源 | 手工从浏览器复制 cookie / 一整行 Cookie 头 | **扫码自取**，token 自动从 `redirect_url` 解析 |
| 列表接口 | `wxmp` 封装的 appmsg | `appmsgpublish`（`sub_action=list_ex`，带编辑器 Referer） |
| 限流 | `ret=200013` 账号级 | 同样 `200013`，**没有改善**；pkucli README 建议「等几小时」并不准确 |
| 无凭据兜底 | 有（合集源，零凭据） | 无，全链路依赖后台会话 |
| 依赖 | `wxmp`（pydantic / pandas 一长串） | `requests` + `bs4`，无额外依赖 |
| 正文 | 走自己的正文抽取 | UA 伪装 + `#js_content` + 自写 HTML→MD，可直接喂给下游 |

**建议**：不要把 `wxmp_spider.py` 塞进 `fetch_wechat.py` 主链路（不值得为一个登录方式替换掉已经跑通的采集逻辑），
而是当成两件事用：

1. **凭据自助化**：cookie 过期时用 `login` 刷一份，再 `import-legacy` 回灌给 `fetch_wechat.py`；比手工复制稳。
2. **正文按需抓**：`scrape` 已能稳定把单篇转成干净 Markdown，适合做「点进去看原文」这类二期能力。

## 6. 遗留

- 登录握手未实测（缺扫码），第 5 步失败时的错误形态未知；
- `200013` 的触发阈值没有实测（需真实账号压测）；
- 文章正文的 Markdown 里图片全是外链（`mmbiz.qpic.cn` 有防盗链，长时间后可能失效），要长期归档需另存。

## 7. 后续更正（2026-10-24）：第 9 步被平台收紧，方案需要降级定位

移植时把第 9 步（`appmsgpublish` 拉列表）当成"稳定可用、只是有频控"。**这个判断需要更正。**

**发生了什么**：2026-07-30/31 起，跨公众号拉文章列表这个**能力**被收紧——
新会话**第一页第一条**就返回 `200013`，等待不恢复、换账号不恢复。
多个互不相关的开源项目同期复现（含 12k star 的 `wechat-article-exporter` 停维）。

**先排除一个误读**：网上说"接口已被废弃/删除"是不准确的。2026-10-24 实测：

| 请求 | 结果 |
|---|---|
| `GET /cgi-bin/appmsg?action=list_ex`（无凭据） | HTTP 200，`{"base_resp":{"ret":200003,"err_msg":"invalid session"}}` |
| `GET /cgi-bin/appmsgpublish?sub=list`（无凭据） | 同上 |
| `GET /cgi-bin/appmsg_not_exist_xyz`（编造路径） | **HTTP 404 / 0 字节** |

删掉的路由是 404；被限的接口回业务码。所以**接口在，权限没了**。
判据是"分裂现象"：同一 token 下 `searchbiz` 成功、单篇正文成功、只有拉列表失败。

**对 pkucli 方案的影响**：

- 第 1–8 步（扫码登录、拿 Referer、搜 fakeid）**全部仍然有效**，工具可以继续用；
- 第 9 步降级为"有凭据时顺手试一下"，**不能作为主链路的唯一来源**；
- 因此本工具新增了第 5.5 节 `sogou` 子命令：`weixin.sogou.com` 公开索引 →
  跳转链拼接 → 签名短链 → 正文，零凭据。实测 4/4 成功。

完整证据、时间线与三条替代路的取舍写在 `docs/wxmp公众号接入.md` 的 **10.13**。

**教训**：这类"反编译后台接口"的方案，生命周期由平台单方面决定，
移植时应同时准备**一条不依赖登录态的兜底路**——pkucli 自己没有，
`fetch_wechat.py` 的合集源有，这次正是它救了场。
