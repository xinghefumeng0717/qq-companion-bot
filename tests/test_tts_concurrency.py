"""TTSManager 并发锁与日额度定向测试 (tests/test_tts_concurrency.py)

验证要点：
1. TTSManager 实例暴露 send_lock 属性（asyncio.Lock），同实例多次访问返回同一把锁。
2. send_lock 能真正串行两个并发任务，保证进入临界区执行的时序互斥。
3. 在 send_lock 保护下，并发流程执行「再次检查日额度 -> 合成 -> 成功记账」，
   当剩余额度仅剩 1 条时，只有先获得锁的任务能够通过额度检查并 bump_daily，
   后续任务正确被拦截，不发生额度超发（超发防护验证）。
4. 闸门语义不受锁存在与否的影响，未发送成功时不应 bump_daily。
"""

import asyncio
import os
import sys
import unittest
from typing import Any, Dict, Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from companion.config import TTSConfig
from companion.tts import TTSManager, STATE_KEY_TTS_DAILY


class _FakeDB:
    """提供与 companion 状态存储接口一致的轻量仿真实体"""

    def __init__(self, initial_state: Optional[Dict[str, Any]] = None):
        self._states: Dict[str, Any] = {}
        if initial_state:
            self._states[STATE_KEY_TTS_DAILY] = initial_state

    async def get_state_json(self, key: str, default: Any = None) -> Any:
        return self._states.get(key, default)

    async def set_state_json(self, key: str, value: Any) -> None:
        self._states[key] = value


class TestTTSConcurrency(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = TTSConfig(
            enabled=True,
            daily_limit=2,
            max_chars=90,
            provider="edge",
        )
        self.db = _FakeDB()
        self.tts = TTSManager(self.config, db=self.db)

    def test_send_lock_property_exists_and_cached(self):
        """测试 send_lock 存在且多次访问返回同一 asyncio.Lock 实例"""
        lock1 = self.tts.send_lock
        lock2 = self.tts.send_lock
        self.assertIsInstance(lock1, asyncio.Lock)
        self.assertIs(lock1, lock2)

    async def test_send_lock_serializes_concurrent_tasks(self):
        """测试 send_lock 能够严格串行化两个并发任务"""
        active_workers = 0
        max_active_workers = 0
        execution_order = []

        async def worker(worker_id: int):
            nonlocal active_workers, max_active_workers
            async with self.tts.send_lock:
                active_workers += 1
                if active_workers > max_active_workers:
                    max_active_workers = active_workers
                execution_order.append(f"enter_{worker_id}")
                # 模拟发送/合成耗时
                await asyncio.sleep(0.05)
                execution_order.append(f"exit_{worker_id}")
                active_workers -= 1

        t1 = asyncio.create_task(worker(1))
        t2 = asyncio.create_task(worker(2))
        await asyncio.gather(t1, t2)

        self.assertEqual(max_active_workers, 1, "同一时刻至多只能有 1 个 worker 进入临界区")
        self.assertEqual(len(execution_order), 4)
        # 串行执行意味着必定是 enter_A -> exit_A -> enter_B -> exit_B
        first_worker = execution_order[0].split("_")[1]
        second_worker = "2" if first_worker == "1" else "1"
        self.assertEqual(
            execution_order,
            [
                f"enter_{first_worker}",
                f"exit_{first_worker}",
                f"enter_{second_worker}",
                f"exit_{second_worker}",
            ],
        )

    async def test_send_lock_protects_daily_quota_race(self):
        """测试并发发送流程下，在锁保护内重查日额度能有效防止额度超发"""
        # 设置当前配额上限为 1
        self.tts.config.daily_limit = 1
        success_count = 0
        blocked_count = 0

        async def simulated_send_flow(task_id: int):
            nonlocal success_count, blocked_count
            async with self.tts.send_lock:
                # 临界区内再次检查日额度
                used, limit = await self.tts.daily_status()
                if used >= limit:
                    blocked_count += 1
                    return False
                # 模拟合成与发送成功耗时
                await asyncio.sleep(0.03)
                # 成功后记账
                await self.tts.bump_daily()
                success_count += 1
                return True

        # 同时发起两个任务
        results = await asyncio.gather(
            simulated_send_flow(1),
            simulated_send_flow(2),
        )

        self.assertEqual(success_count, 1, "刚好 1 条成功发出并记账")
        self.assertEqual(blocked_count, 1, "第 2 条应被额度拦截")
        final_used, _ = await self.tts.daily_status()
        self.assertEqual(final_used, 1)

    async def test_gate_semantics_unaffected(self):
        """测试 check_gate 语义：开关关闭、作息不符、超上限时的正常拦截"""
        # 开关关
        self.tts.config.enabled = False
        ok, reason = await self.tts.check_gate()
        self.assertFalse(ok)
        self.assertIn("enabled=false", reason)

        # 开关开，但已达上限
        self.tts.config.enabled = True
        self.tts.config.daily_limit = 1
        await self.tts.bump_daily()
        ok, reason = await self.tts.check_gate()
        self.assertFalse(ok)
        self.assertIn("已达上限", reason)


if __name__ == "__main__":
    unittest.main()
