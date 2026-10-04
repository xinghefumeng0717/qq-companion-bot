"""语音回复合成模块 (tts.py) —— FIXES22 阶段 A

与 `voice.py`（语音**输入**）对称：那边把 QQ 语音转成文字，这边把文字转成 QQ 语音。
阶段 A 只用 edge-tts 的**官方预置音色**（合规红线：绝不碰任何真人声音素材；
声音复刻属阶段 B，须真人书面授权后另立项）。

三条硬纪律（任务书所有者拍板）：
1. **默认关**（`[tts].enabled` 缺省 false）：新功能一律"先装死、后激活"，
   上线后等文字层稳定再由所有者手动开。
2. **低频**：每日条数上限 + 单条字数上限 + 作息场景闸门（她在上课/合练时不该说话）。
3. **绝不因 TTS 失败丢消息**：合成超时/异常/空文件一律返回 None，
   调用方把文本按普通文字发出去。

失败纪律的写法与 voice.py 一致：整个流程 try/except 包死，finally 清理临时文件。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from companion.config import TTSConfig

logger = logging.getLogger(__name__)

# 每日语音计数在 state 表里的键（与 proactive 的未回计数同一套机制，跨天自动清零）
STATE_KEY_TTS_DAILY = "tts_daily_count"

# 合成超时（秒）。任务书定 10s：edge-tts 走公网，超时按"发不出"降级
SYNTH_TIMEOUT = 10.0

# ============================================================================
# 作息场景闸门
# ============================================================================
# 任务书给的词表：上课/合排/合练/排练/图书馆/考试/讲座/熄灯。
# 但本卡（青梓）的 daily_routine **一个都不写这些字**（写的是"上专业必修""全团合练"
# "基础馆自习室""准备熄灯休息"）——照抄任务书词表的后果是**闸门永远打不响**，
# 等于装了个死开关。所以按本卡实际文案补齐，宁可多拦不可漏拦。
BLOCK_KEYWORDS: Tuple[str, ...] = (
    # 任务书原词表
    "上课", "合排", "合练", "排练", "图书馆", "考试", "讲座", "熄灯",
    # 按青梓 daily_routine 实际文案补的（每条都对应"不方便说话"）
    "必修", "选修课", "研讨课", "专业课",      # "教学区西教连上专业必修""上通识选修课""专业研讨课"
    "自习", "背单词", "写小论文", "刷文献",      # 基础馆自习室/晚自习/背单词写论文
    "基础馆", "借还书",                         # 这张卡里的"图书馆"叫基础馆
    "静音", "睡下", "就寝", "午睡",            # "手机静音""已经睡下了""准备就寝"
    "大排练", "练琴",                          # 大排练厅 / 独奏练琴
)

# 例外白名单：命中屏蔽词但**卡里明说空闲**的活动。
#
# ⚠ 白名单词必须取**卡里的具体措辞**，不能取"回寝室"这种宽泛词——
# 终审抓出来的误伤："回寝室午睡"含"回寝室"会被放行，可她明明在午睡；
# 反过来"晚上在寝室背单词、写小论文，此时精力充沛很适合闲聊"卡里明说适合闲聊，
# 却被"背单词/写小论文"拦下。两头都要对，词就只能取原文里那句"我空闲"的表达。
ALLOW_OVERRIDES: Tuple[str, ...] = (
    "手机在手边",      # "晚自习时间，手机在手边较为空闲"
    "较为空闲",
    "很适合闲聊",      # "此时精力充沛很适合闲聊"
    "看手机聊天",      # "回寝室洗漱，吃点水果，看手机聊天"
    "适合深度聊天",
    "看闲书",
)


def activity_allows_voice(activity: str) -> Tuple[bool, str]:
    """按活动文案判断"此刻方不方便说话"→ (允许, 原因)

    顺序：先看例外白名单（卡里明说空闲的），再看屏蔽词。两者都命中时例外赢——
    宁可在"她在自习"这种边界场景偶尔放开，也不要把她真的在睡觉/合练时叫起来。
    """
    text = (activity or "").strip()
    if not text:
        return True, "没有活动文案（拿不到作息），按允许处理"
    for kw in ALLOW_OVERRIDES:
        if kw in text:
            return True, f"例外放行（命中 {kw}）"
    for kw in BLOCK_KEYWORDS:
        if kw in text:
            return False, f"当前在「{text}」（命中 {kw}），不方便说话"
    return True, "当前活动适合说话"


def truncate_for_voice(text: str, max_chars: int) -> str:
    """把语音文本截到 max_chars 以内（宁可短不可长）。

    切在最近的句读上（。！？，、；…～），找不到就在硬截断点前留个省略号。
    任务书定 60 字 ≈ 20 秒（NapCat >35s 有失败报告，QQ 上限 60s，离边界远）。
    """
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    cut = max(head.rfind(c) for c in "。！？!?，,；;…～~")
    if cut >= max_chars // 3:      # 句读点离得太近就别切了，硬截更自然
        return head[: cut + 1].strip()
    return head.rstrip("，,、 ") + "…"


class TTSManager:
    """文本 → mp3 的合成器 + 两道闸门（开关/日上限/作息）+ 每日计数

    与 voice.VoiceProcessor 的对称点：
      · 延迟加载（edge_tts 只在第一次真的要用时才 import，模块缺失不影响主流程）
      · 失败一律降级不抛（这里返回 None，调用方发文字）
      · 临时文件先落盘再发送、发完 finally 删
    """

    def __init__(self, config: TTSConfig, db: Any = None, out_dir: str = "data/voice_out"):
        self.config = config
        self.db = db
        self.out_dir = out_dir
        os.makedirs(self.out_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # 闸门
    # ------------------------------------------------------------------

    async def check_gate(self, activity: str = "") -> Tuple[bool, str]:
        """能不能现在发语音：开关 → 日上限 → 作息场景。任一不过返回 (False, 原因)"""
        if not self.config.enabled:
            return False, "TTS 开关关闭（[tts].enabled=false）"
        used, limit = await self.daily_status()
        if used >= limit:
            return False, f"今日语音已达上限（{used}/{limit} 条）"
        ok, why = activity_allows_voice(activity)
        if not ok:
            return False, f"作息闸门：{why}"
        return True, f"可用（今日已发 {used}/{limit}）"

    async def daily_status(self) -> Tuple[int, int]:
        """今日已发条数与上限；跨天自动清零（读的时候比日期，不一致就视作 0）"""
        limit = max(0, int(self.config.daily_limit))
        if self.db is None:
            return 0, limit
        rec = await self.db.get_state_json(STATE_KEY_TTS_DAILY, default=None)
        today = datetime.now().strftime("%Y-%m-%d")
        if not isinstance(rec, dict) or rec.get("date") != today:
            return 0, limit
        try:
            return int(rec.get("count", 0)), limit
        except (TypeError, ValueError):
            return 0, limit

    async def bump_daily(self) -> int:
        """成功发出一条后计数 +1，返回今日总数"""
        today = datetime.now().strftime("%Y-%m-%d")
        used, _ = await self.daily_status()
        used += 1
        if self.db is not None:
            await self.db.set_state_json(STATE_KEY_TTS_DAILY, {"date": today, "count": used})
        return used

    # ------------------------------------------------------------------
    # 合成
    # ------------------------------------------------------------------

    def _import_edge_tts(self):
        """延迟 import：模块缺失只影响语音，不影响机器人其余部分"""
        import edge_tts  # noqa: PLC0415 - 故意延迟加载

        return edge_tts

    async def synthesize(self, text: str) -> Optional[str]:
        """文本 → mp3 落盘路径；任何失败返回 None（调用方按文字降级）

        纪律：超时 10s、异常、空文件、零字节都算失败，**不抛异常**——
        语音只是锦上添花，绝不能因为它把她的这条消息弄丢。
        """
        text = (text or "").strip()
        if not text:
            return None
        # 兜底截断：正常链路在 parse_reply 就截了（那里截才能保证"落库记录 = 实发的那段"），
        # 这里再截一次是防绕过解析期的调用方（脚本/测试直接调 synthesize）。
        # 幂等：对已截过的文本没有额外影响。
        clipped = truncate_for_voice(text, int(self.config.max_chars))
        if clipped != text:
            logger.info(
                f"[TTS] 合成前兜底截断（{len(text)} → {len(clipped)} 字）：{clipped}"
            )
            text = clipped
        if not text:
            return None
        try:
            edge_tts = self._import_edge_tts()
        except Exception as e:
            logger.warning(f"[TTS] edge-tts 不可用，语音降级为文字: {e}")
            return None

        out_path = os.path.join(self.out_dir, f"tts_{uuid.uuid4().hex[:12]}.mp3")
        try:
            comm = edge_tts.Communicate(
                text,
                self.config.voice,
                rate=self.config.rate,
            )
            await asyncio.wait_for(comm.save(out_path), timeout=SYNTH_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(f"[TTS] 合成超时（{SYNTH_TIMEOUT}s），语音降级为文字")
            return None
        except Exception as e:
            logger.warning(f"[TTS] 合成异常，语音降级为文字: {e}")
            return None

        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            logger.warning("[TTS] 合成产物为空，语音降级为文字")
            return None
        logger.info(
            f"[TTS] 合成成功: voice={self.config.voice} rate={self.config.rate} "
            f"chars={len(text)} size={os.path.getsize(out_path)}B"
        )
        return out_path

    @staticmethod
    def cleanup(path: Optional[str]) -> None:
        """删临时语音文件（发送完调用；不存在/删不掉都静默）"""
        if not path:
            return
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception as e:  # 清理失败不该影响什么，日志留痕即可
            logger.debug(f"[TTS] 清理临时文件失败（可忽略）: {path} {e}")

    @staticmethod
    def record_text(text: str) -> str:
        """落库形态：与收侧他的语音转写同格式 `（语音消息）文本`

        对称性不是审美问题：observer / 日记 / 欲言又止读的是 turns 里的文本，
        只有统一形态它们才认得出"这是一句话"，而不是"一段语音占位"。
        """
        return f"（语音消息）{(text or '').strip()}"
