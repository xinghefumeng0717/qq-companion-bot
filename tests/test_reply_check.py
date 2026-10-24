"""回复质检：只能让话变好，不能让话丢掉（全 mock，零真实 API）"""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from companion.reply_check import accept_fixed, check_reply

DRAFT = "表白成功以后还聊思想深度，这走向确实没想到\n本来图一乐，结果被上了一课是吧[face:捂脸]"


def gw(resp=None, exc=None, delay=0.0):
    g = MagicMock()
    g.config.observer_model = "deepseek-flash"

    async def chat(**kw):
        if delay:
            await asyncio.sleep(delay)
        if exc:
            raise exc
        return resp

    g.chat = AsyncMock(side_effect=chat)
    return g


class TestReplyCheck(unittest.IsolatedAsyncioTestCase):
    async def test_通过时原样返回(self):
        out, info = await check_reply(gw(json.dumps({"ok": True, "issues": [], "fixed": ""})), "他", DRAFT)
        self.assertEqual(out, DRAFT)
        self.assertTrue(info["checked"])
        self.assertFalse(info["changed"])

    async def test_有毛病时用改写稿(self):
        fixed = "本来图一乐，结果被上了一课是吧[face:捂脸]"
        g = gw(json.dumps({"ok": False, "issues": ["1 复述"], "fixed": fixed}, ensure_ascii=False))
        out, info = await check_reply(g, "他", DRAFT)
        self.assertEqual(out, fixed)
        self.assertTrue(info["changed"])
        self.assertEqual(g.chat.call_args.kwargs["purpose"], "reply_check")

    async def test_改写稿丢了标记就照发原稿(self):
        g = gw(json.dumps({"ok": False, "issues": ["x"], "fixed": "本来图一乐"}, ensure_ascii=False))
        out, info = await check_reply(g, "他", DRAFT)
        self.assertEqual(out, DRAFT)
        self.assertIn("标记", info["why"])

    async def test_报错超时坏JSON都照发原稿(self):
        for g in (gw(exc=RuntimeError("boom")), gw(resp="不是json"), gw(resp="{}", delay=0.5)):
            out, _ = await check_reply(g, "他", DRAFT, timeout=0.2)
            self.assertEqual(out, DRAFT)

    async def test_短草稿和沉默不调用(self):
        g = gw(json.dumps({"ok": True}))
        for d in ("好呀", "[沉默]", "晚安啦[face:耶]"):
            out, _ = await check_reply(g, "他", d)
            self.assertEqual(out, d)
        g.chat.assert_not_called()

    async def test_默认身份是中性兜底(self):
        """生产调用方会传卡与档案的身份；兜底值不能绑任何一张卡的世界观。"""
        g = gw(json.dumps({"ok": True}))
        await check_reply(g, "他", DRAFT)
        system = g.chat.call_args.kwargs["messages"][0]["content"]
        self.assertIn("写草稿的是她，她在跟他聊天", system)
        self.assertNotIn("大学生", system)

    def test_引用标记必须还在行首(self):
        self.assertFalse(accept_fixed("[quote:2] 猜不出来，你自己说吧", "猜不出来，你自己说吧[quote:2]")[0])
        self.assertTrue(accept_fixed("[quote:2] 猜不出来，你自己说吧", "[quote:2] 猜不出来")[0])

    def test_改写稿不许变长(self):
        self.assertFalse(accept_fixed("短一点", "短一点再加点")[0])
        self.assertTrue(accept_fixed("[quote:2]好啊行啊", "[quote:2]好啊")[0])


if __name__ == "__main__":
    unittest.main()
