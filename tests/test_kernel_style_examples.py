"""新内核动态真人示例：确定性抽样、只读选择与空结果契约。"""

import random
import unittest
from datetime import datetime

from companion.style_examples import StyleExampleBank


def _turn(text="今天好累"):
    from companion.kernel.semantic import build_turn_input
    from companion.kernel.contracts import IncomingMessage

    return build_turn_input(
        session_id="style-replay",
        messages=[IncomingMessage(message_id=101, text=text)],
        received_at=datetime(2026, 10, 26, 20, 30),
    )


class TestKernelStyleExamples(unittest.TestCase):
    def test_selection_is_replayable_and_does_not_advance_shared_rng(self):
        from companion.kernel.style_view import collect_style_examples

        items = [
            {"his": f"今天实验好累{i}", "her": f"先休息一下{i}"}
            for i in range(10)
        ]
        rng = random.Random(7)
        bank = StyleExampleBank(items, rng=rng)
        rng_state = rng.getstate()

        first = collect_style_examples(bank, _turn())
        second = collect_style_examples(bank, _turn())

        self.assertEqual(first, second)
        self.assertEqual(rng.getstate(), rng_state)
        self.assertEqual(len(first.items), 3)
        self.assertEqual(list(first.indices), sorted(first.indices))
        self.assertEqual(first.query, "今天好累")
        self.assertEqual(
            first.block,
            bank.render([{"his": his, "her": her} for his, her in first.items]),
        )
        self.assertNotEqual(first.seed, 0)

    def test_selection_uses_cross_version_hash_ranking(self):
        import hashlib
        from dataclasses import replace
        from companion.kernel.style_view import collect_style_examples

        items = [
            {"his": f"今天实验好累{i}", "her": f"先休息一下{i}"}
            for i in range(10)
        ]
        bank = StyleExampleBank(items)
        for index in range(5):
            turn = replace(_turn(), session_id=f"style-replay-{index}")
            selection = collect_style_examples(bank, turn)
            ranked = sorted(
                bank.search_entries(turn.text),
                key=lambda entry: hashlib.sha256(
                    f"{selection.seed}:{entry[0]}".encode("utf-8"),
                ).digest(),
            )
            expected = tuple(sorted(entry[0] for entry in ranked[:3]))
            self.assertEqual(selection.indices, expected)

    def test_no_match_returns_empty_selection_and_no_prompt_block(self):
        from companion.kernel.style_view import collect_style_examples

        bank = StyleExampleBank([
            {"his": "明天考试好紧张", "her": "先深呼吸"},
        ])
        selection = collect_style_examples(bank, _turn("嗯"))

        self.assertEqual(selection.items, ())
        self.assertEqual(selection.indices, ())
        self.assertEqual(selection.block, "")
        self.assertEqual(selection.query, "嗯")


if __name__ == "__main__":
    unittest.main()
