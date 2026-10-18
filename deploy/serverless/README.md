# 阿里云 FC 上手指南（thu-know 定时更新）

照着做即可：**本地自检 → 打包 → 建函数 → 测试 → 加定时器**。
阿里云 OSS 侧**零第三方依赖**，不需要 Docker、不需要装 Layer、不受操作系统限制。

```
本机                              阿里云
┌────────────────────┐          ┌───────────────────────────────┐
│ package.py 打包     │  上传    │ 函数计算 FC（Python 3.10）     │
│ → dist/fc-*.zip    │ ───────► │  定时触发 → 跑 crawling →      │
└────────────────────┘          │  写回 ──► OSS Bucket          │
                                └───────────────────────────────┘
```

---

## 第 0 步：准备好云端资源（约 10 分钟）

1. **开通 OSS**，建一个 Bucket（例：`thu-know-site`），地域选离你近的（如华东1 杭州）。
   记下 **Endpoint**，形如 `oss-cn-hangzhou.aliyuncs.com`。读写权限保持「私有」即可
   （函数用 RAM 角色读写，不需要公开）。
2. **建一个 RAM 角色**（控制台 → RAM → 角色 → 创建角色 → 选「阿里云服务」→「函数计算」）：
   - 角色名例：`fc-thu-know-role`
   - 权限策略：新建自定义策略，只给它这一个 Bucket 的读写：

     ```json
     {
       "Version": "1",
       "Statement": [{
         "Effect": "Allow",
         "Action": ["oss:GetObject", "oss:PutObject"],
         "Resource": ["acs:oss:*:*:thu-know-site/*"]
       }]
     }
     ```
   > 用角色意味着**代码里不出现任何 AK/SK**。FC 绑定角色后会自动往运行环境注入
   > `ALIBABA_CLOUD_ACCESS_KEY_ID` / `_ACCESS_KEY_SECRET` / `_SECURITY_TOKEN`，
   > 自带的 OSS 客户端直接读这三个变量。

---

## 第 1 步：本地先自检（建议做，2 分钟）

**① 验证 OSS 客户端签名算法正确**（与官方 oss2 SDK 逐字节对拍 + 本地 mock 服务）：

```bash
python tests/test_oss_min.py
```

应输出 `✓ 全部通过`。这一步不联网、不碰你的桶。

**② 端到端冒烟**（可选，但能提前发现凭证/桶名问题）——在**本机**直接跑一次完整流程，
把产物写进你真实的 Bucket：

```bash
# 本机用长期 AK 调试（生产环境请改回角色授权）
export OSS_AK=你的AK
export OSS_SK=你的SK
export BUCKET=thu-know-site
export ENDPOINT=oss-cn-hangzhou.aliyuncs.com
export PREFIX=thu-know
export WORK_DIR=./_fctest        # 本地测试用，覆盖默认的 /tmp/thu-know

# 先用 SKIP_CAMPUS=1 只验证「拉基线 → 回写」链路（不抓取，秒级返回）
export SKIP_CAMPUS=1
python deploy/serverless/handler.py
```

跑完去 OSS 控制台看：`thu-know/data/` 下的 zju / sjtu / xjtu.json，以及 `thu-know/index.html` 是否在。
把 `SKIP_CAMPUS` 去掉（或改 0）再跑一次，才是真正的全量更新。Windows 下把 `export` 换成 `set`。

---

## 第 2 步：打包

```bash
python deploy/serverless/package.py
```

产出 `deploy/serverless/dist/fc-thu-know.zip`，约 **1.17 MB 压缩后 / 3.50 MB 原始 / 11 个文件**，纯代码无依赖：

```
handler.py  oss_min.py
refresh.py  fetch_campus.py  fetch_wechat.py  cloud_update.py  template.html
data/{thu,zju,sjtu,xjtu}.json      ← 种子基线
```

（`--no-seed-data` 可去掉里面的 `data/*.json`；一般不用，种子数据能让桶还空时首次运行不丢历史。
`--with-wxmp` 会额外装 wxmp —— 只有真要开 `WITH_WECHAT` 才需要，且必须在 Linux 下打包。）

---

## 第 3 步：创建函数（FC 控制台）

| 字段 | 填什么 |
|---|---|
| 创建方式 | 事件函数 |
| 函数名称 | `thu-know-update` |
| 运行环境 | **Python 3.10**（3.9/3.12 也行，脚本不挑） |
| 代码上传方式 | **ZIP 包** → 上传 `dist/fc-thu-know.zip` |
| 函数入口 | `handler.handler` |
| 内存规格 | **512 MB** 起（抓取以网络 IO 为主） |
| 超时时间 | **1800 秒**（一轮 10+ 分钟，别用默认 60 秒） |
| 执行角色 | 选第 0 步建的 `fc-thu-know-role` |
| VPC | **不要绑定**（绑了就出不了公网，除非配 NAT） |
| 实例并发度 | 建议设为 **1**，避免两次运行重叠 |

**环境变量**（函数配置 → 环境变量）：

| 变量 | 值 | 说明 |
|---|---|---|
| `STORAGE_BACKEND` | `oss` | 可省，默认就是 oss |
| `BUCKET` | `thu-know-site` | 你的桶名 |
| `ENDPOINT` | `oss-cn-hangzhou.aliyuncs.com` | 对应地域 |
| `PREFIX` | `thu-know` | 对象前缀，建议独立前缀 |
| `TZ` | `Asia/Shanghai` | **建议加**：函数默认 UTC，会影响 90 天保留期的日期判定 |
| `SKIP_CAMPUS` | `1`（**仅首次测试用**） | 跳过抓取，只验证「拉基线 → 回写」链路 |
| `WITH_WECHAT` | 空（**默认不开**） | 置 1 才跑公众号抓取。**除非你清楚代价，否则别开** —— 需要 cookies + wxmp 依赖，见下方说明 |

**负载开关**（抓不完 / 撞超时时用，都不是必须的）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `WORKERS` | 4 | 并发抓取的来源数。不同学院站是不同主机，可并行；**同一主机仍严格保持 1.2s 间隔** |
| `MAX_PER_SOURCE` | 15 | 每个来源最多取几条。调到 `8` 能显著压短单轮 |
| `RETRIES` | 2 | 单页失败重试次数。机房 IP 被封的站多时，调到 `1` 可省下退避等待 |
| `DELAY` | 1.2 | 同主机请求间隔（秒），不建议低于 1 |
| `NO_DETAILS` | 关 | 置 1 则完全不抓正文，只取标题/时间/链接，最快但正文与发布日期精度下降 |

> 实测（2026-10，本机）：26 个来源、每源 2 条时，串行 230s → 4 路并发 122s。
> 云端 95 个来源建议起步配置：`WORKERS=6`、`MAX_PER_SOURCE=8`，单轮约 5~10 分钟。

### 关于 `WITH_WECHAT`（公众号抓取）

**默认关闭，而且建议一直关着。** `fetch_wechat.py` 在云函数里有三个障碍：

1. **cookies 是账号凭据** —— 只有你浏览器登录微信公众平台才有，几天就过期。
   塞进函数环境变量等于把账号凭据长期放在云端；
2. **wxmp 依赖链** —— pydantic / pandas / requests / fake-useragent / loguru / tqdm，
   与「阿里云侧零第三方依赖」的打包方式冲突，得额外挂 Layer，且在 **Linux** 下打包
   （`python deploy/serverless/package.py --with-wxmp`）；
3. **无持久盘** —— `_work/wechat/wxmp-fakeid.json` 这类缓存每轮随沙箱销毁（不丢数据，但要重搜）。

外加微信对「拉历史文章列表」有**账号级限流**（`ret=200013`），新登录账号几乎必中，
云端无人值守时撞上了只能空等。**推荐把公众号留在本机**（手动或本机定时任务），
官网放云端 —— 两边写同一份 `data/<校区>.json`，靠「按 `i` 只增不减合并」天然可交替进行
（`refresh.import_items` 会保留云端不认识的 `wx:` 来源）。

> ⚠️ **运行环境别选太新的 3.13+**：脚本虽只依赖标准库，但 FC 的主流镜像是 3.9/3.10/3.12，
> 用版本对齐的运行时最稳。

---

## 第 4 步：测试运行

1. 控制台点「**测试函数**」，事件内容填 `{}`，执行。
2. 看返回 JSON：`{"ok": true, "uploaded": [...]}` 即成功。
3. 看日志：应能看到 `基线数据：0/4 个文件`、`cloud_update.py 退出码 0`、`回写完成`。
4. **回 OSS 控制台**确认 `thu-know/` 前缀下出现了 `index.html` 与 `data/*.json`。

首次用 `SKIP_CAMPUS=1` 跑通后，把该变量删掉（或改 0），再测一次全量——确认三校
都能抓通（这一步约 10 分钟，正好用来看会不会撞超时）。

---

## 第 5 步：加定时触发器

函数 → 触发器 → 创建触发器 → **定时触发器**：

| 字段 | 值 |
|---|---|
| 触发方式 | 定时触发 |
| Cron 表达式 | `0 0 8,20 * * *`（每天 08:00 和 20:00） |
| 时区 | Asia/Shanghai |
| 触发消息 | `{}` |
| 启用 | 是 |

Cron 是 6 段：`秒 分 时 日 月 周`。例如只要工作日：`0 0 8 * * 1-5`。

---

## 第 6 步：把站点发布成在线链接

> **先看这里**：阿里云 OSS 的默认域名会**强制下载 HTML**（服务端加 `Content-Disposition: attachment`），
> 浏览器无法渲染 —— 这是阿里云的硬性策略，想渲染**必须绑已备案的自定义域名**（内地桶）。
> 所以发布有两条路：
> - **路 A（推荐，免备案）**：把 `index.html` 发布到腾讯云 EdgeOne Pages，默认域名直接渲染，见下方「路 A」；
> - **路 B（需备案）**：域名购买 + ICP 备案（2~4 周）后绑定到 OSS，按本节原有步骤配置。

### 路 A：发布到 EdgeOne Pages（免备案，今天就能上线）

1. 登录腾讯云 EdgeOne Pages 控制台（console.tencentcloud.com/edgeone/pages，免费开通）；
2. 「API Token」页 → 创建 Token（设个一年有效期即可）；
3. 本机执行（需 Node.js ≥18）：

   ```bash
   set EDGEONE_TOKEN=你的token
   python deploy/edgeone/deploy_edgeone.py
   ```

4. 输出的 `https://thu-know.edgeone.app`（或类似）就是**可直接访问的网址**，浏览器正常渲染。

**自动化**：本机定时任务里接在更新命令后面即可全链路无人值守：

```bash
python cloud_update.py && python deploy/edgeone/deploy_edgeone.py
```

> 云函数（FC）里跑不了 EdgeOne CLI（Node 工具），所以 EdgeOne 的部署放在本机侧；
> FC 继续负责抓取并回写 OSS（数据基线不受影响）。

### 路 B：绑定已备案域名到 OSS（保留原方案）

函数每轮跑完会把 `index.html` 写到桶里（PREFIX 下 + **桶根各一份**，后者是为了静态托管），
按下面配一次，之后每次自动更新都直接生效：

1. **开公共读**：桶 → 权限管理 → 读写权限 → **公共读**
   （任何人可下载、不可写删；别选「公共读写」）。
2. **开静态托管**：桶 → 数据管理 → **静态页面**：
   - 默认首页填 `index.html` —— **只认根目录文件，不允许带 `/`**；
     handler 已把首页镜像到桶根（`SITE_ROOT_INDEX=0` 可关闭），PREFIX 保持 `thu-know` 不受影响；
   - 默认 404 页留空即可。
3. 保存后页面会给出访问域名，形如
   `https://<bucket>.oss-cn-beijing.aliyuncs.com/`，打开即最新页面。
4. **验证更新生效**：等定时器跑完一轮后 Ctrl+F5 强刷（`index.html` 数据内嵌，浏览器可能缓存）。

> 没绑 CDN 就不用管缓存；以后加了 CDN，把 `index.html` 的缓存 TTL 设短（如 5 分钟）或每轮刷缓存。
> 不想开静态托管的话，直接访问 `https://<bucket>.oss-cn-beijing.aliyuncs.com/thu-know/index.html` 也能看（公共读前提下）。

---

## 排错表

| 现象 | 原因与处理 |
|---|---|
| `缺少 OSS 凭证` | 函数没绑执行角色，或本机没设 `OSS_AK/OSS_SK` |
| `HTTP 403 SignatureDoesNotMatch` | 极少见（算法已与官方 SDK 对拍）；确认 `ENDPOINT` 与 Bucket 地域一致 |
| `HTTP 403 AccessDenied` | 角色策略的 `Resource` 没覆盖该 Bucket/Prefix |
| `HTTP 403 RequestTimeTooSkewed` | 容器时钟偏差 >15 分钟，罕见；重试即可 |
| `请求失败 ... 502` / 大量源失败 | 出口是机房 IP，个别 `http://` 学院站被拦。属预期，源会标记 `failed` 不影响整轮；可在 `_work/sources-<校区>.json` 换 `https` 地址 |
| 执行超时 | 先调负载：加 `WORKERS=6`、`MAX_PER_SOURCE=8`、`RETRIES=1`（抓取器已支持来源级并发，实测能压掉一半以上耗时）；仍超时再把浙大单独拆成一个函数（它最慢，站群 WAF 限流），各挂各的定时器 |
| `index.html` 没出现 | 抓取两条命令都失败了；看日志里 `cloud_update.py 退出码` |
| 站点显示旧数据 | 开了 CDN 没刷缓存，见第 6 步 |

---

## 文件速查

| 文件 | 作用 |
|---|---|
| `handler.py` | 函数入口：复制代码到可写目录 → 拉基线 → 跑更新 → 回写 OSS |
| `oss_min.py` | 自带的标准库 OSS 客户端（V1 签名，已与官方 SDK 对拍） |
| `package.py` | 打包成可上传的 ZIP |
| `../../tests/test_oss_min.py` | 签名与读写自检（不联网） |
| `../../cloud_update.py` | 一条命令跑完两轮抓取，只输出摘要 |
