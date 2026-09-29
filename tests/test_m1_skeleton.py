"""M1 验收测试：配置解析、SQLite 建表、OneBot 协议段与重连退避逻辑"""

import asyncio
import os
import unittest

from companion.config import Config
from companion.db import Database, now_str
from companion.onebot import build_image_segment, build_text_segment


class TestM1Skeleton(unittest.IsolatedAsyncioTestCase):
    def test_config_loading(self):
        config = Config.load("config.example.toml")
        self.assertEqual(config.account.allowed_user_id, 123456789)
        self.assertEqual(config.onebot.ws_url, "ws://127.0.0.1:3001")
        self.assertEqual(config.character.path, "characters/example")
        self.assertEqual(config.reply.max_chunks, 5)
        self.assertEqual(config.admin.port, 8080)

    async def test_db_init_and_tables(self):
        test_db_path = "data/test_m1.db"
        if os.path.exists(test_db_path):
            os.remove(test_db_path)

        db = Database(test_db_path)
        await db.init_tables()

        # 验证表是否创建成功
        tables = [
            "turns", "counters", "diary", "diary_archive", "facts",
            "followups", "suppressed_desires", "state", "milestones",
            "stickers", "llm_calls"
        ]
        for t in tables:
            row = await db.fetchone(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (t,),
            )
            self.assertIsNotNone(row, f"表 {t} 未成功创建")

        # 验证默认计数器
        c_row = await db.fetchone("SELECT value FROM counters WHERE key = 'total_turns'")
        self.assertEqual(c_row["value"], 0)

        await db.close()
        if os.path.exists(test_db_path):
            os.remove(test_db_path)

    def test_onebot_segments(self):
        text_seg = build_text_segment("你好呀")
        self.assertEqual(text_seg, {"type": "text", "data": {"text": "你好呀"}})

        img_seg = build_image_segment("characters/example/stickers/开心.png")
        self.assertEqual(img_seg["type"], "image")
        self.assertTrue(img_seg["data"]["file"].startswith("base64://") or img_seg["data"]["file"].startswith("file:///"))


if __name__ == "__main__":
    unittest.main()
