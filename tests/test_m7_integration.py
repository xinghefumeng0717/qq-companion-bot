"""M7 验收测试：端到端多轮对话流水线、安全关键词拦截与代码人设解耦检查"""

import os
import unittest
from unittest.mock import AsyncMock

from companion.affection import AffectionEngine
from companion.assembler import PromptAssembler
from companion.config import ReplyConfig
from companion.db import Database
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.observer import Observer
from companion.persona import Persona
from companion.replier import Replier
from companion.safety import SafetyChecker
from companion.stickers import StickerManager


class TestM7Integration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_db_path = "data/test_m7.db"
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
        self.db = Database(self.test_db_path)
        await self.db.init_tables()

        self.persona = Persona.load("characters/example")
        self.affection = AffectionEngine(self.db, self.persona.initial_dims)
        self.mood = MoodEngine(self.db)
        self.stickers = StickerManager("characters/example/stickers", self.db)
        self.replier = Replier(ReplyConfig(), self.stickers)

    async def asyncTearDown(self):
        await self.db.close()
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)

    def test_safety_checker(self):
        # Crisis
        crisis_prompt = SafetyChecker.check_message("我最近觉得活不下去了，好累")
        self.assertIsNotNone(crisis_prompt)
        self.assertIn("400-161-9995", crisis_prompt)

        # Watch
        watch_prompt = SafetyChecker.check_message("世界上我只有你了，真的")
        self.assertIsNotNone(watch_prompt)
        self.assertIn("关系边界指引", watch_prompt)

        # Normal
        normal_prompt = SafetyChecker.check_message("今天中午吃了黄焖鸡米饭")
        self.assertIsNone(normal_prompt)

    async def test_full_pipeline_simulation(self):
        # 模拟 Gateway
        mock_gateway = AsyncMock()
        mock_gateway.config.observer_model = "deepseek-chat"
        mock_gateway.chat.return_value = """{
            "self_disclosure": 8.0,
            "responsiveness": 7.5,
            "warmth_score": 8.0,
            "resonance": 7.0,
            "moments": ["深度共情"],
            "mood_impact": {"v": 1.0, "a": 0.5, "trust": 0.08},
            "facts": ["机主打算自学 Python"],
            "followups": [{"topic": "问问他 Python 学得怎么样", "remind_after_hours": 24}],
            "done_followups": [],
            "collect_sticker": false,
            "sticker_name": "",
            "user_state": "认真且开心"
        }"""

        memory = MemoryManager(self.db, mock_gateway)
        assembler = PromptAssembler(
            self.persona, self.affection, self.mood, memory, self.stickers, self.db
        )
        observer = Observer(
            mock_gateway, self.affection, self.mood, memory, self.stickers, self.db
        )

        user_input = "我打算自学 Python，你觉得怎么样？"
        # 1. 组装 prompt
        messages, sys_prompt = await assembler.assemble_messages(user_input)
        self.assertIn("【角色】", sys_prompt)
        self.assertIn("【聊天规则】", sys_prompt)

        # 2. 模拟回复
        bot_raw_reply = "（托腮笑了笑）好呀！*揉揉头* 自学 Python 挺棒的！[sticker:开心] 有不懂的随时问我呀。"
        chunks, clean_record_text = self.replier.parse_reply(bot_raw_reply)
        self.assertNotIn("（托腮笑了笑）", clean_record_text)
        self.assertNotIn("*揉揉头*", clean_record_text)

        # 3. 落库与结算
        await memory.save_turn_pair(user_input, clean_record_text)
        await observer.settle_turn(user_input, clean_record_text)

        # 4. 验证事实与跟进是否落库
        facts = await memory.get_all_facts()
        self.assertIn("机主打算自学 Python", facts)

        fu_rows = await self.db.fetchall("SELECT topic FROM followups")
        self.assertEqual(len(fu_rows), 1)
        self.assertIn("Python", fu_rows[0]["topic"])

    def test_codebase_persona_decoupling(self):
        """验收清单 #10: 代码内全文搜索不出现任何具体角色名（example 角色卡除外）"""
        # 扫描 companion/ 目录下的所有 python 文件
        companion_dir = "companion"
        forbidden_names = ["苏晓棠", "晓棠", "林澈", "可可", "爱丽丝", "初音", "小爱"]
        for root, _, files in os.walk(companion_dir):
            for file in files:
                if file.endswith(".py"):
                    file_path = os.path.join(root, file)
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read()
                        for fn in forbidden_names:
                            self.assertNotIn(
                                fn,
                                content,
                                f"代码文件 {file_path} 中硬编码了具体角色名 '{fn}'，违反人设解耦原则",
                            )


if __name__ == "__main__":
    unittest.main()
