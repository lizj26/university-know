#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""号池调度离线测试：不起网络，全用临时目录。"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import wxmp_pool as P  # noqa: E402


def write_pool(d: Path, name: str, cookie: str = "wap_sid2=ABC==", params=None) -> None:
    (d / f"{name}.txt").write_text(f"Cookie: {cookie}\n", "utf-8")
    if params is not None:
        (d / f"{name}.json").write_text(json.dumps(params), "utf-8")


class PoolBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class TestLoading(PoolBase):
    def test_empty_dir_raises_with_hint(self):
        pool = P.CredentialPool(self.dir)
        with self.assertRaises(P.PoolError) as cm:
            pool.acquire()
        self.assertIn("没有可用凭据", str(cm.exception))

    def test_missing_dir_raises_with_hint(self):
        pool = P.CredentialPool(self.dir / "nope")
        with self.assertRaises(P.PoolError) as cm:
            pool.acquire()
        self.assertIn("没有可用凭据", str(cm.exception))

    def test_skips_file_without_cookie(self):
        write_pool(self.dir, "a1", cookie="")
        self.assertEqual(P.CredentialPool(self.dir).names(), ["a1"])
        self.assertEqual(P.CredentialPool(self.dir).load(), [])

    def test_strips_cookie_prefix_case_insensitive(self):
        (self.dir / "a1.txt").write_text("cookie: wap_sid2=XYZ==\n", "utf-8")
        creds = P.CredentialPool(self.dir).load()
        self.assertEqual(creds[0].cookie, "wap_sid2=XYZ==")

    def test_reads_sidecar_params(self):
        write_pool(self.dir, "a1", params={"uin": "MTIz", "key": "k1"})
        cred = P.CredentialPool(self.dir).load()[0]
        self.assertEqual(cred.params["uin"], "MTIz")
        self.assertEqual(cred.params["key"], "k1")

    def test_broken_sidecar_does_not_crash(self):
        write_pool(self.dir, "a1")
        (self.dir / "a1.json").write_text("{not json", "utf-8")
        self.assertEqual(P.CredentialPool(self.dir).load()[0].params, {})

    def test_names_sorted_and_ignores_tmp(self):
        for n in ("b1", "a1", "c1"):
            write_pool(self.dir, n)
        (self.dir / "d1.txt.tmp").write_text("x", "utf-8")
        self.assertEqual(P.CredentialPool(self.dir).names(), ["a1", "b1", "c1"])


class TestQuota(PoolBase):
    def test_acquire_increments_used(self):
        write_pool(self.dir, "a1")
        pool = P.CredentialPool(self.dir, quota=3)
        pool.acquire()
        pool.acquire()
        row = pool.status()[0]
        self.assertEqual(row["used"], 2)
        self.assertEqual(row["state"], "ready")

    def test_quota_exhausted_then_error(self):
        write_pool(self.dir, "a1")
        pool = P.CredentialPool(self.dir, quota=2)
        pool.acquire()
        pool.acquire()
        self.assertEqual(pool.status()[0]["state"], "full")
        with self.assertRaises(P.PoolError) as cm:
            pool.acquire()
        self.assertIn("配额已满", str(cm.exception))

    def test_window_rollover_resets_used(self):
        write_pool(self.dir, "a1")
        pool = P.CredentialPool(self.dir, quota=1, window=3600)
        now = 1_800_000_000.0
        pool.acquire(now)
        with self.assertRaises(P.PoolError):
            pool.acquire(now + 10)
        # 跨到下一个窗口就能用
        pool.acquire(now + 3601)
        self.assertEqual(pool.status(now + 3601)[0]["used"], 1)

    def test_state_survives_reload(self):
        write_pool(self.dir, "a1")
        P.CredentialPool(self.dir, quota=5).acquire()
        self.assertEqual(P.CredentialPool(self.dir, quota=5).status()[0]["used"], 1)

    def test_capacity_is_alive_count_times_quota(self):
        write_pool(self.dir, "a1")
        write_pool(self.dir, "a2")
        self.assertEqual(P.CredentialPool(self.dir, quota=100).capacity_per_hour(), 200)


class TestRotation(PoolBase):
    def test_prefers_least_used(self):
        for n in ("a1", "a2"):
            write_pool(self.dir, n)
        pool = P.CredentialPool(self.dir, quota=10)
        first = pool.acquire()
        second = pool.acquire()
        self.assertNotEqual(first.name, second.name)

    def test_round_robin_balances(self):
        for n in ("a1", "a2", "a3"):
            write_pool(self.dir, n)
        pool = P.CredentialPool(self.dir, quota=10)
        for _ in range(9):
            pool.acquire()
        used = sorted(r["used"] for r in pool.status())
        self.assertEqual(used, [3, 3, 3])

    def test_all_cooling_lists_them(self):
        write_pool(self.dir, "a1")
        pool = P.CredentialPool(self.dir, quota=10)
        pool.acquire()
        pool.report("a1", P.RET_LIMITED)
        with self.assertRaises(P.PoolError) as cm:
            pool.acquire()
        self.assertIn("冷却中", str(cm.exception))


class TestReport(PoolBase):
    def test_limited_cools_15min_first(self):
        write_pool(self.dir, "a1")
        pool = P.CredentialPool(self.dir)
        self.assertEqual(pool.report("a1", P.RET_LIMITED), 900)
        self.assertEqual(pool.status()[0]["state"], "cooling")

    def test_limited_backoff_doubles(self):
        write_pool(self.dir, "a1")
        pool = P.CredentialPool(self.dir)
        cools = [pool.report("a1", P.RET_LIMITED) for _ in range(5)]
        self.assertEqual(cools[:3], [900, 1800, 3600])
        self.assertEqual(max(cools), P.COOLDOWN_LIMITED_MAX)

    def test_no_session_cools_and_dies_after_three(self):
        write_pool(self.dir, "a1")
        pool = P.CredentialPool(self.dir)
        for i in range(P.DEAD_AFTER):
            pool.report("a1", P.RET_NO_SESSION)
        row = pool.status()[0]
        self.assertEqual(row["state"], "dead")
        self.assertEqual(row["fail"], P.DEAD_AFTER)

    def test_success_clears_cooldown_and_fail(self):
        write_pool(self.dir, "a1")
        pool = P.CredentialPool(self.dir)
        pool.report("a1", P.RET_LIMITED)
        pool.report("a1", 0)
        row = pool.status()[0]
        self.assertEqual(row["state"], "ready")
        self.assertEqual(row["fail"], 0)
        self.assertEqual(row["ok"], 1)

    def test_unknown_ret_short_cooldown(self):
        write_pool(self.dir, "a1")
        pool = P.CredentialPool(self.dir)
        self.assertEqual(pool.report("a1", 99), P.COOLDOWN_OTHER)

    def test_report_unknown_name_is_noop(self):
        write_pool(self.dir, "a1")
        self.assertEqual(P.CredentialPool(self.dir).report("ghost", 200013), 0)

    def test_dead_can_be_revived_by_deleting_state(self):
        write_pool(self.dir, "a1")
        pool = P.CredentialPool(self.dir)
        for _ in range(P.DEAD_AFTER):
            pool.report("a1", P.RET_NO_SESSION)
        self.assertEqual(pool.usable(), [])
        (self.dir / "a1.state.json").unlink()
        self.assertEqual(len(pool.usable()), 1)


class TestBreaker(PoolBase):
    """整池熔断：撞墙不是"这个号不行"，而是"环境/行为被认出来了"。"""

    def setUp(self):
        super().setUp()
        write_pool(self.dir, "a1")
        write_pool(self.dir, "b1", cookie="wap_sid2=BBB==")
        self.pool = P.CredentialPool(self.dir)

    def test_wall_puts_cred_on_long_cooldown(self):
        cool = self.pool.report("a1", P.RET_WALL)
        self.assertEqual(cool, P.COOLDOWN_WALL)
        self.assertGreaterEqual(P.COOLDOWN_WALL, 3600)      # 至少 1 小时，不是 5 分钟
        self.assertEqual([c.name for c in self.pool.usable()], ["b1"])

    def test_wall_does_not_mark_dead(self):
        """撞墙不判死：换个出口可能就好，判死会掩盖真实原因。"""
        for _ in range(P.DEAD_AFTER + 2):
            self.pool.report("a1", P.RET_WALL)
        cred = [c for c in self.pool.load() if c.name == "a1"][0]
        self.assertFalse(cred.state.get("dead"))

    def test_network_error_never_trips_breaker(self):
        for _ in range(P.BREAKER_TRIPS * 3):
            self.pool.report("a1", P.RET_OTHER)
        self.assertEqual(self.pool.paused(), 0.0)
        self.assertEqual(len(self.pool.breaker()["trips"]), 0)

    def test_limited_never_trips_breaker(self):
        """200013 是频率问题，退避就够；把它当熔断信号会让号池过度停摆。"""
        for _ in range(P.BREAKER_TRIPS * 2):
            self.pool.report("a1", P.RET_LIMITED)
        self.assertEqual(self.pool.paused(), 0.0)

    def test_trips_to_threshold_pauses_whole_pool(self):
        for name in ("a1", "b1", "a1"):
            self.pool.report(name, P.RET_WALL)
        self.assertGreater(self.pool.paused(), 0)
        with self.assertRaises(P.PoolError) as cm:
            self.pool.acquire()
        self.assertIn("熔断", str(cm.exception))

    def test_paused_pool_blocks_even_healthy_cred(self):
        write_pool(self.dir, "c1", cookie="wap_sid2=CCC==")   # 完全健康的号
        for _ in range(P.BREAKER_TRIPS - 1):
            self.pool.report("a1", P.RET_WALL)
        self.pool.report("a1", P.RET_WALL)                     # 触发熔断
        with self.assertRaises(P.PoolError) as cm:
            self.pool.acquire()                                # c1 健康也取不到
        self.assertIn("熔断", str(cm.exception))

    def test_resume_clears_state(self):
        for _ in range(P.BREAKER_TRIPS):
            self.pool.report("a1", P.RET_WALL)
        self.pool.resume()
        self.assertEqual(self.pool.paused(), 0.0)
        self.assertEqual(self.pool.breaker()["trips"], [])
        self.assertTrue(self.pool.acquire().name in {"a1", "b1"})

    def test_expired_trips_do_not_accumulate(self):
        """滑窗外的旧信号不该把号池拖进熔断。"""
        old = time.time() - P.BREAKER_WINDOW - 60
        (self.dir / P.BREAKER_FILE).write_text(
            json.dumps({"trips": [old, old], "paused_until": 0.0, "reason": ""}), "utf-8")
        self.pool.report("a1", P.RET_WALL)
        self.assertEqual(self.pool.paused(), 0.0)

    def test_breaker_status_readable_in_both_states(self):
        self.assertIn("正常", self.pool.breaker_status())
        for _ in range(P.BREAKER_TRIPS):
            self.pool.report("a1", P.RET_WALL)
        text = self.pool.breaker_status()
        self.assertIn("熔断", text)
        self.assertIn("resume", text)

    def test_wall_cred_shows_up_in_empty_reason(self):
        self.pool.report("a1", P.RET_WALL)
        self.pool.report("b1", P.RET_WALL)
        msg = ""
        try:
            self.pool.acquire()
        except P.PoolError as exc:
            msg = str(exc)
        self.assertIn("撞验证墙", msg)


class TestMasking(unittest.TestCase):
    def test_mask_hides_middle(self):
        masked = P.mask_cookie("wap_sid2=" + "A" * 40)
        self.assertIn("…", masked)
        self.assertNotIn("A" * 20, masked)

    def test_mask_empty(self):
        self.assertEqual(P.mask_cookie(""), "(空)")

    def test_render_status_does_not_crash_on_empty(self):
        self.assertEqual(P.render_status([]), "（号池为空）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
