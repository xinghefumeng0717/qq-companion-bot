"""动态示例：检索、块格式、默认关时提示词逐字节不变、示例库不含招牌口头禅"""

import json
import os
import random
import re
import unittest

import pytest

from companion.style_examples import BLOCK_HEADER, StyleExampleBank

ROOT = os.path.join(os.path.dirname(__file__), "..")
BANK_PATH = os.path.join(ROOT, "data", "style_examples", "real_chat.json")

ITEMS = [
    {"his": "今天实验做了一整天好累", "her": "辛苦了 吃饭没", "type": "他诉苦递情绪"},
    {"his": "我刚看完一部电影", "her": "什么电影", "type": "他分享"},
    {"his": "晚安晚安", "her": "晚安", "type": "告别收尾"},
    {"his": "[表情包]\n明天考试好紧张", "her": "考什么\n你肯定没问题", "type": "他诉苦递情绪"},
]


class TestBank(unittest.TestCase):
    def setUp(self):
        self.bank = StyleExampleBank(ITEMS, rng=random.Random(0))

    def test_检索最像的排第一(self):
        self.assertEqual(self.bank.search("实验好累啊")[0]["her"], "辛苦了 吃饭没")

    def test_没有共同二元组就不注入(self):
        self.assertEqual(self.bank.build_block("嗯"), "")
        self.assertEqual(self.bank.build_block(""), "")

    def test_块格式_多行压成一行(self):
        block = self.bank.build_block("明天考试")
        self.assertTrue(block.startswith(BLOCK_HEADER))
        self.assertIn("他：[表情包] / 明天考试好紧张 → 她：考什么 / 你肯定没问题", block)
        self.assertIn("不要照抄", block)

    def test_最多抽三条(self):
        items = [{"his": f"今天好累{i}", "her": f"回{i}"} for i in range(10)]
        bank = StyleExampleBank(items, rng=random.Random(1))
        self.assertLessEqual(bank.build_block("今天好累").count("→ 她："), 3)

    def test_载入失败返回None(self):
        self.assertIsNone(StyleExampleBank.load("/nonexistent/x.json"))

    def test_字典格式库可自带表头(self):
        import tempfile
        lib = {"header": "\n【她的官方台词参考】学接法不照抄：", "items": ITEMS}
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(lib, f, ensure_ascii=False)
            path = f.name
        try:
            bank = StyleExampleBank.load(path)
            self.assertIsNotNone(bank)
            self.assertTrue(bank.build_block("明天考试").startswith(lib["header"]))
        finally:
            os.unlink(path)

    def test_列表格式库用默认表头(self):
        import tempfile
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(ITEMS, f, ensure_ascii=False)
            path = f.name
        try:
            bank = StyleExampleBank.load(path)
            self.assertTrue(bank.build_block("明天考试").startswith(BLOCK_HEADER))
        finally:
            os.unlink(path)


@pytest.mark.skipif(not os.path.isfile(BANK_PATH), reason="需要私有示例库，公开克隆时明确跳过")
class TestRealBank(unittest.TestCase):
    def test_库里没有她的招牌口头禅和长英文(self):
        with open(BANK_PATH, encoding="utf-8") as f:
            items = json.load(f)
        self.assertGreater(len(items), 300)
        tic = re.compile(r"哈哈哈|噢噢|我去|笑死|okok|可以可以|好叭|好耶|哇塞|蛙趣")
        for x in items:
            self.assertFalse(tic.search(x["her"]), x["her"])
            self.assertNotRegex(x["her"], r"\[[^\]]*\]")


class TestAssemblerHook(unittest.TestCase):
    def test_模板占位在事实之后_不破坏前缀缓存(self):
        from companion.prompts import SYSTEM_PROMPT_TEMPLATE
        t = SYSTEM_PROMPT_TEMPLATE
        self.assertIn("{life_arcs_block}{style_examples_block}{lore_block}\n【当前关系阶段", t)
        self.assertGreater(t.index("{style_examples_block}"), t.index("{current_time_str}"))

    def test_配置默认关(self):
        from companion.config import ReplyConfig
        self.assertFalse(ReplyConfig().style_examples)


if __name__ == "__main__":
    unittest.main()
