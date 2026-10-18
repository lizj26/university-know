#!/usr/bin/env python3
"""把 thu-know 打包成可直传云函数的 ZIP（默认阿里云 FC 版）。

用法
----
    python deploy/serverless/package.py                  # 阿里云 FC 版 → dist/fc-thu-know.zip（纯代码，零依赖）
    python deploy/serverless/package.py --no-seed-data   # 不带 data/*.json（桶里已有基线时）
    python deploy/serverless/package.py --with-wxmp      # 额外带上 wxmp 依赖（要开 WITH_WECHAT 才需要）
    python deploy/serverless/package.py --backend cos    # 腾讯云 SCF 版（会装 cos-python-sdk-v5，须在 Linux 下打包）

产物结构（ZIP 解压后 = 函数代码目录）
----
    handler.py            ← 入口，函数入口填 handler.handler
    oss_min.py            ← 自带的标准库 OSS 客户端（阿里云侧无需任何依赖）
    refresh.py  fetch_campus.py  fetch_wechat.py  cloud_update.py  template.html
    data/zju.json  data/sjtu.json  data/xjtu.json   （种子基线，可关）

依赖说明：抓取器本身零第三方依赖；阿里云 OSS 侧也用自带客户端，因此默认打的包
是**纯代码**，不需要 pip、不受平台限制。只有两种情况需要联网 pip：
  * `--backend cos`（腾讯云对象存储 SDK）；
  * `--with-wxmp`（公众号抓取，见下）。

关于 --with-wxmp
----------------
`fetch_wechat.py` 只在**真的要抓公众号**（WITH_WECHAT=1 且给了 cookies）时才需要
wxmp / requests。默认不装，函数包保持纯代码。若确实要在云端抓公众号：
  1. 在 **Linux** 环境（或 Docker）里跑 `python deploy/serverless/package.py --with-wxmp`
     —— wxmp 的依赖里有 pandas，含平台相关二进制，Windows 打出来的包装不上 FC；
  2. 把 cookies 放进函数包的 `_work/wechat/wxmp-cookie-header.txt`（**注意这会泄漏账号凭据**，
     更稳的做法是仍旧留在本机抓），或改用环境变量注入；
  3. FC 控制台设 `WITH_WECHAT=1`。
"""

import argparse
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))          # E:/project/thu-know
DIST = os.path.join(HERE, "dist")

CODE_FILES = ["refresh.py", "fetch_campus.py", "fetch_wechat.py",
              "cloud_update.py", "template.html", "oss_min.py"]
SEED_DATA = ["data/zju.json", "data/sjtu.json", "data/xjtu.json",
             # 首屏只内嵌轻量索引，正文与切校区靠这些分片；带上它们，
             # 首轮部署不用等抓取就能点开详情
             "data/shard/zju.json", "data/shard/zju-d.json",
             "data/shard/sjtu.json", "data/shard/sjtu-d.json",
             "data/shard/xjtu.json", "data/shard/xjtu-d.json"]
# 阿里云 OSS 后端走自带的标准库客户端 oss_min.py，零第三方依赖 → ZIP 保持纯代码。
# 只有腾讯云 COS 需要 SDK，且必须在 Linux 环境打包（含平台相关二进制）。
PIP_PACKAGES = {"oss": [], "cos": ["cos-python-sdk-v5"], "none": []}
# 公众号抓取可选的依赖（只有 --with-wxmp 时安装）
WXMP_PACKAGES = ["wxmp"]
ENTRY_FILE = os.path.join(HERE, "handler.py")          # 会被改名成根目录的 handler.py


def say(msg):
    print(f"[package] {msg}", flush=True)


def install_deps(target, backend, with_wxmp=False):
    pkgs = list(PIP_PACKAGES.get(backend, []))
    if with_wxmp:
        pkgs += WXMP_PACKAGES
    if not pkgs:
        return
    cmd = [sys.executable, "-m", "pip", "install", "--target", target,
           "--no-compile", "--disable-pip-version-check", "-q"] + pkgs
    say("安装依赖：" + " ".join(cmd))
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        raise SystemExit(
            "依赖安装失败（需要联网 pip）。"
            "阿里云 FC 且不需要公众号抓取时，请直接跑 python deploy/serverless/package.py（零依赖）。"
        )


def stage(args):
    if os.path.isdir(DIST):
        shutil.rmtree(DIST)
    stage_dir = os.path.join(DIST, "_stage")
    os.makedirs(os.path.join(stage_dir, "data"), exist_ok=True)

    # 1) 入口改名放根目录（FC 入口 = handler.handler）
    shutil.copy2(ENTRY_FILE, os.path.join(stage_dir, "handler.py"))
    # 2) 抓取/建站代码（根目录里的那几个）+ 本目录里的 oss_min.py
    missing = []
    for name in CODE_FILES:
        for base in (ROOT, HERE):
            src = os.path.join(base, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(stage_dir, name))
                break
        else:
            missing.append(name)
    if missing:
        say(f"⚠ 缺少代码文件：{', '.join(missing)}（对应步骤会被跳过）")
    # 3) 种子基线
    if not args.no_seed_data:
        for rel in SEED_DATA:
            src = os.path.join(ROOT, rel)
            if os.path.isfile(src):
                dst = os.path.join(stage_dir, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)   # data/shard/ 需要现建
                shutil.copy2(src, dst)
            else:
                say(f"· 种子数据 {rel} 不存在，跳过")
    # 4) 依赖
    if not args.code_only:
        install_deps(stage_dir, args.backend, with_wxmp=args.with_wxmp)
    return stage_dir


def zip_stage(stage_dir, backend, tag):
    name = f"{tag}.zip"
    out = os.path.join(DIST, name)
    total = files = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for base, _dirs, names in os.walk(stage_dir):
            for fn in names:
                full = os.path.join(base, fn)
                arc = os.path.relpath(full, stage_dir)
                zf.write(full, arc)
                total += os.path.getsize(full)
                files += 1
    say(f"生成 {out}")
    say(f"  文件数 {files}，原始体积 {total / 1048576:.2f} MB，"
        f"压缩后 {os.path.getsize(out) / 1048576:.2f} MB")
    say(f"  FC 函数入口填：handler.handler（运行环境 Python 3.10）")
    return out


def main():
    ap = argparse.ArgumentParser(description="打包 thu-know 云函数 ZIP")
    ap.add_argument("--backend", choices=["oss", "cos", "none"], default="oss",
                    help="对象存储后端，决定装哪个 SDK（默认 oss）")
    ap.add_argument("--code-only", action="store_true", help="只打代码，不装依赖")
    ap.add_argument("--no-seed-data", action="store_true", help="不带 data/*.json 种子")
    ap.add_argument("--with-wxmp", action="store_true",
                    help="额外安装 wxmp（公众号抓取用）。须在 Linux 下打包，且要把 WITH_WECHAT 置 1")
    args = ap.parse_args()

    tag = {"oss": "fc-thu-know", "cos": "scf-thu-know", "none": "code-thu-know"}[args.backend]
    if args.code_only:
        tag += "-code-only"
    if args.with_wxmp:
        tag += "-wxmp"

    say(f"项目根目录：{ROOT}")
    stage_dir = stage(args)
    out = zip_stage(stage_dir, args.backend, tag)
    shutil.rmtree(stage_dir, ignore_errors=True)   # 中间目录（可能含依赖）不必保留
    say("")
    say("下一步：把该 ZIP 在 FC 控制台「代码上传方式 → ZIP 包」里上传，")
    say("并设置环境变量 STORAGE_BACKEND / BUCKET / ENDPOINT / PREFIX（见 deploy/serverless/README.md）。")
    return 0 if out else 1


if __name__ == "__main__":
    sys.exit(main())
