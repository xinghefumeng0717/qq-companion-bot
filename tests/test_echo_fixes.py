"""复述/点评三件套：首泡裁剪、多候选择优（含防卡死）、与质检的配合"""

import asyncio
import time
import unittest

from companion.config import ReplyConfig
from companion.reply_check import (
    candidate_flaws,
    gather_candidates,
    pick_candidate,
    lead_kind,
    trim_echo_lead,
)

HIS = "我今天排练下来骨头都散了"


class TestTrim(unittest.TestCase):
    def test_首泡点评且后面有话_删掉(self):
        out, cut = trim_echo_lead(HIS, "骨头散了这形容太准了\n我今天合练也是，坐一下午腰都不是自己的")
        self.assertEqual(out, "我今天合练也是，坐一下午腰都不是自己的")
        self.assertEqual(cut, "骨头散了这形容太准了")

    def test_首泡复述原话也删(self):
        out, cut = trim_echo_lead(HIS, "排练下来骨头都散了啊\n那早点歇")
        self.assertEqual(out, "那早点歇")

    def test_只有一个气泡不动(self):
        self.assertEqual(trim_echo_lead(HIS, "骨头散了这形容太准了")[1], "")

    def test_后面只剩表情包不动(self):
        self.assertEqual(trim_echo_lead(HIS, "骨头散了这形容太准了\n[sticker:摸摸头]")[1], "")

    def test_首泡没毛病不动(self):
        self.assertEqual(trim_echo_lead(HIS, "辛苦了\n吃饭没")[1], "")

    def test_行首引用不动_表情行保留(self):
        self.assertEqual(trim_echo_lead(HIS, "[quote:1]骨头都散了是吧\n快去躺着")[1], "")
        out, _ = trim_echo_lead(HIS, "[face:捂脸]\n骨头都散了是吧\n快去躺着")
        self.assertEqual(out, "[face:捂脸]\n快去躺着")

    def test_借话表态的不删_那是带温度的那句(self):
        his = "以前练过一点\n想报一个玩玩"
        self.assertEqual(lead_kind("练过一点还叫“玩玩”啊", his), "attitude")
        self.assertEqual(trim_echo_lead(his, "练过一点还叫“玩玩”啊\n那真去吗")[1], "")
        self.assertEqual(lead_kind("不是多愁善感", "感觉过于多愁善感"), "attitude")

    def test_空复述判空接(self):
        self.assertEqual(lead_kind("行，欣赏人类情感的细腻", "我这个叫欣赏人类情感的细腻[憨笑]"), "empty")
        self.assertEqual(lead_kind("辛苦了", HIS), "clean")
        # 带问号也救不了：评他的说法本身就是点评
        self.assertEqual(lead_kind("哦？欣赏人类情感的细腻，说得还挺像回事", "我这个叫欣赏人类情感的细腻"), "empty")

    def test_删完下一句开头的不过一并去掉(self):
        out, cut = trim_echo_lead("我这个叫欣赏人类情感的细腻", "这话说得挺文艺啊\n不过你刚说的以前是啥样")
        self.assertEqual(out, "你刚说的以前是啥样")

    def test_删完只剩一句问话就不删(self):
        self.assertEqual(trim_echo_lead(HIS, "骨头散了这形容太准了\n那你今晚还练吗")[1], "")

    def test_沉默不动(self):
        self.assertEqual(trim_echo_lead(HIS, "[沉默]")[0], "[沉默]")


class TestPick(unittest.TestCase):
    def test_毛病计数(self):
        self.assertEqual(candidate_flaws(HIS, "骨头散了这形容太准了"), 2)
        self.assertEqual(candidate_flaws(HIS, "骨头都散了像被拆了一样"), 1)  # 表态不扣，比喻扣 1
        self.assertEqual(candidate_flaws(HIS, "辛苦了"), 0)

    def test_挑最干净的_并列取先到的(self):
        text, info = pick_candidate(HIS, ["骨头都散了啊\n早点睡", "辛苦了\n早点睡", "辛苦\n睡吧"])
        self.assertEqual(text, "辛苦了\n早点睡")
        self.assertEqual(info["picked"], 1)

    def test_空接比借话表态扣得重(self):
        text, _ = pick_candidate(HIS, ["骨头散了这形容太准了\n早点睡", "骨头都散了还不早点睡？"])
        self.assertEqual(text, "骨头都散了还不早点睡？")

    def test_毛病一样时挑更暖的_叮嘱不加分(self):
        text, info = pick_candidate(HIS, ["早点睡\n别熬了", "辛苦了\n我今天也排到九点[face:捂脸]"])
        self.assertEqual(info["picked"], 1)
        self.assertEqual(info["warmth"][0], 0)

    def test_有话说时沉默不许靠顺序赢(self):
        text, info = pick_candidate(HIS, ["[沉默]", "辛苦了"])
        self.assertEqual(text, "辛苦了")
        self.assertEqual(info["picked"], 1)

    def test_全员沉默才允许沉默(self):
        text, _ = pick_candidate(HIS, ["[沉默]", "[沉默]"])
        self.assertEqual(text, "[沉默]")

    def test_配置默认不开(self):
        self.assertEqual(ReplyConfig().candidates, 1)
        self.assertFalse(ReplyConfig().trim_echo)
        self.assertEqual(ReplyConfig().candidate_tiebreak, "warm")

    def test_first打平时不看温度_取最先生成的(self):
        # 两条都零毛病；第二条明显更暖。first 模式取第一条，默认 warm 仍取更暖的
        cands = ["猜你个头", "辛苦啦\n我今天也排到九点呀[face:捂脸]"]
        text, info = pick_candidate(HIS, cands, tiebreak="first")
        self.assertEqual(text, "猜你个头")
        self.assertEqual(info["picked"], 0)
        _, info2 = pick_candidate(HIS, cands)
        self.assertEqual(info2["picked"], 1)

    def test_first模式下沉默防线不变(self):
        text, _ = pick_candidate(HIS, ["[沉默]", "辛苦了"], tiebreak="first")
        self.assertEqual(text, "辛苦了")
        text2, _ = pick_candidate(HIS, ["[沉默]", "[沉默]"], tiebreak="first")
        self.assertEqual(text2, "[沉默]")


class TestGatherNoHang(unittest.IsolatedAsyncioTestCase):
    async def test_有一条永远不返回_宽限期后照常返回(self):
        calls = iter([0.01, 0.02, None])  # None = 永远卡住

        async def gen():
            d = next(calls)
            if d is None:
                await asyncio.sleep(3600)
            await asyncio.sleep(d)
            return f"回复{d}"

        t0 = time.monotonic()
        texts = await gather_candidates(gen, 3, grace=0.2)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(sorted(texts), ["回复0.01", "回复0.02"])

    async def test_全部报错_抛出给调用方走兜底(self):
        async def gen():
            raise ConnectionError("断网")

        with self.assertRaises(ConnectionError):
            await gather_candidates(gen, 3, grace=0.1)

    async def test_先到的失败_后到的成功也能用(self):
        seq = iter(["fail", "ok"])

        async def gen():
            kind = next(seq)
            if kind == "fail":
                raise TimeoutError("超时")
            await asyncio.sleep(0.05)
            return "好的"

        self.assertEqual(await gather_candidates(gen, 2, grace=0.1), ["好的"])

    async def test_空串不算成功(self):
        seq = iter(["", "嗯"])

        async def gen():
            v = next(seq)
            await asyncio.sleep(0.01 if v == "" else 0.05)
            return v

        self.assertEqual(await gather_candidates(gen, 2, grace=0.1), ["嗯"])


if __name__ == "__main__":
    unittest.main()
