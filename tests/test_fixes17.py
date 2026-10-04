"""FIXES17 验收测试：表情包语义重标注（任务3 提示词呈现换代 / 任务4 新收藏打标规则换代）

覆盖：
- 任务3：`get_prompt_sticker_list` 带 meaning 时输出「名称（含义）」、含义截断 15 字、
  无 meaning 的老数据回退纯键名；两个提示词模板都含新指引句且明确"不要带括号"。
- 任务4：`parse_sticker_desc_json` 新格式/代码块包裹/缺含义栏/旧格式一律返回 None；
  `collect_sticker` 新格式写入 meaning+usage，旧格式降级为纯 desc，解析失败不丢收藏。
"""

import json
import os
import shutil
import unittest

from PIL import Image

from companion.db import Database
from companion.prompts import (
    PROACTIVE_GENERATE_PROMPT,
    STICKER_DESC_PROMPT,
    SYSTEM_PROMPT_TEMPLATE,
)
from companion.stickers import StickerManager, parse_sticker_desc_json

GUIDE = "表情包列表里括号内是它的含义，选择时按含义选；输出时 [sticker:] 里只写名称本身，不要带括号。"


class _FakeGateway:
    """只实现 collect_sticker 用到的那两个属性。"""

    def __init__(self, reply, vision_model="deepseek-flash"):
        self.reply = reply
        self.config = type("C", (), {"vision_model": vision_model})()
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class TestFixes17PromptList(unittest.IsolatedAsyncioTestCase):
    """任务3：提示词呈现换代"""

    async def asyncSetUp(self):
        self.db_path = "data/test_fixes17.db"
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        self.db = Database(self.db_path)
        await self.db.init_tables()
        self.dir = "data/test_fixes17_stickers"
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir, exist_ok=True)
        # 造三张图 + 一份 index：两张带 meaning、一张不带（老数据）
        for fn in ("甲.jpg", "乙.jpg", "丙.jpg"):
            Image.new("RGB", (8, 8), (1, 2, 3)).save(os.path.join(self.dir, fn))
        self.index = {
            "甲": {"file": "甲.jpg", "desc": "白猫紫底", "meaning": "干饭、饿死了，带一点委屈的炫耀", "usage": "喊饿时用"},
            "乙": {"file": "乙.jpg", "desc": "旧描述乙", "meaning": "无语、懒得理你", "usage": "被冷落时用"},
            "丙": {"file": "丙.jpg", "desc": "老数据没有 meaning"},
        }
        with open(os.path.join(self.dir, "index.json"), "w", encoding="utf-8") as f:
            json.dump(self.index, f, ensure_ascii=False)
        self.mgr = StickerManager(self.dir, self.db)

    async def asyncTearDown(self):
        await self.db.close()
        for p in (self.db_path,):
            if os.path.exists(p):
                os.remove(p)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_list_shows_name_and_meaning(self):
        out = self.mgr.get_prompt_sticker_list()
        self.assertIn("甲（干饭、饿死了，带一点委屈的炫耀）", out)

    def test_meaning_truncated_to_15_chars(self):
        out = self.mgr.get_prompt_sticker_list()
        item = [x for x in out if x.startswith("甲（")][0]
        inner = item[len("甲（") : -1]
        self.assertEqual(len(inner), StickerManager.MEANING_DISPLAY_CHARS)
        self.assertEqual(inner, "干饭、饿死了，带一点委屈的炫耀"[:15])

    def test_old_data_without_meaning_falls_back_to_plain_key(self):
        out = self.mgr.get_prompt_sticker_list()
        self.assertIn("丙", out)
        self.assertNotIn("丙（", "".join(out))

    def test_list_is_deterministic(self):
        self.assertEqual(self.mgr.get_prompt_sticker_list(), self.mgr.get_prompt_sticker_list())

    def test_match_sticker_still_accepts_plain_name(self):
        """列表改了格式，但 [sticker:] 里输出的仍是纯名称，match_sticker 必须还能命中。"""
        out = self.mgr.get_prompt_sticker_list()
        listed = [x for x in out if x.startswith("甲")][0]
        plain_name = listed.split("（")[0]
        self.assertIsNotNone(self.mgr.match_sticker(plain_name))

    def test_both_prompt_templates_carry_the_guide(self):
        self.assertIn(GUIDE, SYSTEM_PROMPT_TEMPLATE)
        self.assertIn(GUIDE, PROACTIVE_GENERATE_PROMPT)

    def test_prompt_templates_still_have_sticker_slot(self):
        self.assertIn("{stickers_list}", SYSTEM_PROMPT_TEMPLATE)
        self.assertIn("{stickers_list}", PROACTIVE_GENERATE_PROMPT)


class TestFixes17ParseDesc(unittest.TestCase):
    """任务4：三栏 JSON 解析，失败一律 None（由调用方降级，绝不猜）"""

    def test_parses_clean_json(self):
        got = parse_sticker_desc_json('{"画面": "鲸鱼扒饭", "含义": "干饭、饿死了", "适用场景": "聊吃饭、喊饿时用"}')
        self.assertEqual(got["画面"], "鲸鱼扒饭")
        self.assertEqual(got["含义"], "干饭、饿死了")
        self.assertEqual(got["适用场景"], "聊吃饭、喊饿时用")

    def test_parses_fenced_json(self):
        got = parse_sticker_desc_json('```json\n{"画面": "a", "含义": "b", "适用场景": "c"}\n```')
        self.assertIsNotNone(got)
        self.assertEqual(got["含义"], "b")

    def test_tolerates_full_width_colon(self):
        """中文模型常吐 {"画面"："a"}，全角冒号不是合法 JSON，必须能救回来。"""
        got = parse_sticker_desc_json('{"画面"："a", "含义"："b", "适用场景"："c"}')
        self.assertIsNotNone(got)
        self.assertEqual(got["画面"], "a")
        self.assertEqual(got["含义"], "b")
        self.assertEqual(got["适用场景"], "c")

    def test_full_width_colon_inside_value_is_preserved(self):
        """只归一化键位置的冒号；含义里自己写的全角冒号不能被改掉。"""
        got = parse_sticker_desc_json('{"画面":"a", "含义":"备注：这是真的", "适用场景":"c"}')
        self.assertIsNotNone(got)
        self.assertEqual(got["含义"], "备注：这是真的")

    def test_tolerates_key_with_trailing_colon(self):
        got = parse_sticker_desc_json('{"画面：": "a", "含义：": "b", "适用场景：": "c"}')
        self.assertIsNotNone(got)
        self.assertEqual(got["含义"], "b")

    def test_missing_meaning_is_rejected(self):
        """缺"含义"栏 = 这轮升级的核心没了，判失败并降级，不接受半截三栏。"""
        self.assertIsNone(parse_sticker_desc_json('{"画面": "白猫", "适用场景": "闲聊"}'))

    def test_old_single_sentence_format_returns_none(self):
        self.assertIsNone(parse_sticker_desc_json("白色卡通小动物，紫底，带腮红"))

    def test_garbage_returns_none(self):
        for bad in ("", "   ", "不是 JSON 的乱码", "{坏掉的 json"):
            self.assertIsNone(parse_sticker_desc_json(bad))

    def test_desc_prompt_demands_three_columns(self):
        for k in ('"画面"', '"含义"', '"适用场景"'):
            self.assertIn(k, STICKER_DESC_PROMPT)
        self.assertIn("JSON", STICKER_DESC_PROMPT)


class TestFixes17Collect(unittest.IsolatedAsyncioTestCase):
    """任务4：collect_sticker 解析链路 + 降级链"""

    async def asyncSetUp(self):
        self.db_path = "data/test_fixes17c.db"
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        self.db = Database(self.db_path)
        await self.db.init_tables()
        self.dir = "data/test_fixes17c_stickers"
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir, exist_ok=True)
        self.src = "data/test_fixes17c_src.png"
        Image.new("RGB", (8, 8), (9, 9, 9)).save(self.src)

    async def asyncTearDown(self):
        await self.db.close()
        for p in (self.db_path, self.src):
            if os.path.exists(p):
                os.remove(p)
        shutil.rmtree(self.dir, ignore_errors=True)

    def _index(self):
        with open(os.path.join(self.dir, "index.json"), "r", encoding="utf-8") as f:
            return json.load(f)

    async def test_new_format_writes_meaning_and_usage(self):
        mgr = StickerManager(self.dir, self.db)
        gw = _FakeGateway('{"画面": "鲸鱼端碗扒饭", "含义": "干饭、饿死了", "适用场景": "聊吃饭、喊饿时用"}')
        self.assertTrue(await mgr.collect_sticker(self.src, "新格式", gw))
        e = self._index()["新格式"]
        self.assertEqual(e["desc"], "鲸鱼端碗扒饭")
        self.assertEqual(e["meaning"], "干饭、饿死了")
        self.assertEqual(e["usage"], "聊吃饭、喊饿时用")
        # 收藏必须同时落 DB，不因新字段丢记录
        row = await self.db.fetchone("SELECT name FROM stickers WHERE name=?", ("新格式",))
        self.assertIsNotNone(row)

    async def test_old_format_degrades_to_desc_only(self):
        """模型还在按旧规则回一句话 → 降级为纯 desc，不炸、不丢收藏。"""
        mgr = StickerManager(self.dir, self.db)
        gw = _FakeGateway("白色卡通小动物，紫底，带腮红，很可爱的一只猫猫")
        self.assertTrue(await mgr.collect_sticker(self.src, "旧格式", gw))
        e = self._index()["旧格式"]
        self.assertEqual(e["desc"], "白色卡通小动物，紫底，带腮红，很可爱的一只猫猫"[:15])
        self.assertNotIn("meaning", e)
        self.assertNotIn("usage", e)

    async def test_api_error_still_collects_with_name_fallback(self):
        mgr = StickerManager(self.dir, self.db)
        gw = _FakeGateway(RuntimeError("boom"))
        self.assertTrue(await mgr.collect_sticker(self.src, "接口炸了", gw))
        e = self._index()["接口炸了"]
        self.assertEqual(e["desc"], "接口炸了")
        self.assertNotIn("meaning", e)

    async def test_no_gateway_still_collects(self):
        mgr = StickerManager(self.dir, self.db)
        self.assertTrue(await mgr.collect_sticker(self.src, "无网关", None))
        self.assertIn("无网关", self._index())

    async def test_parsed_sticker_shows_up_in_prompt_list(self):
        """端到端：收藏进来的新表情，提示词列表里必须带含义（任务3+4 串起来）。"""
        mgr = StickerManager(self.dir, self.db)
        gw = _FakeGateway('{"画面": "白猫举手", "含义": "无语、懒得理你", "适用场景": "被敷衍时用"}')
        await mgr.collect_sticker(self.src, "端到端", gw)
        self.assertIn("端到端（无语、懒得理你）", mgr.get_prompt_sticker_list())


if __name__ == "__main__":
    unittest.main()
