"""主聊连续性：关心、告别与梗不得跟着历史无限复读。"""

import unittest

from companion.prompts import SYSTEM_PROMPT_TEMPLATE


class TestMainChatContinuity(unittest.TestCase):
    def test_同一项关心不重复(self):
        self.assertIn("对话连续性", SYSTEM_PROMPT_TEMPLATE)
        self.assertIn("同一项关心或指令给过一次就不要重复", SYSTEM_PROMPT_TEMPLATE)
        self.assertIn("只接新增细节", SYSTEM_PROMPT_TEMPLATE)

    def test_告别后没有新事就短收(self):
        self.assertIn("已经道别或说睡", SYSTEM_PROMPT_TEMPLATE)
        self.assertIn("不再追加叮嘱", SYSTEM_PROMPT_TEMPLATE)

    def test_重复递梗不必每次都接(self):
        self.assertIn("反复递同一个梗", SYSTEM_PROMPT_TEMPLATE)
        self.assertIn("已经接过的就翻篇", SYSTEM_PROMPT_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
