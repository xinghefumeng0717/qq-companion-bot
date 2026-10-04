"""FIXES19 内心旁白滤网测试集 (tests/test_fixes19.py)

病灶证据：FIXES18 对聊仿真 S1 局（data/duo_sim/S1-smoke-20261004/transcript.md
第 9 轮）她把第三人称内心旁白当普通气泡发了出去——
  「行吧，饿着躺。/ 我刚到楼门口，摸兜里还压着半块黑巧。/ 这人嘴硬，我折回去看看。」

病根：角色卡禁的是括号旁白（strip_narration 兜底），堵住了动作描写的传统形式，
却管不住"不加括号、整句内心戏直接上屏"这条漏网之路。

本测试集覆盖任务书三组：
1. 命中用例（必须丢）：S1 真实翻车原句 + 同类句式枚举 + 多行混合
2. 放行用例（误杀防线，必须留）：含"你"的行、无第三人称指代的行、行内夹杂、
   表情包段 / [沉默] / 普通短气泡不受影响
3. 两条通路（reply / proactive）各至少一个用例；丢弃后无空气泡残留

全部本地构造文本，零真实 API 调用。
"""

import unittest

from companion.config import ReplyConfig
from companion.replier import (
    INNER_NARRATION_FIRST_PERSON,
    INNER_NARRATION_THIRD_PERSON,
    SILENCE_TOKEN,
    Replier,
    drop_inner_narration_lines,
    is_inner_narration_line,
)


class _RecordingStickers:
    """假表情包管理器：按描述词直接回一个假路径（不落盘）"""

    def match_sticker(self, desc: str):
        return f"/fake/stickers/{desc}.png"

    def get_prompt_sticker_list(self):
        return ["猫猫", "狗头"]


def _replier() -> Replier:
    return Replier(ReplyConfig(), _RecordingStickers())


def _capture_logs(fn):
    """跑一次 fn 并收集 companion.replier 的日志记录。

    不用 assertLogs：它在"一条日志都没有"时会自己判失败，
    而"不该记日志"本身就是我们要验的场景。
    """
    import logging

    logger = logging.getLogger("companion.replier")
    records = []

    class _Collector(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Collector()
    old_level, old_prop = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        fn()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logger.propagate = old_prop
    return records


# S1 真实翻车原句（整行形态）
REAL_CASE = "这人嘴硬，我折回去看看。"

# 按同一病句式枚举（覆盖 a 条的每种第三人称指代 + b 条的各类动词）
ENUMERATED_CASES = [
    "他还是老样子，我去看看他。",          # 「他」人称代词 + 我去看
    "这家伙又熬夜，我决定明天说说他。",      # 这家伙 + 我决定
    "那家伙今天挺累，我琢磨他要不要休息",    # 那家伙 + 我琢磨
    "那小子又偷懒，我打算不管他了",          # 那小子 + 我打算
    "他应该还没回，我去找他吧",              # 他 + 我去找
]

# 放行用例：误杀防线
RELEASE_CASES = [
    "他说今晚不来了，你别等了",              # 转述第三人称但含「你」= 在对他说话
    "我折回去拿了趟东西，累死我了",          # 有第一人称动作但无第三人称指代
    "你要是困了就去睡，他明天还要上课",      # 含「你」优先放行
    "我也觉得他这样挺累的",                  # 「我觉」不在动词表内，条件 b 不成立
    "其他的事我回头再理",                    # 「其他」的「他」不是人称代词
    "吉他音准还是没调好",                    # 「吉他」的「他」不是人称代词
    "他们都在等消息",                        # 「他们」复数泛指，不作命中信号
    "我去看个电影",                          # 正常第一人称，无第三人称
    "行吧，饿着躺。",                        # S1 原文里的正常行，必须逐字保留
    "我刚到楼门口，摸兜里还压着半块黑巧。",   # S1 原文里的正常行，必须逐字保留
]


# ==========================================
# 1. 命中用例（必须丢）
# ==========================================


class TestInnerNarrationHit(unittest.TestCase):
    def test_S1真实翻车原句被丢弃(self):
        self.assertTrue(is_inner_narration_line(REAL_CASE))
        self.assertEqual(drop_inner_narration_lines(REAL_CASE), "")

    def test_同类句式枚举全部丢弃(self):
        for line in ENUMERATED_CASES:
            with self.subTest(line=line):
                self.assertTrue(is_inner_narration_line(line), f"漏掉了：{line}")
                self.assertEqual(drop_inner_narration_lines(line), "")

    def test_多行混合只丢旁白行正常行逐字保留(self):
        text = "\n".join([
            "行吧，饿着躺。",
            "我刚到楼门口，摸兜里还压着半块黑巧。",
            REAL_CASE,
        ])
        kept = drop_inner_narration_lines(text)
        self.assertEqual(kept, "行吧，饿着躺。\n我刚到楼门口，摸兜里还压着半块黑巧。")
        for normal in ("行吧，饿着躺。", "我刚到楼门口，摸兜里还压着半块黑巧。"):
            self.assertIn(normal, kept)

    def test_三条件缺一不可(self):
        """逐条拆开验证：只满足 a、只满足 b 都不能丢。"""
        only_a = "他嘴硬。"            # 有第三人称，无第一人称动词组
        only_b = "我折回去看看。"       # 有第一人称动词组，无第三人称
        self.assertTrue(INNER_NARRATION_THIRD_PERSON.search(only_a))
        self.assertFalse(is_inner_narration_line(only_a))
        self.assertTrue(INNER_NARRATION_FIRST_PERSON.search(only_b))
        self.assertFalse(is_inner_narration_line(only_b))

    def test_丢弃记INFO日志(self):
        with self.assertLogs("companion.replier", level="INFO") as cm:
            drop_inner_narration_lines(REAL_CASE, source="proactive")
        joined = "\n".join(cm.output)
        self.assertIn("内心旁白兜底", joined)
        self.assertIn("proactive", joined)
        self.assertIn("这人嘴硬", joined)

    def test_无丢弃时不记日志(self):
        """没有旁白行时**一条日志都不该有**。

        这里不能用 assertLogs：它在"零条日志"时会自己判失败，
        而本用例要验证的正是零条日志。手工挂 handler 收集。
        """
        records = _capture_logs(lambda: drop_inner_narration_lines("我去看个电影", "reply"))
        self.assertFalse(
            [r for r in records if "内心旁白兜底" in r.getMessage()],
            f"没丢旁白却记了日志：{records}",
        )


# ==========================================
# 2. 放行用例（误杀防线）
# ==========================================


class TestInnerNarrationRelease(unittest.TestCase):
    def test_放行用例逐字保留(self):
        for line in RELEASE_CASES:
            with self.subTest(line=line):
                self.assertFalse(is_inner_narration_line(line), f"误杀了：{line}")
                self.assertEqual(drop_inner_narration_lines(line), line)

    def test_含你的行永不被丢(self):
        """保险丝：只要行里有「你」，无论其余条件多像旁白都放行。"""
        for line in (
            "他应该还没回，我去找他吧，你要不要一起",
            "这家伙真麻烦，我觉得我该去说说他了，你说呢",
            "那小子又在偷懒，我琢磨他，你帮我看看",
        ):
            with self.subTest(line=line):
                self.assertIn("你", line)
                self.assertFalse(is_inner_narration_line(line))
                self.assertEqual(drop_inner_narration_lines(line), line)

    def test_他字边界_其他与吉他不算人称代词(self):
        for line in ("其他的我不管了", "吉他我明天带过去"):
            with self.subTest(line=line):
                self.assertFalse(is_inner_narration_line(line), f"误杀了：{line}")

    def test_他说字的行在真实语料里很常见不能滥杀(self):
        """她转述「他说…」是真人 QQ 的高频句式，宁漏勿错。"""
        for line in ("他说他今天不来了", "他说想见你"):
            with self.subTest(line=line):
                self.assertFalse(is_inner_narration_line(line))


# ==========================================
# 3. 接入 parse_reply：两条通路 + 无空气泡残留
# ==========================================


class TestParseReplyWiring(unittest.TestCase):
    def test_reply通路旁白被拦(self):
        chunks, record = _replier().parse_reply(REAL_CASE, source="reply")
        self.assertEqual(chunks, [], f"旁白不该发出去，实际发了：{chunks}")
        self.assertEqual(record, "")

    def test_proactive通路旁白同样被拦(self):
        """旁白病两条路都可能犯，两条都必须生效。"""
        chunks, record = _replier().parse_reply(REAL_CASE, source="proactive")
        self.assertEqual(chunks, [])
        self.assertEqual(record, "")

    def test_S1整条原文走真实管道只保留两条正常气泡(self):
        """拿 S1 那条真实输出走完整条发送管道。"""
        raw = "\n".join([
            "行吧，饿着躺。",
            "我刚到楼门口，摸兜里还压着半块黑巧。",
            REAL_CASE,
        ])
        chunks, record = _replier().parse_reply(raw, source="reply")
        texts = [c["content"] for c in chunks if c["type"] == "text"]
        self.assertNotIn(REAL_CASE, record)
        self.assertNotIn("这人嘴硬", "".join(texts))
        self.assertIn("行吧，饿着躺。", record)
        self.assertIn("摸兜里还压着半块黑巧", record)
        # 旁白行整行消失，不留空气泡
        self.assertTrue(all(t.strip() for t in texts), f"出现空气泡：{texts}")

    def test_丢弃后不留空气泡段(self):
        raw = "\n".join([REAL_CASE, REAL_CASE])
        chunks, record = _replier().parse_reply(raw, source="reply")
        self.assertEqual(chunks, [])
        self.assertEqual(record, "")

    def test_混合时无空白行残留(self):
        raw = "\n".join(["在呢", REAL_CASE, "你吃了没"])
        chunks, _record = _replier().parse_reply(raw, source="reply")
        texts = [c["content"] for c in chunks if c["type"] == "text"]
        self.assertTrue(all(t.strip() for t in texts), f"出现空气泡：{texts}")
        self.assertFalse(any("\n\n" in t for t in texts), f"出现空白行：{texts}")

    def test_沉默语义不受影响(self):
        chunks, record = _replier().parse_reply(SILENCE_TOKEN, source="reply")
        self.assertEqual(chunks, [])
        self.assertEqual(record, "")

    def test_表情包段不受影响(self):
        chunks, record = _replier().parse_reply("[sticker:猫猫]\n在呢", source="reply")
        self.assertTrue(any(c["type"] == "sticker" for c in chunks))
        self.assertIn("在呢", record)

    def test_表情包旁边的旁白被拦表情包保留(self):
        chunks, _record = _replier().parse_reply(
            f"[sticker:猫猫]\n{REAL_CASE}", source="reply"
        )
        self.assertEqual(len([c for c in chunks if c["type"] == "sticker"]), 1)
        self.assertFalse([c for c in chunks if c["type"] == "text"])

    def test_普通短气泡不受影响(self):
        for text in ("在呢", "嗯嗯", "刚忙完", "睡了"):
            with self.subTest(text=text):
                chunks, record = _replier().parse_reply(text, source="reply")
                self.assertEqual(len(chunks), 1)
                self.assertEqual(chunks[0]["content"], text)
                self.assertEqual(record, text)

    def test_图片占位符滤网未被破坏(self):
        """两道滤网共用同一个循环段，别把上一道挤坏。"""
        chunks, record = _replier().parse_reply("[图片]\n在呢", source="reply")
        self.assertFalse([c for c in chunks if "图片" in c.get("content", "")])
        self.assertIn("在呢", record)

    def test_括号旁白既有行为不受影响(self):
        """strip_narration 是另一道滤网，本次不动它的语义。"""
        chunks, _record = _replier().parse_reply("在呢（她笑了笑）", source="reply")
        self.assertTrue(chunks)
        self.assertNotIn("她笑了笑", chunks[0]["content"])


class TestKnownBlindSpot(unittest.TestCase):
    """已知盲区：整行判定 → 旁白被拆成多行时会漏。显式记录，不装作没有。"""

    def test_拆成两行的旁白会漏网(self):
        split = "这人嘴硬。\n我折回去看看。"
        self.assertEqual(
            drop_inner_narration_lines(split), split,
            "若这里被拦了，说明滤网已经越过整行判定，请更新任务书与盲区说明",
        )

    def test_盲区与真实翻车形态的对照(self):
        """真实翻车是整行形态，可拦；拆行形态不可拦。区别只在行边界。"""
        self.assertTrue(is_inner_narration_line(REAL_CASE))


if __name__ == "__main__":
    unittest.main()
