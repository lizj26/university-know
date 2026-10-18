#!/usr/bin/env python3
"""thu-know 云函数入口（阿里云函数计算 FC / 腾讯云云函数 SCF 通用）。

DeepSeek 那份方案给了骨架，但漏了本项目最关键的一步：
index.html 是把数据**内嵌**进去的单文件（<script id="notice-data">），
所以「把 data/*.json 写进 OSS/COS」并不会让网站更新——必须重跑 build
并把 **index.html 本身**上传。这个 handler 就是替你把整条链路包好。

每次触发做四件事：
  1. 把函数包里的代码（只读）复制一份到可写目录 /tmp/thu-know
  2. 从对象存储拉上一轮的 data/*.json 作为「只增不减」的基线
  3. 运行 cloud_update.py（内部依次跑 refresh.py + fetch_campus.py，重建 index.html）
  4. 把 data/*.json 与 index.html 回写对象存储（覆盖同名对象）

环境变量
--------
STORAGE_BACKEND   oss | cos，留空默认 oss（OSS 后端零依赖，COS 才需要 SDK）
BUCKET            存储桶名
ENDPOINT          区域 Endpoint，如 oss-cn-hangzhou.aliyuncs.com / cos.ap-guangzhou.myqcloud.com
PREFIX            对象前缀，默认 thu-know（建议把项目放在独立前缀下）
SKIP_REFRESH      置 1 则传 --skip-refresh 给 cloud_update.py（清华已改为外链，该开关现为
                  空操作，保留以兼容旧配置）
SKIP_CAMPUS       置 1 则跳过三校（等价 --skip-campus）
WITH_WECHAT       置 1 则额外跑微信公众号（等价 --with-wechat）——**默认关闭**，见下
MAX_PER_SOURCE    每个来源最多取多少条（默认 15；调小可显著压短单轮耗时）
MAX_WECHAT        每个来源最多抓多少篇公众号文章（默认 8）
WORKERS           并发抓取的来源数（默认 4；不同主机并行、同主机仍限速）
DELAY             同一主机两次请求的最小间隔秒数（默认 1.2，不建议低于 1）
RETRIES           单页失败重试次数（默认 2；云端网络受限时可设 1 压缩耗时）
NO_DETAILS        置 1 则不抓正文，只取标题/时间/链接（最快，但正文与发布日期精度下降）

为什么公众号默认不开
--------------------
`fetch_wechat.py` 需要两样本机资源，云函数里都不成立：
  ① **登录后的 cookies** —— 只有你自己浏览器登录公众平台才有，且几天就过期；
     把 cookies 塞进函数环境变量等于把账号凭据长期放在云端，得不偿失；
  ② **wxmp 依赖链**（pydantic / pandas / requests / fake-useragent / …）——
     与本项目「阿里云侧零第三方依赖」的打包方式冲突，要额外挂 Layer。
无状态函数里也没有持久盘存 `_work/wechat/wxmp-fakeid.json` 这类缓存。
所以公众号抓取留在**本机**做（本机定时任务 / 手动跑），云端只负责官网。
真要开，需自备 cookies 文件并自带依赖，再把 WITH_WECHAT 置 1。

凭证：**优先用云平台的执行角色**。阿里云 FC 绑定 RAM 角色后会自动注入
ALIBABA_CLOUD_ACCESS_KEY_ID / _ACCESS_KEY_SECRET / _SECURITY_TOKEN 三个环境变量，
oss_min 直接读它们，无需 SDK、无需明文 AK。本地调试或临时测试可设
OSS_AK / OSS_SK（腾讯云侧为 COS_SECRET_ID / COS_SECRET_KEY），生产请改回角色授权。

依赖：阿里云 OSS 侧**零第三方依赖**（走自带的 oss_min.py 标准库客户端）；
腾讯云 COS 侧需要 cos-python-sdk-v5，且**必须在 Linux 环境打包**
（SDK 含平台相关二进制，Windows 打包的包在 FC/SCF 上跑不起来）。
"""

import os
import re
import shutil
import subprocess
import sys
import time
import traceback

# 让本文件所在目录进入模块搜索路径：这样 oss_min.py 无论跟 handler.py 一起放在
# 函数根目录（FC 上传后）还是放在 deploy/serverless/（本地调试）都能被导入。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# --- 路径 ---------------------------------------------------------------
# 函数包的代码目录因平台而异，逐个探测；本目录应是只读的。
CODE_CANDIDATES = ("/var/task", "/var/user", "/code", "/opt/code", os.getcwd())
# 唯一可写的只有 /tmp；本地冒烟测试可用环境变量 WORK_DIR 覆盖成任意可写目录。
WORK_DIR = os.environ.get("WORK_DIR") or "/tmp/thu-know"

# 必须随函数包一起上传的文件（crawler 自身零第三方依赖，全靠标准库）
# 注意：oss_min.py 不在其中——它只被 handler 进程使用，抓取子进程用不到。
# fetch_wechat.py 里带上是为了让 WITH_WECHAT=1 时能找到入口；它只有在
# 真的被调用（且给了 cookies）时才会 import requests/wxmp。
CODE_FILES = ("refresh.py", "fetch_campus.py", "fetch_wechat.py",
              "cloud_update.py", "template.html")
# 需要「拉下来当基线 / 回写上去」的数据文件
DATA_DIR = "data"
SYNC_DOWN = ("data/zju.json", "data/sjtu.json", "data/xjtu.json")
# index.html 只内嵌「轻量索引」，正文与切校区所需的完整数据在 data/shard/*.json 里，
# 所以分片必须跟 index.html 一起回写，否则线上页面点进去取不到正文。
SHARD_FILES = (
    "data/shard/zju.json", "data/shard/zju-d.json",
    "data/shard/sjtu.json", "data/shard/sjtu-d.json",
    "data/shard/xjtu.json", "data/shard/xjtu-d.json",
)
SYNC_UP = ("data/zju.json", "data/sjtu.json", "data/xjtu.json", "index.html") + SHARD_FILES
# 抓取的完整日志也回传,某一轮失败时可以从桶里取回现场(存在才传)
SYNC_UP_LOG = "_work/run.log"


def log(msg):
    print(f"[thu-know] {msg}", flush=True)


def find_code_dir():
    for cand in CODE_CANDIDATES:
        if cand and os.path.isfile(os.path.join(cand, "cloud_update.py")):
            return cand
    raise RuntimeError(
        "找不到 cloud_update.py，请确认 refresh.py / fetch_campus.py / "
        "cloud_update.py / template.html 都随函数包一起上传了"
    )


# --- 对象存储：两个后端，接口一致 --------------------------------------
class OssBackend:
    """阿里云 OSS —— 走自带的 oss_min.py（纯标准库）。

    为什么不用官方 oss2：它会拖进 requests / cryptography / pycryptodome 等
    900+ 个文件、20+ MB，且含平台相关二进制（Windows 上打出来的 .pyd 在
    Linux 的 FC 里无法加载）。我们只需要"读一个对象/写一个对象"，用标准库
    自签名即可，函数包保持纯代码。签名算法已与官方 SDK 对拍通过。
    """

    def __init__(self, bucket, endpoint, prefix):
        from oss_min import OSSClient  # 与 handler.py 同目录

        self.client = OSSClient(bucket, endpoint)
        self.prefix = prefix.strip("/")

    def _key(self, rel):
        return f"{self.prefix}/{rel}" if self.prefix else rel

    def download_if_exists(self, rel, dest):
        data = self.client.get(self._key(rel))
        if data is None:
            return False
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(data)
        return True

    def upload(self, rel, src, content_type="application/octet-stream"):
        with open(src, "rb") as fh:
            self.client.put(self._key(rel), fh.read(), content_type=content_type)

    def upload_root(self, name, src, content_type="application/octet-stream"):
        """上传到桶根目录（无视 PREFIX），供静态网站托管的默认首页使用。"""
        with open(src, "rb") as fh:
            self.client.put(name, fh.read(), content_type=content_type)


class CosBackend:
    """腾讯云 COS（依赖 cos-python-sdk-v5）"""

    def __init__(self, bucket, endpoint, prefix):
        from qcloud_cos import CosConfig, CosS3Client

        sid, skey = os.environ.get("COS_SECRET_ID"), os.environ.get("COS_SECRET_KEY")
        token = os.environ.get("COS_SESSION_TOKEN")
        # 无显式密钥时，走函数运行角色（CAM）——SCF 会自动注入临时凭证
        cfg = CosConfig(Region=endpoint.split(".")[1] if "." in endpoint else endpoint,
                        SecretId=sid, SecretKey=skey, Token=token)
        self.client = CosS3Client(cfg)
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def _key(self, rel):
        return f"{self.prefix}/{rel}" if self.prefix else rel

    def download_if_exists(self, rel, dest):
        from qcloud_cos import CosServiceError

        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=self._key(rel))
            resp["Body"].get_stream_to_file(dest)
            return True
        except CosServiceError as exc:
            if exc.get_status_code() == 404:
                return False
            raise

    def upload(self, rel, src, content_type="application/octet-stream"):
        with open(src, "rb") as fh:
            self.client.put_object(Bucket=self.bucket, Key=self._key(rel), Body=fh,
                                   ContentType=content_type)

    def upload_root(self, name, src, content_type="application/octet-stream"):
        """上传到桶根目录（无视 PREFIX）。"""
        with open(src, "rb") as fh:
            self.client.put_object(Bucket=self.bucket, Key=name, Body=fh,
                                   ContentType=content_type)


def make_backend():
    backend = (os.environ.get("STORAGE_BACKEND") or "").strip().lower()
    if not backend:
        # OSS 后端已改用自带的标准库客户端（oss_min），不需要任何第三方依赖，
        # 所以默认走 oss；只有显式 STORAGE_BACKEND=cos 时才用腾讯云 SDK。
        backend = "oss"
    bucket = os.environ["BUCKET"]
    endpoint = os.environ.get("ENDPOINT", "")
    prefix = os.environ.get("PREFIX", "thu-know")
    log(f"存储后端 {backend}，bucket={bucket}，prefix={prefix or '(根)'}")
    return (OssBackend if backend == "oss" else CosBackend)(bucket, endpoint, prefix)


# --- 主流程 -------------------------------------------------------------
def prepare_workdir(code_dir):
    """把只读代码复制进 /tmp，并建好空的 data/ 目录。"""
    if os.path.isdir(WORK_DIR):
        shutil.rmtree(WORK_DIR)
    os.makedirs(os.path.join(WORK_DIR, DATA_DIR), exist_ok=True)
    for name in CODE_FILES:
        src = os.path.join(code_dir, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(WORK_DIR, name))
        else:
            log(f"⚠ 缺少代码文件 {name}（若跳过对应步骤可忽略）")
    # 首轮基线：若函数包里带了 data/*.json（打包时从本机带上的），先铺进去，
    # 这样桶还是空的时候第一次运行也不会丢历史；随后 pull_baseline 会用桶里的更新版本覆盖。
    seed = os.path.join(code_dir, DATA_DIR)
    if os.path.isdir(seed):
        seeded = 0
        for name in sorted(os.listdir(seed)):
            if name.endswith(".json"):
                shutil.copy2(os.path.join(seed, name), os.path.join(WORK_DIR, DATA_DIR, name))
                seeded += 1
        if seeded:
            log(f"函数包内种子数据：{seeded} 个 data/*.json")
    # 顺带带上打包时的 index.html 快照：万一这一轮抓取命令都被跳过，
    # 也还能把上一次的页面发布出去，不至于没有产物。
    snapshot = os.path.join(code_dir, "index.html")
    if os.path.isfile(snapshot):
        shutil.copy2(snapshot, os.path.join(WORK_DIR, "index.html"))
    # 可选：资料库里手工维护的补充信息源
    work_sources = os.path.join(code_dir, "_work")
    if os.path.isdir(work_sources):
        shutil.copytree(work_sources, os.path.join(WORK_DIR, "_work"), dirs_exist_ok=True)
    return WORK_DIR


def pull_baseline(store):
    """拉上一轮数据；沙箱/函数都是无状态的，这一步决定了「只增不减」是否成立。"""
    got = []
    for rel in SYNC_DOWN:
        dest = os.path.join(WORK_DIR, rel)
        try:
            if store.download_if_exists(rel, dest):
                got.append(rel)
        except Exception as exc:  # noqa: BLE001
            log(f"⚠ 拉取 {rel} 失败：{exc}")
    log(f"基线数据：{len(got)}/{len(SYNC_DOWN)} 个文件（首次运行拿不到属正常）")


def run_update():
    py = sys.executable or "python3"
    argv = [py, "cloud_update.py"]
    if os.environ.get("SKIP_REFRESH") == "1":
        argv.append("--skip-refresh")
    if os.environ.get("SKIP_CAMPUS") == "1":
        argv.append("--skip-campus")
    # 公众号这一步默认关闭(需要 cookies + wxmp,云函数里都没有);
    # 只有显式 WITH_WECHAT=1(且函数包里带了依赖与 cookies)才会跑。
    if os.environ.get("WITH_WECHAT") == "1":
        argv.append("--with-wechat")
    # 负载开关:抓不完/超时就调小 MAX_PER_SOURCE 或调大 WORKERS
    if os.environ.get("MAX_PER_SOURCE"):
        argv += ["--max-per-source", os.environ["MAX_PER_SOURCE"]]
    if os.environ.get("MAX_WECHAT"):
        argv += ["--max-wechat", os.environ["MAX_WECHAT"]]
    if os.environ.get("WORKERS"):
        argv += ["--workers", os.environ["WORKERS"]]
    if os.environ.get("DELAY"):
        argv += ["--delay", os.environ["DELAY"]]
    if os.environ.get("RETRIES"):
        argv += ["--retries", os.environ["RETRIES"]]
    if os.environ.get("NO_DETAILS") == "1":
        argv.append("--no-details")
    started = time.time()
    proc = subprocess.run(argv, cwd=WORK_DIR, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT)
    text = (proc.stdout or b"").decode("utf-8", "replace")
    took = time.time() - started
    log(f"cloud_update.py 退出码 {proc.returncode}，耗时 {took / 60:.1f} 分钟")
    # 只回显摘要（cloud_update 已把上千行进度收进 _work/run.log）
    for line in text.splitlines()[-40:]:
        print(line, flush=True)
    # 静默失败预警:refresh.py 抓取失败也会正常退出(退出码 0),
    # 但表现为「几秒就结束」。摘要行里能看出来,这里点名提醒。
    if os.environ.get("SKIP_REFRESH") != "1":
        m = re.search(r"清华 refresh.py：(\S+)（(\d+)m(\d+)s", text)
        if m and int(m.group(2)) * 60 + int(m.group(3)) < 5:
            log("⚠ 清华这轮只用了几秒，疑似上游接口请求失败没有导入新数据；"
                "完整原因见桶里 _work/run.log")
    if os.environ.get("WITH_WECHAT") == "1":
        for line in text.splitlines():
            if line.startswith("⚠ 公众号") or line.startswith("· 公众号"):
                log(line)
    return proc.returncode


def push_results(store):
    uploaded = []
    for rel in SYNC_UP:
        src = os.path.join(WORK_DIR, rel)
        if not os.path.isfile(src):
            log(f"⚠ 产物 {rel} 不存在，跳过上传")
            continue
        ctype = "text/html; charset=utf-8" if rel.endswith(".html") else "application/json"
        store.upload(rel, src, content_type=ctype)
        uploaded.append(f"{rel} ({os.path.getsize(src) / 1048576:.2f} MB)")
    # 完整抓取日志(含每个来源的成败明细),排查某轮失败时从桶里取
    log_src = os.path.join(WORK_DIR, SYNC_UP_LOG)
    if os.path.isfile(log_src):
        store.upload(SYNC_UP_LOG, log_src, content_type="text/plain; charset=utf-8")
    mirror_site_index(store, uploaded)
    return uploaded


def mirror_site_index(store, uploaded):
    """把 index.html 额外镜像一份到桶根目录（OSS 静态托管的默认首页只认根目录文件）。

    开了静态网站托管后，访问 http://bucket.oss-xxx.aliyuncs.com/ 时 OSS 只会去
    桶根找默认首页，不允许填 thu-know/index.html 这种带子目录的路径。所以这里
    在 PREFIX 下传完正式的那份之后，再镜像一份到根目录，两边内容完全一致。

    设 SITE_ROOT_INDEX=0 可关闭镜像（比如桶根还要放别的东西时）。
    """
    if (os.environ.get("SITE_ROOT_INDEX") or "1").strip() == "0":
        return
    if not store.prefix:          # 本来就在根目录，不用镜像
        return
    src = os.path.join(WORK_DIR, "index.html")
    if not os.path.isfile(src):
        return
    size = os.path.getsize(src)
    store.upload_root("index.html", src, content_type="text/html; charset=utf-8")
    uploaded.append(f"index.html (根目录镜像, {size / 1048576:.2f} MB)")

    # 根目录那份 index.html 会按 data/shard/... 去取分片，所以分片也得镜像到根目录，
    # 否则从网站首页进来能看列表、点开详情却是空的。
    for rel in SHARD_FILES:
        shard_src = os.path.join(WORK_DIR, rel)
        if not os.path.isfile(shard_src):
            continue
        store.upload_root(rel, shard_src, content_type="application/json")
    uploaded.append(f"data/shard/*.json (根目录镜像, {len(SHARD_FILES)} 份)")


def handler(event, context):
    """FC / SCF 事件函数入口。"""
    log("=" * 60)
    try:
        code_dir = find_code_dir()
        store = make_backend()
        prepare_workdir(code_dir)
        pull_baseline(store)
        code = run_update()
        uploaded = push_results(store)
        log(f"回写完成：{', '.join(uploaded) or '（无）'}")
        if code != 0:
            # 抓取器「部分源失败」是预期内的，只有两条命令都失败才算真失败
            return {"ok": False, "exit_code": code, "uploaded": uploaded}
        return {"ok": True, "uploaded": uploaded}
    except Exception as exc:  # noqa: BLE001
        log("执行失败：\n" + traceback.format_exc())
        return {"ok": False, "error": str(exc)}


if __name__ == "__main__":
    # 本地冒烟测试（需先 pip install oss2 或 cos-python-sdk-v5 并设好环境变量）
    print(handler({}, None))
