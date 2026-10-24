"""benchmark_v5 指标纯函数（真人回复与青梓回复走同一套口径）"""

import importlib.util
import os
import unittest
from datetime import datetime

_PATH = os.path.join(os.path.dirname(__file__), "..", "scripts", "sim", "benchmark_v5.py")
_spec = importlib.util.spec_from_file_location("benchmark_v5", _PATH)
bm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bm)


class TestReplyMetrics(unittest.TestCase):
    def test_气泡与字数按换行拆_标记不计字(self):
        m = bm.reply_metrics("笑死了 当时表情包是随手打的\n[表情包]\n你呢？")
        self.assertEqual(m["bubbles"], 3)
        self.assertEqual(m["word_bubbles"], 2)
        self.assertEqual(m["chars"], [13, 2])
        self.assertEqual(m["q_end"], 1)

    def test_沉默单独计(self):
        self.assertTrue(bm.reply_metrics("[沉默]")["silence"])
        agg = bm.aggregate([bm.reply_metrics("[沉默]"), bm.reply_metrics("好")])
        self.assertEqual(agg["沉默率%"], 50.0)
        self.assertEqual(agg["单气泡≤5字%"], 100.0)

    def test_比喻与写景(self):
        m = bm.reply_metrics("湖面晨光像撒了一层碎金一样")
        self.assertEqual(m["metaphor"], 1)
        self.assertEqual(m["scenery"], 2)

    def test_时钟对齐到同星期且保留时刻(self):
        clk = bm.probe_clock({"date": "2025-09-27", "time": "23:02"})  # 2025-09-27 是周六
        self.assertEqual(clk.weekday(), datetime(2025, 9, 27).weekday())
        self.assertEqual((clk.hour, clk.minute), (23, 2))
        self.assertEqual(clk.isocalendar()[1], bm.WEEK_MONDAY.isocalendar()[1])

    def test_复述与新比喻句式(self):
        his = "后面表白成功以后他们的思想深度直接让我非常震惊[晕]"
        self.assertTrue(bm.reply_metrics("表白成功以后还聊思想深度，这走向没想到", his)["echo"])
        self.assertFalse(bm.reply_metrics("你说大数据为什么会推给你", his)["echo"])
        self.assertEqual(bm.reply_metrics("你的优先级听着跟许愿池似的")["metaphor"], 1)
        self.assertEqual(bm.reply_metrics("你怎么突然像在破案")["metaphor"], 1)


if __name__ == "__main__":
    unittest.main()
