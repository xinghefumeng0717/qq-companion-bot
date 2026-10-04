"""表情包系统 (stickers.py)
管理角色表情包库、索引 (index.json)、模糊匹配、图片缩放与表情包收藏。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import shutil
from typing import Any, Dict, List, Optional, Tuple
from PIL import Image

from companion.db import Database, now_str
from companion.prompts import STICKER_DESC_PROMPT

logger = logging.getLogger(__name__)

MAX_STICKERS_COUNT = 200
MAX_LONG_EDGE = 1568
MAX_BASE64_SIZE = 10 * 1024 * 1024  # 10MB

# 新表情包自动打标的字段长度上限（desc 不进提示词，只是给看板/人看的说明）
DESC_MAX_CHARS = 40
MEANING_MAX_CHARS = 60
USAGE_MAX_CHARS = 60

_JSON_TAIL = re.compile(r"```(?:json)?\s*|\s*```", re.I)
# 中文模型偶尔吐全角冒号 {"画面"："a"}，这不是合法 JSON，json.loads 会直接挂。
# 只替换**键位置**的冒号（带引号包住），值里的全角冒号原样保留，不污染标注文本。
_FW_COLON_KEY = re.compile(r'"([^"]+)"\s*：\s*')


def parse_sticker_desc_json(text: str) -> Optional[Dict[str, str]]:
    """解析 STICKER_DESC_PROMPT 的三栏 JSON。失败返回 None —— 调用方据此降级，**不猜**。

    容忍模型把 JSON 包在 ```json 代码块里、键位置用全角冒号、键名尾部带冒号。
    """
    if not text:
        return None
    s = _JSON_TAIL.sub("", text.strip()).strip()
    s = _FW_COLON_KEY.sub(r'"\1": ', s)
    candidates = [s]
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j > i:
        candidates.append(s[i : j + 1])

    for cand in candidates:
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        # 三栏里"含义"是这次升级的核心，缺它就当解析失败
        meaning = _pick(obj, "含义", "意思", "meaning")
        if not meaning:
            continue
        return {
            "画面": _pick(obj, "画面", "描述", "image")[:DESC_MAX_CHARS],
            "含义": meaning[:MEANING_MAX_CHARS],
            "适用场景": _pick(obj, "适用场景", "使用场景", "场景", "usage")[:USAGE_MAX_CHARS],
        }
    return None


def _pick(obj: Dict[str, Any], *keys: str) -> str:
    """按键名取值，容忍全角/半角冒号与空格差异；只接受字符串值。"""
    norm = {}
    for k, v in obj.items():
        if isinstance(v, str):
            norm[str(k).strip().strip("：:").strip()] = v.strip()
    for k in keys:
        if norm.get(k):
            return norm[k]
    return ""


def compute_file_md5(filepath: str) -> str:
    """计算文件 MD5"""
    hasher = hashlib.md5()
    with open(filepath, "rb") as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()


def resize_image_if_needed(image_path: str) -> None:
    """如果长边超过 1568px，进行等比例缩小并覆写原图"""
    try:
        with Image.open(image_path) as img:
            w, h = img.size
            max_edge = max(w, h)
            if max_edge > MAX_LONG_EDGE:
                scale = MAX_LONG_EDGE / float(max_edge)
                new_w = int(w * scale)
                new_h = int(h * scale)
                resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                # 处理 RGBA 转 RGB
                if img.format in ("JPEG", "JPG") and resized.mode in ("RGBA", "P"):
                    resized = resized.convert("RGB")
                resized.save(image_path)
                logger.info(f"[Stickers] 图片缩放: {w}x{h} -> {new_w}x{new_h}")
    except Exception as e:
        logger.warning(f"[Stickers] 检查/缩放图片失败: {e}")


def image_to_base64_data_url(image_path: str) -> Tuple[Optional[str], Optional[str]]:
    """读取图片并转换为 base64 data url。
    如果超过 10MB 则返回 (None, "图片太大了，我看不清")
    """
    if not os.path.exists(image_path):
        return None, "找不到图片文件"

    resize_image_if_needed(image_path)

    file_size = os.path.getsize(image_path)
    if file_size > MAX_BASE64_SIZE:
        return None, "图片太大了，我看不清"

    from companion.onebot import detect_image_ext_and_mime

    with open(image_path, "rb") as f:
        data = f.read()
    _, mime = detect_image_ext_and_mime(data, image_path)
    b64 = base64.b64encode(data).decode("utf-8")
    data_url = f"data:{mime};base64,{b64}"
    return data_url, None


class StickerManager:
    def __init__(self, stickers_dir: str, db: Database):
        self.stickers_dir = stickers_dir
        self.db = db
        self.index_file = os.path.join(stickers_dir, "index.json")
        self._index: Dict[str, Dict[str, str]] = {}
        os.makedirs(self.stickers_dir, exist_ok=True)
        self.load_index()

    def load_index(self) -> None:
        """加载 stickers/index.json"""
        if os.path.exists(self.index_file):
            try:
                with open(self.index_file, "r", encoding="utf-8") as f:
                    self._index = json.load(f)
            except Exception as e:
                logger.error(f"[Stickers] 加载表情包索引失败: {e}")
                self._index = {}
        else:
            self._index = {}

    async def sync_initial_stickers(self) -> None:
        """启动时将 index.json 中的初始表情包（计算 MD5）同步进 SQLite stickers 表"""
        for name, data in self._index.items():
            rel_file = data.get("file", "")
            desc = data.get("desc", name)
            full_path = os.path.join(self.stickers_dir, rel_file)
            if os.path.exists(full_path):
                file_md5 = compute_file_md5(full_path)
                await self.db.execute(
                    """
                    INSERT OR IGNORE INTO stickers (name, file, desc, md5, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (name, rel_file, desc, file_md5, now_str()),
                )

    def save_index(self) -> None:
        """保存 index.json"""
        try:
            with open(self.index_file, "w", encoding="utf-8") as f:
                json.dump(self._index, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[Stickers] 保存表情包索引失败: {e}")

    # 提示词可用表情包列表的硬上限：当前 43 键远未到顶，超出才截断（确定性：按键名排序取前 N）
    PROMPT_STICKER_LIST_MAX = 60
    # 列表里括号内含义的显示上限（FIXES17 任务3）：太长会把提示词撑爆且模型抓不住重点
    MEANING_DISPLAY_CHARS = 15

    def get_prompt_sticker_list(self) -> List[str]:
        """获取提示词可用的表情包描述词列表（确定性：按键名排序全量返回）。

        FIXES11 任务5/E6：旧实现键数 >30 时 random.sample 随机抽 30，同一个描述词
        这轮在列表、下轮消失，模型建立不了稳定手感。这里改为排序后全量返回，
        保证同一份 index.json 每次给出完全相同的列表；超过
        PROMPT_STICKER_LIST_MAX 时按同一排序取前 N 个（仍然确定性）。
        index.json 本身不含 created_at，故截断按键名序而非入库时间序——
        43 键的现状下该分支不可达，取哪种稳定序对模型手感没有区别。

        FIXES17 任务3：返回项从纯键名改为「名称（含义）」。旧标注只是画面描述
        （"白色卡通小动物、紫底、带腮红"），她只能按画面贴话题，而表情包真正的
        货币是语用功能（这张图是"干饭/撒娇/无语"）。含义太长截断 15 字；
        没有 meaning 的老数据回退纯键名（兼容，不让格式突然变样）。
        """
        keys = sorted(self._index.keys())
        if len(keys) > self.PROMPT_STICKER_LIST_MAX:
            keys = keys[: self.PROMPT_STICKER_LIST_MAX]
        out: List[str] = []
        for k in keys:
            meaning = (self._index[k].get("meaning") or "").strip()
            if meaning:
                out.append(f"{k}（{meaning[: self.MEANING_DISPLAY_CHARS]}）")
            else:
                out.append(k)
        return out

    def match_sticker(self, word: str) -> Optional[str]:
        """按描述词匹配表情包文件路径（优先精确，失败则模糊子串匹配）
        返回表情包绝对路径，未匹配到返回 None
        """
        word = word.strip()
        if not word:
            return None

        # 1. 精确匹配
        if word in self._index:
            rel_file = self._index[word].get("file", "")
            full_path = os.path.join(self.stickers_dir, rel_file)
            if os.path.exists(full_path):
                return os.path.abspath(full_path)

        # 2. 模糊匹配（子串包含）
        for k, v in self._index.items():
            if word in k or k in word:
                rel_file = v.get("file", "")
                full_path = os.path.join(self.stickers_dir, rel_file)
                if os.path.exists(full_path):
                    return os.path.abspath(full_path)

        return None

    async def collect_sticker(
        self,
        image_path: str,
        sticker_name: str,
        gateway: Any,
    ) -> bool:
        """收藏表情包：
        1. 检查上限 (200 张，查 SQLite)
        2. MD5 去重 (查 SQLite)
        3. 复制到 stickers/ 并处理重名
        4. 调用视觉模型生成三栏标注（画面/含义/适用场景）
        5. 写入 index.json 与 SQLite

        FIXES17 任务4：旧的只产 15 字画面描述，等于把"语用功能"丢掉。这里改成
        三栏 JSON。但**降级链必须活着**：模型返回旧格式 / 解析失败 / 调 API 报错，
        都退回"名称 + 画面 desc"的旧两栏行为——不炸、不丢收藏。
        """
        count_row = await self.db.fetchone("SELECT COUNT(*) as cnt FROM stickers")
        current_cnt = count_row["cnt"] if count_row else 0
        if current_cnt >= MAX_STICKERS_COUNT:
            logger.warning("[Stickers] 表情包库已达 200 张上限，拒绝收藏新表情")
            return False

        if not os.path.exists(image_path):
            return False

        file_md5 = compute_file_md5(image_path)
        row = await self.db.fetchone("SELECT name FROM stickers WHERE md5 = ?", (file_md5,))
        if row:
            logger.info(f"[Stickers] 图片已收藏过 ({row['name']})，跳过")
            return False

        ext = os.path.splitext(image_path)[1].lower() or ".png"
        clean_name = "".join(c for c in sticker_name if c.isalnum() or c in ("_", "-")).strip()
        if not clean_name:
            clean_name = f"表情_{now_str()[:10]}"

        target_filename = f"{clean_name}{ext}"
        target_path = os.path.join(self.stickers_dir, target_filename)

        idx = 1
        while os.path.exists(target_path) or clean_name in self._index:
            target_filename = f"{clean_name}_{idx}{ext}"
            target_path = os.path.join(self.stickers_dir, target_filename)
            idx += 1

        shutil.copy2(image_path, target_path)

        # 调用视觉模型生成三栏标注（失败一律降级，绝不因为标注失败丢掉这次收藏）
        desc = clean_name
        meaning = ""
        usage = ""
        try:
            data_url, _ = image_to_base64_data_url(target_path)
            if data_url and gateway and gateway.config.vision_model:
                messages = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": STICKER_DESC_PROMPT},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ]
                desc_reply = await gateway.chat(
                    messages=messages,
                    model=gateway.config.vision_model,
                    temperature=0.5,
                    purpose="sticker_desc",
                )
                parsed = parse_sticker_desc_json(desc_reply)
                if parsed:
                    desc, meaning, usage = parsed["画面"], parsed["含义"], parsed["适用场景"]
                else:
                    # 降级 1：模型还在按旧规则回一句话 → 当成画面描述裁 15 字
                    desc = (desc_reply or "").strip()[:15] or clean_name
                    logger.info(f"[Stickers] 表情包 {clean_name} 三栏 JSON 解析失败，降级为旧格式画面描述")
        except Exception as e:
            # 降级 2：API 报错 → 连 desc 都没有，用名称兜底
            logger.warning(f"[Stickers] 视觉模型描述表情包失败，使用默认名称: {e}")

        # 写入 index.json：有含义才带 meaning/usage，字段形状向后兼容
        entry: Dict[str, str] = {"file": target_filename, "desc": desc}
        if meaning:
            entry["meaning"] = meaning
            entry["usage"] = usage
        self._index[clean_name] = entry
        self.save_index()

        # 写入 SQLite
        await self.db.execute(
            """
            INSERT OR REPLACE INTO stickers (name, file, desc, md5, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (clean_name, target_filename, desc, file_md5, now_str()),
        )
        logger.info(f"[Stickers] 成功收藏新表情包: {clean_name} -> {target_filename} ({desc})")
        return True
