# A 路线可行性：安卓副号 + mitmproxy 抓 `wap_sid2`

> 目标：给定一个公众号，拿到它**最近 90 天全部新发布文章的标题 / 日期 / 链接**。
> 评估对象：`tools/wxmp_spider.py history` 的前置步骤——用**安卓副号 + mitmproxy** 抓一份
> 真实微信客户端的 `wap_sid2`。
> 本文所有"实测"均为 **2026-10-24** 在本机（Windows 11 / 26200，Python 3.13.14）跑出来的；
> 标注"外部经验"的条目来自公开项目文档，未自测。**没有手机，所以三个核心闸门仍未闭合。**

---

## 1. 结论摘要

| 问题 | 结论 |
|---|---|
| 技术上能不能做 | **能**。mitmproxy 在 Windows 上装好即可用，`--mode wireguard` 能起（用户态 WireGuard，**不需要 root、不需要改系统代理**）；抓包插件已写好，端到端跑通 |
| 它解决什么 | 把「微信客户端发出的那次 `mp/profile_ext` 请求」原样记下来：Cookie 里的 `wap_sid2` + URL 里的 `uin/key/pass_ticket/appmsg_token`。这是**唯一能枚举某号全部已发布文章**的入口（文档 10.15） |
| 真正的风险在哪 | 不在 mitmproxy，在三个**外部未知**：① 安卓非 root 时微信是否信任用户 CA；② 副号（未关注目标号）能否拿到 `wap_sid2` 并触发 `getmsg`；③ 2026-07-30 的「拉列表收紧」是否波及 `getmsg` |
| 成本 | 首次 20~30 分钟（装工具 + 信任证书），之后每次抓包 3~5 分钟；**每次抓到的凭据只有 30 分钟~3.5 小时寿命** |
| 值不值得做 | 只在「**必须不漏篇**」时值得。若只要能"看到热点"，零凭据三路（合集 + 搜狗 + 内链）就够，不必碰抓包 |
| 现在的状态 | 工具链就绪 + 离线测试全绿；**缺一次真机验证**（30 分钟可判生死，见 §6） |

一句话：**A 路线已经"从想法变成一条命令"，但"能不能真的列出列表"还差一次手机实测。**

---

## 2. 为什么非要抓这个 cookie

`mp/profile_ext?action=getmsg` 的行为（2026-10-24 复测，`probe` 子命令）：

```json
{"base_resp":{"ret":-3,"errmsg":"no session","cookie_count":1},"ret":-3}
```

- 它**不要后台 token、不验签名、不需要扫码登录公众平台**；
- 它只认「微信客户端的会话」——也就是 Cookie 里那串 `wap_sid2`；
- 同一台机器上，塞一个**假** `wap_sid2`，服务端照样数到 1 个 cookie，但仍回 `-3`。
  ⇒ 关键不是"有没有 cookie 这个字段"，而是**这个值是不是服务端认的真实会话**。

对比一下取数能力的边界：

| 路径 | 覆盖 | 能否"枚举某号全部" | 代价 |
|---|---|---|---|
| 官网 / RSS（现主链路） | 校内新闻为主 | 不适用 | 0 |
| 搜狗微信 | 关键词命中，实测 20 条里只有 3 条属于目标号 | ❌ | 0，但有验证码 |
| 公众号合集 `appmsgalbum` | 只有建了合集的号 | ❌（只覆盖合集内） | 0 |
| 后台 `appmsgpublish` | 全量 | ✅ | 需公众平台号 + **2026-07-30 起仍 200013** |
| **`profile_ext?action=getmsg`** | **全量** | **✅** | **一份真 `wap_sid2`** |

---

## 3. 方案形状：谁抓、抓什么、怎么用

```text
 ┌──────────────┐   WireGuard 隧道（UDP 51820）   ┌────────────────────┐
 │ 安卓副号微信  │ ───────────────────────────────▶│  PC: mitmproxy      │
 │ （登录态）    │     全部 IPv4 流量              │  --mode wireguard   │
 └──────────────┘                                 │  + wap_sid2_capture │
        │                                          └─────────┬──────────┘
        │ 打开目标号任一文章 / 查看历史消息                   │ 明文 HTTPS
        ▼                                                    ▼
   mp.weixin.qq.com  ◀──────────────────────────────  自动落盘：
   ?action=home / getmsg                              _work/wechat/wxmp-wap-cookies.txt
                                                      _work/wechat/wxmp-wap-params.json
                                                              │
                                                              ▼
                                        python tools/wxmp_spider.py history --name 浙江大学
```

两个关键点，也是 mitmproxy 相对"手机装 Reqable"的优势与代价：

1. **必须走 VPN 模式，不能走"Wi-Fi 代理"**。微信部分版本绕过系统 HTTP 代理，代理式抓包会一条请求都看不到。mitmproxy 的 `--mode wireguard` 是系统层 TUN，绕过不了——这正是选它的理由。
2. **证书要装到手机上，而且必须被微信信任**。这是整条路上唯一"靠运气"的环节（见 §5.1）。

---

## 4. 本机实测（已完成，2026-10-24）

| 项 | 结果 | 证据 |
|---|---|---|
| mitmproxy 安装 | **12.3.0**（Python 3.13.14，Windows 11 26200） | `mitmdump --version` |
| WireGuard 模式可用 | ✅ 能启动，自动生成 `~/.mitmproxy/wireguard.conf` 与 CA 证书（`mitmproxy-ca-cert.pem/.cer/.p12`） | `mitmdump --mode wireguard --set wireguard_listen=0.0.0.0:51833` |
| 抓包插件可用 | ✅ 真代理链路跑通：假 `wap_sid2` → 自动写 cookie / 参数 / 日志，并打印闸门判定 | 见下 |
| 代理转发保真 | ✅ 直连与经代理，服务端返回一致（`ret=-3`，`cookie_count=1`） | curl 对比 |
| `cookie_count` 可靠性 | ⚠️ 同参数下出现过 0 和 1 → **不能把它当作"cookie 有没有送到"的唯一判据**，以 `getmsg` 的 `ret` 为准 | 4 次重复 |

端到端实跑的日志（假 cookie，故意让它失败）：

```text
[✓] 抓到含 wap_sid2 的 cookie → _work/wechat/wxmp-wap-cookies.txt
    rewardsn=1; wap_sid2=FAKEVA…
[gate3] ❌ getmsg → ret=-3 no session：cookie 里没有真 wap_sid2，或 uin/key 不配套
```

这说明**管道通了**：真机上只要微信发出那条请求，剩下的全是自动的。

---

## 5. 四道闸门：每一道怎么判、失败怎么办

按顺序过，**任何一道不过就不要往下走**（省时间，也少暴露）。

### 5.1 闸门 1：证书能不能被信任（决定 A 路线生死）

| 项 | 说明 |
|---|---|
| 判据 | 手机开隧道后，在微信里打开任意公众号文章：<br>① 文章能正常显示；② mitmproxy 里出现**明文** `GET https://mp.weixin.qq.com/s?__biz=...`（不是 `Tunnel to ...:443`，也没有 TLS 报错） |
| 为什么可能失败 | **Android 7 起，App 默认不信任"用户证书"**（只信系统证书区），`targetSdk ≥ 24` 的 App 全部如此。把 `mitm.it` 的证书装成"用户凭据"**不等于**微信会信它 |
| 现实的三种结果 | ① 微信这版信任用户证书 → 直接过（社区里 Reqable 类工具能抓，说明这条路在部分版本成立）；② 只对内置 WebView 生效、原生网络库不生效 → 也能过（我们要的正是 WebView 的 `mp.weixin.qq.com`）；③ 完全不信任 / 有 pinning → 文章白屏或一直转圈，**A 路线在非 root 安卓上到此为止** |
| 失败出路 | ① 换一台 **Android 8~13 的旧机**（新系统更严，不是更松）；② 安卓模拟器（MuMu / 夜神，自带 root，可把 CA 装进**系统证书区**）；③ **改走 PC 微信 + `--mode local`**（见 §5.5）；④ root 真机装系统证书（副号场景下不推荐，性价比低） |

> 这一条的判断成本极低：装证书 + 打开一篇文章，2 分钟见分晓。**先做它，别先写代码。**

### 5.2 闸门 2：Cookie 里有没有 `wap_sid2`

| 项 | 说明 |
|---|---|
| 判据 | 插件打印 `[✓] 抓到含 wap_sid2 的 cookie`；或手动看请求头 `Cookie:` 里有 `wap_sid2=` |
| 失败形态 | 只有 `rewardsn` / `wxtokenkey` / `pac_uid` 这类**游客 cookie** → 说明微信没登录，或抓的是未登录的内置浏览器请求 |
| 处理 | 确认副号**已登录且已完成新手引导**；杀掉微信进程重进；先打开一篇文章再点「查看历史消息」 |

补充：`wap_sid2` 通常是**打开文章页时由响应 `Set-Cookie` 下发的**（公开项目
`wechat-article-exporter` 的 credentials 文档就是这么取的）。所以插件同时看请求头和
响应头，两处都收。

### 5.3 闸门 3：`getmsg` 到底给不给数据（**最关键的未知**）

| 子项 | 说明 |
|---|---|
| 3a 接口可用性 | 判据：插件打印 `[gate3] ✅ getmsg → ret=0，本页 N 条群发`。若 `-3` 说明 cookie 不被认；若 `200013` 说明**收紧波及到了这里**，A 路线的价值立刻归零（那就退回零凭据抽样） |
| 3b 未关注能不能枚举 | **未验证**。理论上 `__biz` 是参数，与"是否关注"无关；但外部有"未关注只看最近 10 篇"的说法。测法：副号**不关注**目标号，直接 `history --name X -n 20`，看能否翻到第 2 页（`can_msg_continue=1`）；翻不动就先关注再测。**这一步决定"副号要不要关注一堆号"** |
| 3c 时效 | 外部经验：`key` / `pass_ticket` / `appmsg_token` 约 **30 分钟 ~ 3.5 小时**过期；`wap_sid2` 本身是会话级。过期后整条请求作废，需重抓 |
| 3d 翻页上限 | 外部经验：`offset` 上限约 1000（≈100 页 / 1000 次群发）。本项目只要 90 天，约 9~15 页/号，**碰不到这个墙** |

### 5.4 闸门 4：一次凭据够不够跑完全量（算术题）

| 量 | 数 |
|---|---|
| 目标号 | 约 40 个（浙大 / 上交 / 西交） |
| 90 天页数 | 9~15 页/号（按每天 1~2 次群发估） |
| 总请求数 | 40 × 12 ≈ **480 次** |
| 1.5s 抖动下耗时 | ≈ 12 分钟（加超时与重试按 20 分钟算） |
| 凭据寿命 | 30 分钟~3.5 小时 |

结论：**卡在"30 分钟"这一档会很紧，3.5 小时这档很宽松。** 稳妥做法：
- `--delay-ms 800`（仍是人类速度）；
- **按校区分批**（一次 12~14 个号，约 4 分钟），任一批失败只重抓一次；
- 抓包时就顺手把最想拿的 1~2 个号先跑掉（`-n 5`），先确认闸门 3，再铺开。

### 5.5 备选：PC 微信 + `--mode local`（可能是更省事的一条）

mitmproxy 12 的 `local` 模式用系统级 API 抓**本机进程**流量，Windows 可用，
**不用改系统代理**——这正好避开文档 10.16 里"系统代理被 `127.0.0.1:7897` 抢走、
形成双层代理"的坑：

```bash
mitmdump -s tools/wap_sid2_capture.py --mode local
```

- 证书优势：Windows 装进「受信任的根证书颁发机构」，微信内置浏览器 `WeChatAppEx.exe`
  是 Chromium 分支（XWeb），**默认信任系统根证书**，比安卓用户证书稳；
- 待验证：PC 微信的请求里**有没有** `wap_sid2`。旁证是 `wechat-article-exporter` 的文档
  明确写"用电脑端微信打开目标公众号文章 → 从响应里复制 `wap_sid2`"，说明 PC 端确实会下发；
- 风险更低：不碰手机、不需 root、证书用完从 `certmgr.msc` 删掉即可。

> 如果 PC 端闸门 1、2 都过，**优先用 PC 端**；安卓副号是"PC 端拿不到 `wap_sid2`"时的退路。
> 这也修正了文档 10.16 里"PC 端能不能成还未知"的说法：工具层面已就绪，缺的是一次实测。

---

## 6. 真机验证流程（30 分钟，按顺序，随时可停）

### 6.1 准备（一次性，约 15 分钟）

```bash
# 1. 装 mitmproxy（本项目已装在隔离环境；也可 pipx install mitmproxy）
C:/Users/Kevin/.workbuddy/binaries/python/envs/default/Scripts/mitmdump.exe --version

# 2. 起抓包（WireGuard 模式；安卓走这条）
C:/Users/Kevin/.workbuddy/binaries/python/envs/default/Scripts/mitmweb.exe \
  -s tools/wap_sid2_capture.py --mode wireguard

# 3. 手机会员：装 WireGuard App → 扫 mitmweb 页面上的二维码 → 打开隧道
# 4. 手机浏览器开 http://mitm.it → 下载 Android 证书 → 设置里安装为「VPN 和应用 / CA 证书」
```

### 6.2 验证（每次约 5 分钟）

```text
5. 副号登录微信（不要用主号）
6. 微信里打开目标号任意一篇文章（可以从「搜一搜」进，不必关注）
7. 右上「…」→「查看公众号」→「查看历史消息」→ 在列表里往下滚 2 屏
8. 看 PC 终端：
   [✓] 抓到含 wap_sid2 的 cookie        ← 闸门 2 过
   [gate3] ✅ getmsg → ret=0，本页 10 条  ← 闸门 3 过（决定性）
9. 立刻收口（两分钟窗口内）：
   python tools/wxmp_spider.py history --name 浙江大学 -n 5
   python tools/wxmp_spider.py history --name 浙江大学 --since 2026-07-26 -o _work/wechat/history-zju.json
```

> 插件已经把 `uin/key/pass_ticket/appmsg_token/__biz` 写进
> `_work/wechat/wxmp-wap-params.json`，`history` 会自动读，**不用再手敲一长串参数**。

### 6.3 收尾（必须做）

```text
10. 关 WireGuard 隧道；关 mitmweb
11. 手机「加密与凭据 → 用户凭据」删掉 mitmproxy CA
12. 删掉 _work/wechat/wxmp-wap-cookies.txt 与 wxmp-wap-params.json（用完即焚）
```

---

## 7. 各闸门失败的降级路线

| 失败点 | 现象 | 下一步 |
|---|---|---|
| 闸门 1（安卓证书） | 微信文章白屏 / 一直转圈 / mitmproxy 全是 TLS 错误 | 换 PC `--mode local`；或安卓模拟器（root + 系统证书）；或旧安卓机 |
| 闸门 2（无 `wap_sid2`） | 只有游客 cookie | 确认副号登录态；先打开文章再进历史；杀进程重进 |
| 闸门 3a（`-3`） | 有 cookie 但 `getmsg` 拒绝 | 核对 `uin/key/pass_ticket` 是否与 cookie 同一次会话；重抓；确认微信 UA |
| 闸门 3a（`200013`） | 被限/能力收紧 | **A 路线作废**，回到零凭据三路（搜狗 + 合集 + 内链），接受"抽样" |
| 闸门 3b（只给 10 篇） | 翻页 `can_msg_continue=0` | 副号关注该号再测；批量场景则改为"关注清单"（约 40 个号，可控） |
| 闸门 4（跑不完） | 中途 `-3` | 按校区分批；`--delay-ms 800`；先跑重点号 |

---

## 8. 风险清单（按"会不会出事"排序）

| 风险 | 具体表现 | 降到最低 |
|---|---|---|
| **副号被限/被封** | 高频拉列表被判异常，轻则功能限制，重则封号 | 只用副号；每号每分钟 ≤ 1 次翻页；一批 12~14 个号；不 7×24 挂着 |
| **cookie 泄露 = 账号被接管** | `wap_sid2` + `uin` 足以冒充会话 | 只存本机；`_work/wechat/wxmp-wap-cookies*.txt` 已在 `.gitignore`；**不发截图、不进仓库**；用完删 |
| **根证书残留** | 装了根证书后，任何持有该私钥的程序都能解本机 HTTPS | 手机关隧道后删证书；Windows 用 `certmgr.msc` 删；**别长期留着** |
| **代理/隧道残留** | 抓完忘关 → 手机或整机断网 | 先关隧道再关 mitmweb；确认手机能正常上网 |
| **验证墙 / IP 被判** | `getmsg` 返回 HTML「验证」页 | 撞到就停 30 分钟；换出口（手机热点）；降低频率 |
| **版权与合规** | 批量抓全文再分发 = 转载 | 只存**标题 + 日期 + 链接**；正文按需单篇抓，不做全文镜像 |

推荐用法：**一次性使用**。抓一次 → 跑完这一轮 → 删 cookie。账号暴露窗口是几分钟，不是整月。

---

## 9. 工具：这次补上的东西

| 文件 | 作用 |
|---|---|
| `tools/wap_sid2_capture.py` | **新增**。mitmproxy 插件：从 `mp.weixin.qq.com` 流量里合成 Cookie、抽 5 个参数、自动落盘、实时打印闸门判定。不 import mitmproxy（flow 用鸭子类型），所以能离线测 |
| `tools/wxmp_spider.py` | `history` 支持 `--appmsg-token`、`--params`，并**默认自动读** `_work/wechat/wxmp-wap-params.json`；`fetch_history_page` 真的把 `appmsg_token` 带上；`cookie-from-curl` 也认 `appmsg_token` 并把参数落成 JSON |
| `tests/test_wap_capture.py` | **新增** 20 例离线测试：URL 参数解码、Set-Cookie 属性剥离、Cookie 合成顺序/覆盖、掩码、`getmsg` 判定、假 flow 落盘、非微信域名忽略、重复 cookie 不刷屏 |
| `tests/test_wxmp_spider.py` | 新增 5 例：`appmsg_token` 解析、参数文件读取/脏数据、`getmsg` 请求参数断言（92 例全绿） |
| `docs/A路线-wap_sid2抓包可行性.md` | 本文 |

插件自检（无需 mitmproxy）：

```bash
python tools/wap_sid2_capture.py --selftest
# params: {'uin': 'MTIz=', 'key': 'abc', ...}
# cookie: rewardsn=1; wap_sid2=NEW; pac_uid=9 | has_sid2: True
# verdict: ('ok', 'ret=0，本页 1 条群发 —— 闸门 3 通过，可以跑 history')
```

---

## 10. 边界：本文没有证明的事

1. **微信是否信任 mitmproxy 的用户证书**——没有真机，未测。这是 A 路线的生死点（闸门 1）。
2. **副号未关注目标号时 `getmsg` 能否翻页**——未测（闸门 3b）。外部资料互相矛盾。
3. **2026-07-30 的收紧是否波及 `getmsg`**——`getmsg` 目前返回的是 `-3`（要会话）而不是 `200013`（被限），**迹象是没波及**，但只有真 cookie 才能证明。
4. **PC 微信是否有 `wap_sid2`**——只有第三方项目文档的旁证，未自测。
5. **`history` 还没接进 `refresh.py` 主链路**——目前是"拿到 JSON 手动看"，
   要把公众号来源并入站点，还需要一个 `history JSON → data/*.json` 的适配层（下一步再做）。

---

## 11. 一句话决策

- **要"不漏一篇"** → 必须做 A；先花 2 分钟测闸门 1，过了就基本成了。
- **能接受"抽样"** → 不做 A，维持现有三路兜底，省下所有风险与维护。
- **想长期自动化** → A 不合适（凭据寿命 30 分钟~3.5 小时，必须人肉重抓）。
  长期方案仍是 we-mp-rss 那类"读书模式 + 定时扫码"，代价是每 3 天扫一次码。
