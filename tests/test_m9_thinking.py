"""思考模式参数注入测试：主聊天开 high、后台任务关、思考态不传 temperature"""

import unittest

from companion.config import LLMConfig
from companion.gateway import apply_thinking, LLMGateway


class TestThinkingParams(unittest.TestCase):
    def _base_payload(self):
        return {"model": "deepseek-flash", "messages": [], "temperature": 0.7}

    def test_thinking_enabled_high_drops_temperature(self):
        payload = apply_thinking(self._base_payload(), True, "high")
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertNotIn("temperature", payload)  # 思考态不传 temperature

    def test_thinking_disabled_keeps_temperature(self):
        payload = apply_thinking(self._base_payload(), False, "low")
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", payload)
        self.assertEqual(payload["temperature"], 0.7)

    def test_default_config_values(self):
        cfg = LLMConfig()
        self.assertTrue(cfg.thinking_chat)
        self.assertEqual(cfg.thinking_effort_chat, "high")
        self.assertFalse(cfg.thinking_tasks)
        self.assertEqual(cfg.thinking_effort_tasks, "low")

    def test_config_load_from_toml(self):
        cfg = LLMConfig()
        gw = LLMGateway(cfg)
        self.assertTrue(gw.config.thinking_chat)

    def test_thinking_per_purpose_whitelist(self):
        """用户可见行文的后台用途（日记/识图描述/主动消息）默认开思考，其余关"""
        cfg = LLMConfig()
        self.assertTrue(cfg.thinking_for_purpose("diary_archive"))
        self.assertTrue(cfg.thinking_for_purpose("vision_perception"))
        self.assertTrue(cfg.thinking_for_purpose("proactive_message"))
        self.assertFalse(cfg.thinking_for_purpose("observer"))
        self.assertFalse(cfg.thinking_for_purpose("sticker_desc"))
        self.assertFalse(cfg.thinking_for_purpose("proactive_decision"))

    def test_thinking_tasks_master_switch_covers_all(self):
        """旧版总开关 thinking_tasks=True 时所有用途都开（向后兼容）"""
        cfg = LLMConfig(thinking_tasks=True)
        self.assertTrue(cfg.thinking_for_purpose("observer"))

    def test_thinking_tasks_purposes_from_toml(self):
        """toml 里显式给出白名单时以配置为准"""
        cfg = LLMConfig(thinking_tasks_purposes=["observer"])
        self.assertTrue(cfg.thinking_for_purpose("observer"))
        self.assertFalse(cfg.thinking_for_purpose("diary_archive"))


if __name__ == "__main__":
    unittest.main()
