"""M2 验收测试：人设加载、旁白剥离、切句合并、回复管道与工作记忆"""

import os
import unittest

from companion.config import ReplyConfig
from companion.db import Database
from companion.memory import MemoryManager
from companion.persona import Persona
from companion.replier import chunk_text_sentences, strip_narration, Replier
from companion.stickers import StickerManager


class TestM2Dialogue(unittest.IsolatedAsyncioTestCase):
    def test_persona_loading(self):
        persona = Persona.load("characters/example")
        self.assertEqual(persona.name, "示例角色")
        self.assertEqual(len(persona.stages), 10)
        self.assertEqual(persona.user_address, "你")
        # 测试作息表覆盖
        act_noon = persona.get_current_activity(13)
        self.assertIn("午", act_noon)
        act_night = persona.get_current_activity(2)
        self.assertIn("睡", act_night)

    def test_narration_stripping(self):
        raw = "（低头笑了笑）今天天气真好呢！*轻轻揉了揉眼睛* 你吃过晚饭了吗？"
        stripped = strip_narration(raw)
        self.assertEqual(stripped, "今天天气真好呢！ 你吃过晚饭了吗？")

        # 嵌套/全旁白
        all_narration = "（有些失落地叹了口气）"
        self.assertEqual(strip_narration(all_narration), "")

    def test_sentence_chunking(self):
        long_text = "哈喽呀！今天天气真的超级好呢。我刚刚去楼下买了一杯奶茶。路上遇到了一只特别可爱的小猫咪！它还冲我喵喵叫了两声呢。你今天过得怎么样呀？"
        chunks = chunk_text_sentences(long_text, max_chunks=5)
        self.assertLessEqual(len(chunks), 5)
        for c in chunks:
            self.assertTrue(len(c) > 0)

        # 超短句合并（<= 15 字）
        short_sentences = "在呢！好的。没问题。"
        merged = chunk_text_sentences(short_sentences, max_chunks=3)
        self.assertLessEqual(len(merged), 2)

    async def test_replier_and_turns(self):
        test_db_path = "data/test_m2.db"
        if os.path.exists(test_db_path):
            os.remove(test_db_path)

        db = Database(test_db_path)
        await db.init_tables()
        stickers = StickerManager("characters/example/stickers", db)
        replier = Replier(ReplyConfig(), stickers)

        raw_reply = "快看这个！[sticker:猫猫探头] 是不是超萌？"
        chunks, clean_text = replier.parse_reply(raw_reply)

        self.assertIn("[表情:猫猫探头]", clean_text)
        types = [c["type"] for c in chunks]
        self.assertIn("sticker", types)
        self.assertIn("text", types)

        # 验证落库
        memory = MemoryManager(db)
        await memory.save_turn_pair("发我个表情看看", clean_text)

        turns = await memory.get_recent_turns(limit=5)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["role"], "user")
        self.assertEqual(turns[1]["role"], "assistant")

        await db.close()
        if os.path.exists(test_db_path):
            os.remove(test_db_path)


if __name__ == "__main__":
    unittest.main()
