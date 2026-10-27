#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""号池调度：多份 `wap_sid2` 凭据 + 配额 + 冷却 + 健康度。

方案 A（多号池 + getmsg 分钟级轮询）里，唯一不能靠"多写代码"解决的是**请求配额**
——单份凭据在 10 分钟内打几十次就会 `200013`。号池就是把这些配额拼起来：
N 个号 × 每号每小时 Q 次，才是你能跑的频率上限。

本模块只做四件事，不碰网络，方便离线测：

1. `acquire()`  挑一个"还没冷却、配额没用完、最久没用"的号；整池熔断时直接拒绝；
2. `report()`   把这次请求的返回值记成健康度，决定谁冷却、谁判死；
3. `breaker()`  整池熔断：撞验证墙是"环境/行为被认出来"，不是"这个号不行"；
4. `status()`   让人能看懂"现在还能不能跑、哪个号废了"。

目录约定（默认 `_work/wechat/wxmp-pool/`）：

    a1.txt        # 一行 `Cookie: ...`，与 history 用的格式一致
    a1.json       # 可选：{"uin":..., "key":..., "pass_ticket":..., "appmsg_token":...}
    a1.state.json # 本模块写：配额/冷却/健康度，不用手改
    b1.txt / b1.json / b1.state.json
    ...

**cookie 与参数必须同号配套**：`uin`/`key`/`pass_ticket` 是跟着会话走的，
拿 A 号的 cookie 配 B 号的 key 必然 `-3`。所以文件名相同的 `.txt` 与 `.json` 视为一套。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 返回值语义（getmsg 家族）
RET_OK = {0, None}
RET_NO_SESSION = -3          # cookie 里没有真 wap_sid2，或参数不配套
RET_LIMITED = 200013         # 限流 / 能力收紧（接口层，按号或按 IP）
RET_WALL = -99               # 验证墙 / 账号级异常提示（HTML、滑块、异常页）
RET_OTHER = 999999           # 网络异常/超时：不算号的错，也绝不触发熔断

DEFAULT_QUOTA = 120          # 每号每小时请求数上限（保守；600 以上基本必撞）
DEFAULT_WINDOW = 3600

COOLDOWN_LIMITED = 900       # 200013 首次冷却 15 分钟
COOLDOWN_LIMITED_MAX = 7200  # 指数退避封顶 2 小时
COOLDOWN_NO_SESSION = 3600   # -3 首次冷却 1 小时（可能只是参数没配好）
COOLDOWN_WALL = 14400        # 撞验证墙：4 小时内别再碰这个号（账号级信号）
COOLDOWN_OTHER = 300
DEAD_AFTER = 3               # 连续这么多次 -3 就判死，需要人工重抓

# 整池熔断：验证墙不是"这个号不行"，而是"这台机器/这套行为被认出来了"。
# 此时继续换号只会把还没被牵连的号一起送进去，所以停的是整池，不是单个号。
BREAKER_TRIPS = 3            # 窗口内累计这么多次撞墙就熔断
BREAKER_WINDOW = 1800        # 30 分钟滑动窗口
BREAKER_PAUSE = 3600         # 熔断后整池静默 1 小时
BREAKER_FILE = "_breaker.json"


class PoolError(RuntimeError):
    """号池不可用（全冷却 / 全判死 / 目录为空）。"""


# --------------------------------------------------------------------------- #
# 单份凭据
# --------------------------------------------------------------------------- #
@dataclass
class Cred:
    name: str
    cookie: str
    params: dict[str, str] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def alive(self) -> bool:
        return not self.state.get("dead")

    def mask(self) -> str:
        return mask_cookie(self.cookie)


def mask_cookie(cookie: str, keep: int = 6) -> str:
    """只露头尾，避免日志/终端泄凭据。"""
    cookie = (cookie or "").strip()
    if len(cookie) <= keep * 2:
        return "***" if cookie else "(空)"
    return f"{cookie[:keep]}…{cookie[-keep:]}（{len(cookie)}B）"


def read_cookie_file(path: Path) -> str:
    """读一行 `Cookie: xxx`（也容忍直接存裸 cookie）。"""
    text = path.read_text("utf-8", "replace").strip()
    if not text:
        return ""
    # 取第一行，并去掉前缀
    line = text.splitlines()[0].strip()
    return line[7:].strip() if line.lower().startswith("cookie:") else line


def atomic_write(path: Path, text: str) -> None:
    """先写临时文件再替换：任务计划程序并发跑时不会读到半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, "utf-8")
    os.replace(tmp, path)


def fresh_state(now: float, window: int = DEFAULT_WINDOW) -> dict[str, Any]:
    return {
        "window": int(now // window * window),
        "used": 0,
        "ok": 0,
        "fail": 0,
        "last_used": 0.0,
        "cooldown_until": 0.0,
        "dead": False,
        "last_ret": None,
    }


# --------------------------------------------------------------------------- #
# 号池
# --------------------------------------------------------------------------- #
class CredentialPool:
    def __init__(
        self,
        pool_dir: str | Path,
        quota: int = DEFAULT_QUOTA,
        window: int = DEFAULT_WINDOW,
    ) -> None:
        self.dir = Path(pool_dir)
        self.quota = max(1, int(quota))
        self.window = max(60, int(window))

    # -- 读写 -------------------------------------------------------------- #
    def names(self) -> list[str]:
        if not self.dir.exists():
            return []
        return sorted(p.stem for p in self.dir.glob("*.txt")
                      if not p.name.startswith(".") and not p.stem.endswith(".tmp"))

    def load(self) -> list[Cred]:
        out: list[Cred] = []
        for name in self.names():
            cpath = self.dir / f"{name}.txt"
            cookie = read_cookie_file(cpath)
            if not cookie:
                continue
            params: dict[str, str] = {}
            ppath = self.dir / f"{name}.json"
            if ppath.exists():
                try:
                    raw = json.loads(ppath.read_text("utf-8"))
                    params = {str(k): str(v) for k, v in raw.items() if v}
                except (OSError, ValueError):
                    params = {}
            out.append(Cred(name, cookie, params, self._load_state(name)))
        return out

    def _state_path(self, name: str) -> Path:
        return self.dir / f"{name}.state.json"

    def _load_state(self, name: str) -> dict[str, Any]:
        p = self._state_path(name)
        if not p.exists():
            return fresh_state(time.time(), self.window)
        try:
            data = json.loads(p.read_text("utf-8"))
            if not isinstance(data, dict):
                raise ValueError
        except (OSError, ValueError):
            return fresh_state(time.time(), self.window)
        base = fresh_state(time.time(), self.window)
        base.update({k: v for k, v in data.items() if k in base})
        return base

    def _save_state(self, cred: Cred) -> None:
        atomic_write(self._state_path(cred.name),
                     json.dumps(cred.state, ensure_ascii=False, indent=2))

    # -- 整池熔断 ---------------------------------------------------------- #
    def _breaker_path(self) -> Path:
        return self.dir / BREAKER_FILE

    def breaker(self) -> dict[str, Any]:
        """读熔断状态：{trips:[时间戳...], paused_until, reason}。"""
        base: dict[str, Any] = {"trips": [], "paused_until": 0.0, "reason": ""}
        p = self._breaker_path()
        if not p.exists():
            return base
        try:
            data = json.loads(p.read_text("utf-8"))
        except (OSError, ValueError):
            return base
        if not isinstance(data, dict):
            return base
        base.update({k: data.get(k, base[k]) for k in base})
        if not isinstance(base["trips"], list):
            base["trips"] = []
        return base

    def _save_breaker(self, br: dict[str, Any]) -> None:
        atomic_write(self._breaker_path(),
                     json.dumps(br, ensure_ascii=False, indent=2))

    def paused(self, now: float | None = None) -> float:
        """整池还要静默多少秒（0 = 没熔断）。"""
        now = time.time() if now is None else now
        return max(0.0, float(self.breaker().get("paused_until") or 0) - now)

    def resume(self) -> None:
        """人工解熔断（换出口/改频率之后再开）。"""
        self._save_breaker({"trips": [], "paused_until": 0.0, "reason": ""})

    def _trip(self, ret: int, now: float) -> None:
        br = self.breaker()
        trips = [t for t in br["trips"]
                 if isinstance(t, (int, float)) and now - t <= BREAKER_WINDOW]
        trips.append(now)
        br["trips"] = trips
        if len(trips) >= BREAKER_TRIPS and float(br.get("paused_until") or 0) <= now:
            br["paused_until"] = now + BREAKER_PAUSE
            br["reason"] = (f"{BREAKER_WINDOW // 60} 分钟内 {len(trips)} 次撞墙/账号异常"
                            f"（ret={ret}）")
        self._save_breaker(br)

    def breaker_status(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        br = self.breaker()
        left = int(max(0.0, float(br.get("paused_until") or 0) - now))
        recent = sum(1 for t in br["trips"]
                     if isinstance(t, (int, float)) and now - t <= BREAKER_WINDOW)
        if left:
            return (f"⚠ 整池熔断中，还需 {left}s（{br.get('reason') or '多次撞墙'}）\n"
                    "  换出口/降频率后人工开闸：pool --resume")
        return f"熔断器 正常（近 {BREAKER_WINDOW // 60} 分钟 {recent}/{BREAKER_TRIPS} 次信号）"

    # -- 时间窗口 ---------------------------------------------------------- #
    def _roll(self, st: dict[str, Any], now: float) -> None:
        """跨窗口就清零配额；顺便把过期的冷却标记成"未冷却"。"""
        win = int(now // self.window * self.window)
        if st.get("window") != win:
            st["window"] = win
            st["used"] = 0
        if (st.get("cooldown_until") or 0) <= now:
            st["cooldown_until"] = 0.0

    # -- 查询 -------------------------------------------------------------- #
    def usable(self, now: float | None = None) -> list[Cred]:
        now = time.time() if now is None else now
        out: list[Cred] = []
        for c in self.load():
            self._roll(c.state, now)
            if not c.alive:
                continue
            if (c.state.get("cooldown_until") or 0) > now:
                continue
            if (c.state.get("used") or 0) >= self.quota:
                continue
            out.append(c)
        return out

    def capacity_per_hour(self) -> int:
        """理论小时吞吐 = 活号数 × 每号配额。"""
        return sum(1 for c in self.load() if c.alive) * self.quota

    # -- 取号 -------------------------------------------------------------- #
    def acquire(self, now: float | None = None) -> Cred:
        """挑"用量占比最低、最久没用"的号。取不到就抛 PoolError。"""
        now = time.time() if now is None else now
        left = self.paused(now)
        if left > 0:
            br = self.breaker()
            raise PoolError(
                f"号池已熔断，还需 {int(left)} 秒（{br.get('reason') or '多次撞墙'}）。\n"
                "  熔断期间换号没有意义：撞墙说明是出口或行为模式被认出来了，\n"
                "  继续只会让更多号被牵连。确认已换出口/降频率后用 `pool --resume` 开闸。"
            )
        cands = self.usable(now)
        if not cands:
            raise PoolError(self._why_empty(now))
        cands.sort(key=lambda c: ((c.state.get("used") or 0) / self.quota,
                                  c.state.get("last_used") or 0.0))
        cred = cands[0]
        cred.state["used"] = (cred.state.get("used") or 0) + 1
        cred.state["last_used"] = now
        self._save_state(cred)
        return cred

    def _why_empty(self, now: float) -> str:
        creds = self.load()
        if not creds:
            return (f"{self.dir} 里没有可用凭据。放一份 `a1.txt`（一行 `Cookie: ...`），"
                    "或跑 tools/wap_sid2_capture.py 抓一份。")
        dead = [c.name for c in creds if not c.alive]
        cooling = [(c.name, int((c.state.get("cooldown_until") or 0) - now))
                   for c in creds if (c.state.get("cooldown_until") or 0) > now]
        full = [c.name for c in creds if (c.state.get("used") or 0) >= self.quota]
        parts = []
        if dead:
            parts.append(f"已判死 {dead}（需重抓 cookie，删掉对应 .state.json 可复活）")
        walled = [c.name for c in creds
                  if c.state.get("last_ret") == RET_WALL
                  and (c.state.get("cooldown_until") or 0) > now]
        if walled:
            parts.append(f"撞验证墙 {walled}（账号级信号，别急着换号：先降频/换出口）")
        if cooling:
            parts.append("冷却中 " + ", ".join(f"{n} 还需 {s}s" for n, s in cooling))
        if full:
            parts.append(f"本小时配额已满 {full}（quota={self.quota}/h，等下一个整点）")
        return "；".join(parts) or "无可用号"

    # -- 记结果 ------------------------------------------------------------ #
    def report(self, name: str, ret: int | None):
        """把一次请求的返回值落到健康度上。返回本次造成的冷却秒数（0 = 未惩罚）。"""
        now = time.time()
        creds = {c.name: c for c in self.load()}
        cred = creds.get(name)
        if cred is None:
            return 0
        st = cred.state
        st["last_ret"] = ret

        if ret in RET_OK:
            st["ok"] = (st.get("ok") or 0) + 1
            st["fail"] = 0
            st["cooldown_until"] = 0.0
            self._save_state(cred)
            return 0

        st["fail"] = (st.get("fail") or 0) + 1
        n = int(st["fail"])

        if ret == RET_WALL:
            # 账号级信号：单号长冷却，并且记一次整池 trip。
            # 不判死 —— 换个出口可能就好了，判死反而掩盖了真正的原因。
            cool = COOLDOWN_WALL
            self._trip(int(ret), now)
        elif ret == RET_LIMITED:
            cool = min(COOLDOWN_LIMITED * (2 ** (n - 1)), COOLDOWN_LIMITED_MAX)
        elif ret == RET_NO_SESSION:
            cool = COOLDOWN_NO_SESSION * n
            if n >= DEAD_AFTER:
                st["dead"] = True
        else:
            cool = COOLDOWN_OTHER

        st["cooldown_until"] = now + cool
        self._save_state(cred)
        return cool

    # -- 展示 -------------------------------------------------------------- #
    def status(self, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        rows: list[dict[str, Any]] = []
        for c in self.load():
            self._roll(c.state, now)
            st = c.state
            cool = int((st.get("cooldown_until") or 0) - now)
            rows.append({
                "name": c.name,
                "state": ("dead" if st.get("dead")
                          else "cooling" if cool > 0
                          else "full" if (st.get("used") or 0) >= self.quota
                          else "ready"),
                "used": st.get("used") or 0,
                "quota": self.quota,
                "ok": st.get("ok") or 0,
                "fail": st.get("fail") or 0,
                "last_ret": st.get("last_ret"),
                "cooldown": max(0, cool),
                "mask": c.mask(),
            })
        return rows


def render_status(rows: list[dict[str, Any]]) -> str:
    """把状态打成一张人看的表。"""
    if not rows:
        return "（号池为空）"
    lines = [f"{'号':10} {'状态':8} {'本小时':10} {'成功':>5} {'连败':>5} {'冷却':>6}  凭据"]
    for r in rows:
        lines.append(
            f"{r['name'][:10]:10} {r['state']:8} "
            f"{r['used']:>3}/{r['quota']:<6} {r['ok']:>5} {r['fail']:>5} "
            f"{(str(r['cooldown']) + 's') if r['cooldown'] else '-':>6}  {r['mask']}"
        )
    ready = sum(1 for r in rows if r["state"] == "ready")
    lines.append(f"—— 可立即使用 {ready}/{len(rows)}，"
                 f"理论吞吐 {ready * rows[0]['quota']} 次/小时")
    return "\n".join(lines)
