"""M4 验收测试：图片缩放、表情包匹配与收藏回路"""

import os
import shutil
import unittest
from PIL import Image

from companion.db import Database
from companion.stickers import (
    StickerManager,
    image_to_base64_data_url,
    resize_image_if_needed,
)


class TestM4Stickers(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_db_path = "data/test_m4.db"
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
        self.db = Database(self.test_db_path)
        await self.db.init_tables()

        self.test_stickers_dir = "data/test_stickers"
        os.makedirs(self.test_stickers_dir, exist_ok=True)
        # 复制 example 的 index 和文件作为测试基底
        shutil.copytree("characters/example/stickers", self.test_stickers_dir, dirs_exist_ok=True)

    async def asyncTearDown(self):
        await self.db.close()
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
        if os.path.exists(self.test_stickers_dir):
            shutil.rmtree(self.test_stickers_dir)

    def test_sticker_matching(self):
        mgr = StickerManager(self.test_stickers_dir, self.db)
        # 精确匹配
        exact = mgr.match_sticker("猫猫探头")
        self.assertIsNotNone(exact)
        self.assertTrue(os.path.exists(exact))

        # 模糊匹配
        fuzzy = mgr.match_sticker("探头")
        self.assertIsNotNone(fuzzy)

        # 未匹配
        none_match = mgr.match_sticker("不存在的表情包")
        self.assertIsNone(none_match)

    def test_pillow_resizing(self):
        large_img_path = "data/test_large.jpg"
        img = Image.new("RGB", (2000, 1000), color=(100, 150, 200))
        img.save(large_img_path)

        resize_image_if_needed(large_img_path)
        with Image.open(large_img_path) as resized:
            w, h = resized.size
            self.assertLessEqual(max(w, h), 1568)
            self.assertEqual(w, 1568)

        data_url, err = image_to_base64_data_url(large_img_path)
        self.assertIsNone(err)
        self.assertTrue(data_url.startswith("data:image/jpeg;base64,"))

        if os.path.exists(large_img_path):
            os.remove(large_img_path)

    async def test_sticker_collection(self):
        mgr = StickerManager(self.test_stickers_dir, self.db)
        sample_path = "data/test_sample.png"
        img = Image.new("RGB", (100, 100), color=(255, 100, 50))
        img.save(sample_path)

        # 模拟无 LLM 降级收藏
        collected = await mgr.collect_sticker(sample_path, "测试收藏", gateway=None)
        self.assertTrue(collected)

        # 验证重复收藏去重
        duplicate = await mgr.collect_sticker(sample_path, "测试收藏", gateway=None)
        self.assertFalse(duplicate)

        if os.path.exists(sample_path):
            os.remove(sample_path)


if __name__ == "__main__":
    unittest.main()
