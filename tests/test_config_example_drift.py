"""config.example.toml 与代码默认值不许漂移（DEEP_AUDIT 面 E-7 / E-8）。

example 是公开仓库里唯一的配置文档：
- E-7：FIXES15 的 `[timing]` 整节曾完全没进 example，使用者不知道有"首条延迟/正在输入"这组开关；
- E-8：`thinking_tasks_purposes` 注释写"三项"、列表只有三项，而代码默认早已是四项（多 life_arc）
  ——照 example 抄一份 config.toml 会让 life_arc 静默退回不思考，代码默认值兜底失效。

这里用 dataclass 默认值逐项对账，把这两处钉死。全部本地读文件，零真实 API。
"""

from __future__ import annotations

import os
import sys
import tomllib
import unittest
from dataclasses import fields

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from companion.config import LLMConfig, TimingConfig  # noqa: E402

EXAMPLE = os.path.join(_REPO_ROOT, "config.example.toml")


def _example_text() -> str:
    with open(EXAMPLE, "r", encoding="utf-8") as f:
        return f.read()


def _example_data() -> dict:
    with open(EXAMPLE, "rb") as f:
        return tomllib.load(f)


class TestTimingSectionInExample(unittest.TestCase):
    def test_timing整节存在(self):
        self.assertIn("timing", _example_data(), "example 缺 [timing] 整节（E-7）")

    def test_逐字段与代码默认值一致(self):
        timing = _example_data()["timing"]
        defaults = TimingConfig()
        for f in fields(TimingConfig):
            with self.subTest(field=f.name):
                self.assertIn(f.name, timing, f"example 缺字段 {f.name}")
                self.assertEqual(timing[f.name], getattr(defaults, f.name))

    def test_每个字段都带注释(self):
        inside = False
        checked = 0
        for line in _example_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                inside = stripped == "[timing]"
                continue
            if not inside or not stripped:
                continue
            with self.subTest(line=stripped):
                self.assertIn("#", line, "每个字段都要有说明注释")
            checked += 1
        self.assertEqual(checked, len(fields(TimingConfig)), "字段数与代码不一致")


class TestThinkingPurposesInExample(unittest.TestCase):
    def test_白名单与代码默认值一致且含life_arc(self):
        purposes = _example_data()["llm"]["thinking_tasks_purposes"]
        self.assertIn("life_arc", purposes, "漏了 life_arc 会让主线生成静默退回不思考（E-8）")
        self.assertEqual(sorted(purposes), sorted(LLMConfig.DEFAULT_THINKING_PURPOSES))

    def test_注释不再自称三项(self):
        text = _example_text()
        self.assertNotIn("下列三项", text)
        self.assertIn("四项", text)
        self.assertIn("life_arc", text.split("thinking_tasks_purposes")[0], "须在列表前写明要含 life_arc")


if __name__ == "__main__":
    unittest.main()
