#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 thu-know 的 index.html 部署到腾讯云 EdgeOne Pages（免费静态托管）。

为什么用它：阿里云 OSS 的默认域名会强制下载 HTML（服务端加 Content-Disposition: attachment），
正常渲染必须绑「已备案」的自定义域名。EdgeOne Pages 的默认域名
（https://<项目名>.edgeone.app）无需备案即可在浏览器直接渲染，国内可访问。

用法
----
    # 首次：先在 EdgeOne 控制台创建 API Token（https://console.tencentcloud.com/edgeone/pages）
    set EDGEONE_TOKEN=你的token
    python deploy/edgeone/deploy_edgeone.py

    # 常用参数
    python deploy/edgeone/deploy_edgeone.py --name thu-know      # 指定项目名
    python deploy/edgeone/deploy_edgeone.py --src index.html     # 指定页面文件

原理：把 index.html 打包成 zip → 调 EdgeOne CLI 上传 → 返回可访问 URL。
CLI 要求 Node.js ≥18（npx 会自动下载，首次慢一点）。

自动化：本机定时任务方案 A 里，把它接在 cloud_update.py 之后即可全链路无人值守：
    python cloud_update.py && python deploy/edgeone/deploy_edgeone.py
"""

import argparse
import os
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))          # E:/project/thu-know
DEFAULT_SRC = os.path.join(ROOT, "index.html")
STAGE = os.path.join(HERE, "dist")                      # 打包用的临时目录


def say(msg):
    print(f"[edgeone] {msg}", flush=True)


def make_zip(src, stage_zip):
    """把 index.html 装进一个 zip（EdgeOne 要求入口文件名为 index.html）。"""
    os.makedirs(os.path.dirname(stage_zip), exist_ok=True)
    with zipfile.ZipFile(stage_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.write(src, "index.html")
    say(f"打包完成：{stage_zip}（{os.path.getsize(stage_zip) / 1048576:.2f} MB）")


def deploy(zip_path, name, token, area="global"):
    cmd = ["npx", "-y", "edgeone@latest", "pages", "deploy", zip_path,
           "-n", name, "-t", token, "--json", "--skip-ai-gateway-sync"]
    if area:
        cmd += ["-a", area]
    say("调用 EdgeOne CLI 部署（首次会自动下载 CLI，请稍候）…")
    proc = subprocess.run(cmd, capture_output=True)
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    # --json 模式下最后一行是 JSON：{"success": true, "url": "..."}
    url = ""
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            import json
            try:
                data = json.loads(line)
            except ValueError:
                continue
            url = data.get("url") or data.get("domain") or ""
            if data.get("success") is False:
                sys.stderr.write(err or out)
                raise SystemExit(f"部署失败：{data}")
            break
    if proc.returncode != 0 or not url:
        sys.stderr.write("---- CLI 输出 ----\n" + out + "\n---- 错误 ----\n" + err)
        raise SystemExit(f"部署失败（退出码 {proc.returncode}）")
    return url


def main():
    ap = argparse.ArgumentParser(description="部署 index.html 到 EdgeOne Pages")
    ap.add_argument("--src", default=DEFAULT_SRC, help="页面文件（默认项目根的 index.html）")
    ap.add_argument("--name", default=os.environ.get("EDGEONE_PROJECT") or "thu-know",
                    help="EdgeOne 项目名（即默认域名前缀）")
    ap.add_argument("--token", default=os.environ.get("EDGEONE_TOKEN"),
                    help="API Token；不传则读环境变量 EDGEONE_TOKEN")
    ap.add_argument("--area", default=os.environ.get("EDGEONE_AREA") or "global",
                    choices=["global", "overseas"],
                    help="global=含中国节点（备案域名才能吃到国内加速）；overseas=纯海外")
    args = ap.parse_args()

    src = os.path.abspath(args.src)
    if not os.path.isfile(src):
        raise SystemExit(f"找不到页面文件：{src}（先跑 python cloud_update.py 生成）")
    if not args.token:
        raise SystemExit("缺少 API Token：请 set EDGEONE_TOKEN=xxx（控制台 API Token 页创建）")

    zip_path = os.path.join(STAGE, f"{args.name}.zip")
    make_zip(src, zip_path)
    url = deploy(zip_path, args.name, args.token, args.area)
    say(f"✓ 部署成功：{url}")
    say("浏览器打开即可（国内可直接渲染，无需备案）。")
    try:
        os.remove(zip_path)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
