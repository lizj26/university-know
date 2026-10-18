#!/usr/bin/env python3
"""无人值守 / 云上模式专用入口:一条命令跑完三轮抓取,终端只留一段摘要。

用法
----
    python cloud_update.py                  # 抓取各高校(浙大/上交/西交),重建 index.html
    python cloud_update.py --skip-campus    # 跳过抓取,只重建页面
    python cloud_update.py --with-wechat    # 再加上公众号这一步
    python cloud_update.py --summarize-only # 不抓取,只按现有 data/ 打印摘要(自检用)
    python cloud_update.py --stats-only     # 同 --summarize-only,语义更直白

为什么要有这个文件
------------------
给「云端定时任务 / cron / 无人值守」当唯一入口。
「清华知道」在本站是外链（直达 pkuknow.cn/thu），故本入口不再跑清华这一步；
默认只跑 `fetch_campus.py` 抓浙大/上交/西交三校，再（可选）跑公众号，最后重建页面。

`refresh.py`、`fetch_campus.py`、`fetch_wechat.py` 会打印上千行进度(上百个来源逐个报状态),
在由 AI 驱动的云端任务里,这些输出会整段进入模型上下文,显着抬高积分消耗,
而里面 99% 的信息(每个源开始抓、抓到几条)对人没有价值。

所以这里把三个抓取器的**完整输出**重定向到 `_work/run.log`,
终端只输出一段十几行的摘要:各校区条数、本轮新增/更新、失败来源、产物大小。

三个抓取器本身**一行都没有改**:失败重试、http→https 回退、
只增不减合并、90 天保留期、写入前备份 .bak 全部照旧生效。

关于第三步(公众号)为什么默认关闭
--------------------------------
`fetch_wechat.py` 的 API 模式依赖两样本机资源:**登录后的 cookies** 与 **wxmp 依赖**,
两者在无状态的云函数里都不成立(见 docs/wxmp公众号接入.md 第 9 节)。
所以云端默认**不跑**公众号这一步;要跑必须显式 `--with-wechat`(或环境变量 WITH_WECHAT=1),
且函数包/运行环境里得自己准备好 cookies 与依赖。本机定时任务则可以放心打开。
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
WORK_DIR = os.path.join(HERE, "_work")
LOG_PATH = os.path.join(WORK_DIR, "run.log")

CAMPUS_NAMES = {
    "thu": "清华",
    "zju": "浙大",
    "sjtu": "上交",
    "xjtu": "西交",
}


def disp_width(text):
    """终端里中文按两格算,这样列才对得齐。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in str(text))


def pad(text, width, align="left"):
    gap = " " * max(0, width - disp_width(text))
    return (str(text) + gap) if align == "left" else (gap + str(text))


def out(text=""):
    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:  # very old console codepages
        sys.stdout.write(text.encode("utf-8", "replace").decode("utf-8", "replace") + "\n")
    sys.stdout.flush()


def run_step(label, argv, log_handle):
    """跑一个子进程,把它所有的 stdout/stderr 灌进日志,只返回耗时与退出码。"""
    log_handle.write(f"\n{'=' * 72}\n== {label}: {' '.join(argv)}\n{'=' * 72}\n")
    log_handle.flush()
    started = time.time()
    try:
        proc = subprocess.run(
            argv, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        raw = proc.stdout or b""
        code = proc.returncode
    except Exception as exc:  # noqa: BLE001
        raw = f"启动失败: {exc}\n".encode("utf-8")
        code = -1
    text = raw.decode("utf-8", "replace")
    log_handle.write(text)
    if not text.endswith("\n"):
        log_handle.write("\n")
    log_handle.flush()
    return time.time() - started, code, len(text.splitlines())


def load_campus(campus):
    for path in (os.path.join(DATA_DIR, f"{campus}.json"),
                 os.path.join(DATA_DIR, f"{campus}.js")):
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:  # noqa: BLE001
                return None
    return None


def failed_sources(data):
    """从 source_registry 或 sources 列表里挑出本轮失败的来源。"""
    rows = []
    reg = data.get("source_registry") or {}
    for meta in reg.values():
        if isinstance(meta, dict) and meta.get("status") == "failed":
            rows.append((meta.get("name") or "?", meta.get("url") or ""))
    srcs = data.get("sources") or []
    for src in srcs:
        if isinstance(src, dict) and src.get("status") == "failed":
            rows.append((src.get("name") or src.get("key") or "?", src.get("url") or ""))
    seen, uniq = set(), []
    for name, url in rows:
        if (name, url) in seen:
            continue
        seen.add((name, url))
        uniq.append((name, url))
    return uniq


def short_delta(li):
    """把 last_import 归一成可以塞进摘要一列的短文本。

    历史遗留:不同抓取器写过三种格式 ——
      * dict  {"added": 3, "updated": 1, ...}   ← refresh.py / fetch_campus.py
      * 字符串 "fetch_wechat.py / wxmp · 新增 0 条"  ← fetch_wechat.py 早期版本
      * 空
    统一显示成 "+新增 / 更新"；字符串里的中文数字捞不出来就退化成 "更新过"。
    """
    if isinstance(li, dict):
        return f"+{int(li.get('added') or 0)} / {int(li.get('updated') or 0)}"
    if not li:
        return "—"
    # 字符串形态:尽力从「新增 N 条」里抠出数字
    m = re.search(r"新增\s*(\d+)", str(li))
    if m:
        return f"+{m.group(1)} / ?"
    return "已更新"


def summarize():
    lines = []
    total_items = total_added = total_updated = 0
    all_failed = []
    found = [os.path.splitext(os.path.basename(f))[0]
             for f in glob.glob(os.path.join(DATA_DIR, "*.json"))]
    order = ["thu", "zju", "sjtu", "xjtu"]
    campuses = [k for k in order if k in found] + sorted(k for k in found if k not in order)
    lines.append("")
    lines.append(pad("校区", 8) + pad("条目", 6, "right") + "  "
                 + pad("本轮新增/更新", 16) + "来源")
    lines.append("-" * 56)
    for key in campuses:
        data = load_campus(key)
        if not data:
            lines.append(f"{key:<8}  读取失败")
            continue
        items = data.get("items") or []
        reg = data.get("source_registry") or {}
        srcs = data.get("sources") or []
        count = len(reg) or len(srcs)
        fails = failed_sources(data)
        all_failed.extend((key, n, u) for n, u in fails)
        total_items += len(items)
        li = data.get("last_import")
        if isinstance(li, dict):
            total_added += int(li.get("added") or 0)
            total_updated += int(li.get("updated") or 0)
        delta = short_delta(li)
        stat = (f"{count} 个来源" if count else "—") + (f" · 失败 {len(fails)}" if fails else "")
        lines.append(pad(key, 8) + pad(len(items), 6, "right") + "  "
                     + pad(delta, 16) + stat)
    lines.append("-" * 56)
    lines.append(pad("合计", 8) + pad(total_items, 6, "right") + "  "
                 + f"+{total_added} / {total_updated}")

    if all_failed:
        lines.append("")
        lines.append("抓取失败的来源:")
        for key, name, url in all_failed[:20]:
            lines.append(f"  [{key}] {name}  {url}")
        if len(all_failed) > 20:
            lines.append(f"  …… 另有 {len(all_failed) - 20} 个")

    index = os.path.join(HERE, "index.html")
    if os.path.exists(index):
        size = os.path.getsize(index) / 1048576
        stamp = datetime.fromtimestamp(os.path.getmtime(index)).strftime("%m-%d %H:%M")
        lines.append("")
        lines.append(f"产物 index.html：{size:.2f} MB，更新于 {stamp}")
    else:
        lines.append("")
        lines.append("⚠ 没有找到 index.html")
    return lines


def wechat_status_from_log(log_path):
    """从 run.log 的公众号段落里读出「这一步到底怎么样」，并翻译成一句人话。

    为什么需要:公众号这一步**有三种"看起来成功、实际没拿到东西"**的结局,
    但退出码都是 0(设计如此 —— 拿不到内容时必须保留旧数据,不能把空结果当成"无新内容"写入):

      ① 没装 wxmp / 没 cookies → 自动降级到"公开种子"模式,可能一条都抓不到;
      ② 账号级限流 200013 → 全部号为 0 篇,整轮中止不写 data;
      ③ 名单/网络问题 → 部分号失败。

    摘要里如果只写「OK」,无人值守时很容易误以为公众号在正常更新,
    所以这里显式点出来,并附带修法。
    """
    try:
        with open(log_path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return ""
    if "fetch_wechat.py" not in text:
        return ""
    # 只取最后一段公众号日志(每轮日志会被覆盖,这里也防同文件里出现多次)
    seg = text.rsplit("== 公众号 fetch_wechat.py", 1)[-1]

    if "未能加载 wxmp" in seg:
        return ("⚠ 公众号：本机没装 wxmp，已自动降级为「公开种子」模式。"
                "要按号成批抓取请 pip install wxmp（需 Python >= 3.10）")
    if "200013" in seg or "freq control" in seg:
        # 分粒度给建议：账号级只能换账号（等待无效），fakeid 级会自动跳过个别号。
        # 旧版这里一律说"等数小时至 1 天解禁"，对账号级限流是错的——会让人陷入
        # "等一天→重跑→还是 0 篇→再等一天" 的死循环。
        if "账号级限流" in seg:
            return ("⚠ 公众号：被微信**账号级限流**(ret=200013 freq control)，本轮所有号都没拉到，"
                    "已保留旧数据。**这种限流等待无效，只能换登录账号** —— 把第 2 份 Cookie 整行"
                    "存成 _work/wechat/wxmp-cookie-header-pool2.txt，脚本会自动轮换；"
                    "没有第二份凭据就配 _work/wechat/wxmp-albums.json 走公开合集源（不受限流影响）")
        if "fakeid 级限流" in seg:
            return ("⚠ 公众号：部分号被**单号限流**(ret=200013)，只影响这几个号，其余正常。"
                    "已记入 _work/wechat/wxmp-blocked.json，6 小时内自动跳过，无需人工干预")
        return ("⚠ 公众号：出现限流(ret=200013 freq control)，本轮未拉全，已保留旧数据。"
                "账号级限流等待无效、只能换账号(存成 wxmp-cookie-header-pool2.txt)；"
                "单号限流会自动跳过。详见 docs/wxmp公众号接入.md 第 10 节")
    if "没有 cookies" in seg:
        return ("⚠ 公众号：没找到 cookies，已降级为「公开种子」模式。"
                "把登录后的 Cookie 整行存到 _work/wechat/wxmp-cookie-header.txt 即可启用按号抓取")
    # 统计本轮实际入库量
    m = None
    for m2 in re.finditer(r"合并完成：新增 (\d+) 条，现有 (\d+) 条", seg):
        m = m2
    if m:
        return f"· 公众号：本轮新增 {m.group(1)} 条，现有 {m.group(2)} 条"
    return ""


# 兼容旧名(早期版本叫 wechat_failure_note)
wechat_failure_note = wechat_status_from_log


def main():
    ap = argparse.ArgumentParser(description="无人值守入口:跑完抓取只输出摘要")
    ap.add_argument("--skip-refresh", action="store_true",
                    help="已废弃：清华改为外链后不再单独抓取，此开关现为空操作（保留以兼容旧调用）")
    ap.add_argument("--skip-campus", action="store_true", help="跳过各高校(fetch_campus.py)")
    ap.add_argument("--skip-wechat", action="store_true",
                    help="跳过公众号(fetch_wechat.py);默认就跳过,除非给了 --with-wechat")
    ap.add_argument("--with-wechat", action="store_true",
                    help="把微信公众号抓取(fetch_wechat.py)作为第三步跑(需本机有 cookies/wxmp)")
    ap.add_argument("--wechat-only", action="store_true",
                    help="只跑公众号这一步(等价 --skip-campus --with-wechat)")
    ap.add_argument("--campus", default=None,
                    help="限定校区(逗号分隔,如 zju / zju,sjtu);默认三校全抓。用于调试单校")
    ap.add_argument("--summarize-only", "--stats-only", dest="summarize_only",
                    action="store_true", help="不抓取,只按现有 data/ 打印摘要")
    # 负载开关(云端函数用环境变量注入,见 handler.py):
    # 想压短单轮耗时,要么调小 --max-per-source,要么调大 --workers。
    ap.add_argument("--max-per-source", type=int, default=None,
                    help="每个来源最多取多少条(默认交给 fetch_campus.py,即 15)")
    ap.add_argument("--max-wechat", type=int, default=None, help="每个来源最多抓多少篇公众号文章")
    ap.add_argument("--workers", type=int, default=None,
                    help="并发抓取的来源数(默认交给 fetch_campus.py,即 4)")
    ap.add_argument("--delay", type=float, default=None, help="同一主机两次请求的最小间隔(秒)")
    ap.add_argument("--domain-delay", type=float, default=None,
                    help="透传给 fetch_campus.py:同一注册域名聚合请求的最小间隔(秒),zju 建议 1.0~2.0")
    ap.add_argument("--rotate", action="store_true",
                    help="透传给 fetch_campus.py:按日期轮换来源抓取起点(zju 建议开启)")
    ap.add_argument("--no-details", action="store_true", help="不抓正文,只取标题/时间/链接(快很多)")
    ap.add_argument("--retries", type=int, default=None,
                    help="单页失败重试次数(默认 2;云端网络受限可设 1)")
    ap.add_argument("--wechat-count", type=int, default=None,
                    help="公众号每个号最多取几篇(默认交给 fetch_wechat.py,即 15)")
    ap.add_argument("--wechat-pages", type=int, default=None,
                    help="公众号每个号最多翻几页(每页 5 篇,默认 3)")
    ap.add_argument("--wechat-no-content", action="store_true",
                    help="公众号只取列表摘要,不抓正文(最快,也最不容易触发风控)")
    args = ap.parse_args()

    if args.wechat_only:
        args.skip_campus = True
        args.with_wechat = True
    if args.skip_wechat:
        args.with_wechat = False

    if args.summarize_only:
        out("== thu-know 现状摘要（未抓取）==")
        for line in summarize():
            out(line)
        return 0

    os.makedirs(WORK_DIR, exist_ok=True)
    py = sys.executable or "python"
    started = time.time()
    steps = []

    campus_args = []
    if args.campus:
        campus_args += ["--campus", args.campus]
    if args.max_per_source is not None:
        campus_args += ["--max-per-source", str(args.max_per_source)]
    if args.max_wechat is not None:
        campus_args += ["--max-wechat", str(args.max_wechat)]
    if args.workers is not None:
        campus_args += ["--workers", str(args.workers)]
    if args.delay is not None:
        campus_args += ["--delay", str(args.delay)]
    if args.domain_delay is not None:
        campus_args += ["--domain-delay", str(args.domain_delay)]
    if args.rotate:
        campus_args.append("--rotate")
    if args.retries is not None:
        campus_args += ["--retries", str(args.retries)]
    if args.no_details:
        campus_args.append("--no-details")

    # 公众号这一步的参数:尽量复用上面的通用开关,再叠加它专属的
    wechat_args = []
    if args.campus:
        wechat_args += ["--campus", args.campus]
    if args.max_wechat is not None:
        wechat_args += ["--count", str(args.max_wechat)]
    if args.wechat_count is not None:
        wechat_args = [a for a in wechat_args
                       if not a.startswith("--count")] + ["--count", str(args.wechat_count)]
    if args.wechat_pages is not None:
        wechat_args += ["--pages", str(args.wechat_pages)]
    if args.workers is not None:
        wechat_args += ["--workers", str(args.workers)]
    if args.retries is not None:
        wechat_args += ["--retries", str(args.retries)]
    if args.wechat_no_content or args.no_details:
        wechat_args.append("--no-content")
    # 公众号自己会重建一次页面;在流水线里让它跳过,由最后统一 build
    wechat_args.append("--no-build")

    with open(LOG_PATH, "w", encoding="utf-8") as log_handle:
        log_handle.write(f"thu-know 更新开始 {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        # 注：清华已是外链，不再有独立的 refresh.py 抓取步骤
        if not args.skip_campus:
            steps.append(("各高校 fetch_campus.py", [py, "fetch_campus.py"] + campus_args))
        if args.with_wechat:
            if os.path.isfile(os.path.join(HERE, "fetch_wechat.py")):
                steps.append(("公众号 fetch_wechat.py", [py, "fetch_wechat.py"] + wechat_args))
            else:
                out("⚠ 找不到 fetch_wechat.py，已跳过公众号这一步")

        results = []
        wechat_note = ""
        for label, argv in steps:
            took, code, nlines = run_step(label, argv, log_handle)
            results.append((label, took, code, nlines))
            if label.startswith("公众号") and code == 0:
                wechat_note = wechat_failure_note(log_handle.name)
                if wechat_note:
                    out("")
                    out(wechat_note)

        # 三个抓取器都会自己调 refresh.build(),但公众号那步带了 --no-build,
        # 且三步都没跑(全跳过)时页面也不会更新 —— 这里统一收口重建一次。
        if any(lbl.startswith("公众号") for lbl, _ in steps):
            took, code, nlines = run_step("重建 index.html", [py, "refresh.py", "--build-only"], log_handle)
            results.append(("重建 index.html", took, code, nlines))
        log_handle.write(f"\n全部结束 {datetime.now():%Y-%m-%d %H:%M:%S}\n")

    elapsed = time.time() - started
    out("")
    out(f"== thu-know 更新结束 {datetime.now():%Y-%m-%d %H:%M:%S} ==")
    out(f"总耗时 {int(elapsed // 60)}m{int(elapsed % 60):02d}s")
    for label, took, code, nlines in results:
        mark = "OK" if code == 0 else f"退出码 {code}"
        out(f"  · {label}：{mark}（{int(took // 60)}m{int(took % 60):02d}s，{nlines} 行日志）")
    for line in summarize():
        out(line)
    out("")
    out(f"完整抓取日志：_work/run.log（{os.path.getsize(LOG_PATH) / 1024:.0f} KB，仅供排查用）")
    bad = [r for r in results if r[2] != 0]
    return 1 if bad and len(bad) == len(results) else 0


if __name__ == "__main__":
    sys.exit(main())
