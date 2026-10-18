#!/usr/bin/env python3
"""极简阿里云 OSS 客户端——纯标准库，零第三方依赖。

为什么要自己写
--------------
官方 `oss2` 会拖进 requests / urllib3 / cryptography / pycryptodome / 阿里云 SDK
等 900+ 个文件、20+ MB，而且**含平台相关的 C 扩展**。在 Windows 上打包出来的
`.pyd` 传到 Linux 的 FC 上根本加载不了；即使能装，函数包也白白胖肿一大圈。

本项目只用得到两个动作——**读一个对象、写一个对象**。用标准库的
urllib + hmac + hashlib + base64 就能完成 OSS V1 签名，代码不到 150 行，
在任何平台、任何 Python 版本上都能跑，函数包可以保持"纯代码"。

签名算法见官方文档「在 Header 中包含签名（V1）」，本实现已用官方给出的
示例向量做过校验（见 test_oss_min.py）。

凭证
----
优先读 FC 绑定 RAM 角色后**自动注入的环境变量**（无需 SDK、无需明文 AK）：
    ALIBABA_CLOUD_ACCESS_KEY_ID
    ALIBABA_CLOUD_ACCESS_KEY_SECRET
    ALIBABA_CLOUD_SECURITY_TOKEN   （STS 临时凭证才有）
回退到 OSS_AK / OSS_SK / OSS_SECURITY_TOKEN（仅本地调试用）。
"""

import base64
import hashlib
import hmac
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import format_datetime

__all__ = ["OSSClient", "string_to_sign", "canonicalized_oss_headers", "OSSNotFound"]


class OSSNotFound(Exception):
    """对象不存在（404）。"""


def canonicalized_oss_headers(headers):
    """取所有 x-oss-* 头，名转小写、按字典序排序，拼成 `k:v\\n`。"""
    items = sorted((k.lower(), v) for k, v in headers.items()
                   if k.lower().startswith("x-oss-"))
    return "".join(f"{k}:{v}\n" for k, v in items)


def string_to_sign(verb, content_md5, content_type, date, oss_headers, resource):
    """构造待签名字符串（纯函数，便于单元测试）。"""
    parts = [verb, content_md5 or "", content_type or "", date or ""]
    base = "\n".join(parts) + "\n"
    return base + canonicalized_oss_headers(oss_headers) + resource


class OSSClient:
    def __init__(self, bucket, endpoint, access_key_id=None, access_key_secret=None,
                 security_token=None, timeout=60, base_url=None):
        base_url = base_url or os.environ.get("OSS_BASE_URL") or None
        self.bucket = bucket
        self.endpoint = endpoint.strip().rstrip("/")
        self.ak = access_key_id or os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID") \
            or os.environ.get("OSS_AK") or ""
        self.sk = access_key_secret or os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET") \
            or os.environ.get("OSS_SK") or ""
        self.token = security_token or os.environ.get("ALIBABA_CLOUD_SECURITY_TOKEN") \
            or os.environ.get("OSS_SECURITY_TOKEN") or None
        self.timeout = timeout
        # base_url 仅用于本地测试：直接指定 http://127.0.0.1:port，绕过 bucket 子域名
        if base_url:
            self.host = ""
            self.base_url = base_url.rstrip("/")
        else:
            self.host = f"{bucket}.{self.endpoint}"
            self.base_url = f"https://{self.host}"
        if not self.ak or not self.sk:
            raise RuntimeError(
                "缺少 OSS 凭证：请在函数上绑定 RAM 角色（FC 会自动注入 "
                "ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET），或设 OSS_AK/OSS_SK 做本地调试"
            )

    # --- 内部 ---
    def _resource(self, key):
        return f"/{self.bucket}/{key}"

    def _authorization(self, verb, key, headers):
        """按 V1 算法生成 Authorization 头值。"""
        resource = self._resource(key)
        to_sign = string_to_sign(
            verb,
            headers.get("Content-MD5"),
            headers.get("Content-Type"),
            headers.get("Date"),
            headers,
            resource,
        )
        digest = hmac.new(self.sk.encode("utf-8"), to_sign.encode("utf-8"),
                          hashlib.sha1).digest()
        signature = base64.b64encode(digest).decode("ascii")
        return f"OSS {self.ak}:{signature}"

    def _request(self, verb, key, body=None, content_type=None):
        headers = {"Date": format_datetime(datetime.now(timezone.utc), usegmt=True)}
        if content_type:
            headers["Content-Type"] = content_type
        if self.token:
            # x-oss-security-token 以 x-oss- 开头，会自动进入待签名的规范头
            headers["x-oss-security-token"] = self.token
        headers["Authorization"] = self._authorization(verb, key, headers)

        url = f"{self.base_url}/{urllib.parse.quote(key, safe='/')}"
        req = urllib.request.Request(url, data=body, headers=headers, method=verb)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise OSSNotFound(key) from exc
            detail = exc.read()[:400].decode("utf-8", "replace")
            raise RuntimeError(f"OSS {verb} {key} 失败：HTTP {exc.code} {detail}") from exc

    # --- 对外 ---
    def get(self, key):
        """读对象；不存在返回 None。"""
        try:
            return self._request("GET", key)
        except OSSNotFound:
            return None

    def put(self, key, data, content_type="application/octet-stream"):
        """写对象（覆盖）。"""
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._request("PUT", key, body=data, content_type=content_type)


if __name__ == "__main__":  # 极简自测：环境变量齐全时可真实读写
    cli = OSSClient(os.environ["BUCKET"], os.environ.get("ENDPOINT", ""))
    probe = os.environ.get("PROBE_KEY", "thu-know/_selftest.txt")
    cli.put(probe, b"hello from oss_min", content_type="text/plain")
    print("写入并读回：", cli.get(probe))
