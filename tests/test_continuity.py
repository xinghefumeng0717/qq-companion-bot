"""语句连贯性闸门：模型只判硬伤，代码负责选择，全部失败开放。"""

import json
import unittest
from types import SimpleNamespace

from companion.config import ReplyConfig
from companion.reply_check import check_continuity


def _payload(entries, fixed=""):
    return json.dumps({"candidates": entries, "fixed": fixed}, ensure_ascii=False)


def _entry(i, answers=True, coherent=True, consistent=True, why=""):
    return {"i": i, "answers": answers, "coherent": coherent, "consistent": consistent, "why": why}


class _Gateway:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []
        self.config = SimpleNamespace(observer_model="deepseek-flash")

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class TestContinuity(unittest.IsolatedAsyncioTestCase):
    async def test_预选通过就原样保留(self):
        gateway = _Gateway(_payload([_entry(1)], fixed="不要采用"))
        out, info = await check_continuity(gateway, "你吃了吗", ["好"], 0)
        self.assertEqual(out, "好")
        self.assertEqual(info["mode"], "keep")
        self.assertFalse(info["changed"])

    async def test_预选坏就按原规则换通过候选(self):
        gateway = _Gateway(_payload([
            _entry(1, answers=False, why="答非所问"),
            _entry(2),
            _entry(3),
        ]))
        out, info = await check_continuity(
            gateway, "你吃了吗", ["答非所问", "第二条", "第三条"], 0, tiebreak="first"
        )
        self.assertEqual(out, "第二条")
        self.assertEqual(info["mode"], "switch")
        self.assertEqual(info["choice"], 1)

    async def test_有通过候选就不采用修补(self):
        gateway = _Gateway(_payload([
            _entry(1, coherent=False),
            _entry(2),
            _entry(3),
        ], fixed="修补稿"))
        out, info = await check_continuity(gateway, "你吃了吗", ["坏", "好", "另一个好"], 0)
        self.assertEqual(out, "好")
        self.assertEqual(info["mode"], "switch")

    async def test_全部坏才允许最小修补(self):
        gateway = _Gateway(_payload([
            _entry(1, answers=False, coherent=False, consistent=False),
        ], fixed="修"))
        out, info = await check_continuity(gateway, "你吃了吗", ["坏草稿"], 0)
        self.assertEqual(out, "修")
        self.assertEqual(info["mode"], "rewrite")

    async def test_修补变长或改沉默都保留原稿(self):
        for fixed in ("变长很多的修补稿", "[沉默]"):
            with self.subTest(fixed=fixed):
                gateway = _Gateway(_payload([_entry(1, coherent=False)], fixed=fixed))
                out, info = await check_continuity(gateway, "你吃了吗", ["短"], 0)
                self.assertEqual(out, "短")
                self.assertEqual(info["mode"], "keep")

    async def test_异常和坏JSON都保留原稿(self):
        for response, error in ((RuntimeError("offline"), True), ("not-json", False), ("{}", False)):
            with self.subTest(response=response, error=error):
                gateway = _Gateway(response=response, error=error if error else None)
                out, info = await check_continuity(gateway, "你吃了吗", ["原稿"], 0)
                self.assertEqual(out, "原稿")
                self.assertFalse(info["changed"])

    async def test_沉默和空调用不请求模型(self):
        gateway = _Gateway(_payload([]))
        out, _ = await check_continuity(gateway, "", ["原稿"], 0)
        self.assertEqual(out, "原稿")
        out, _ = await check_continuity(gateway, "晚安", ["[沉默]"], 0)
        self.assertEqual(out, "[沉默]")
        self.assertEqual(gateway.calls, [])

    async def test_历史和调用参数正确(self):
        gateway = _Gateway(_payload([_entry(1)]))
        history = [
            {"role": "user", "content": "旧话", "created_at": "2026-10-24 12:00"},
            {"role": "assistant", "content": "旧回复", "created_at": "2026-10-24 12:01"},
            {"role": "user", "content": "新话", "created_at": "2026-10-24 12:02"},
            {"role": "assistant", "content": "新回复", "created_at": "2026-10-24 12:03"},
        ]
        await check_continuity(
            gateway, "最新", ["原稿"], 0, history=history,
            her_identity="小艾", his_identity="小明",
        )
        kwargs = gateway.calls[0]
        self.assertEqual(kwargs["purpose"], "reply_check")
        self.assertEqual(kwargs["model"], "deepseek-flash")
        self.assertEqual(kwargs["temperature"], 0.2)
        self.assertTrue(kwargs["json_mode"])
        self.assertEqual(kwargs["max_retries"], 0)
        prompt = kwargs["messages"][-1]["content"]
        self.assertIn("小艾", prompt)
        self.assertIn("小明", prompt)
        self.assertIn("新回复", prompt)
        self.assertNotIn("旧话", prompt)

    def test_默认关闭且超时有上限(self):
        # 2026-10-25 云端审查：真实对照里常等满 8 秒超时，先默认关，开了照常可用
        cfg = ReplyConfig()
        self.assertFalse(cfg.continuity_check)
        self.assertEqual(cfg.continuity_timeout, 8.0)


if __name__ == "__main__":
    unittest.main()
