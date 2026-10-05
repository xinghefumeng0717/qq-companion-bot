"""语音回复合成模块 (tts.py) —— FIXES22 阶段 A（edge-tts）+ 阶段 B（海螺 MiniMax）

与 `voice.py`（语音**输入**）对称：那边把 QQ 语音转成文字，这边把文字转成 QQ 语音。
只用**官方预置音色**（合规红线：绝不碰任何真人声音素材；声音复刻须真人书面授权后另立项）。

provider 两档，由 `[tts].provider` 选：
  · `"edge"`（默认）——阶段 A 的 edge-tts，零成本、保留可用；
  · `"minimax"`——海螺 t2a_v2，音色 `Chinese (Mandarin)_Gentle_Senior`（温柔学姐）、
    语速 1.0，key 复用现有 `[models.minimax]` 档案（不新增密钥字段）。

三条硬纪律（任务书所有者拍板）：
1. **默认关**（`[tts].enabled` 缺省 false）：新功能一律"先装死、后激活"，
   上线后等文字层稳定再由所有者手动开。
2. **低频**：每日条数上限 + 单条字数上限 + 作息场景闸门（她在上课/合练时不该说话）。
3. **绝不因 TTS 失败丢消息**：合成超时/异常/空文件一律返回 None，
   调用方把文本按普通文字发出去。

失败纪律的写法与 voice.py 一致：整个流程 try/except 包死，finally 清理临时文件。
MiniMax 侧只走项目既有的 aiohttp（不引新依赖），**不引入 edge-tts 之外的任何新包**。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from companion.config import ModelPreset, TTSConfig

logger = logging.getLogger(__name__)

# 每日语音计数在 state 表里的键（与 proactive 的未回计数同一套机制，跨天自动清零）
STATE_KEY_TTS_DAILY = "tts_daily_count"

# 合成超时（秒）。任务书定 10s：两家都走公网，超时按"发不出"降级
SYNTH_TIMEOUT = 10.0

# ── MiniMax（海螺）t2a_v2 接口常量 ──
# 官方文档：POST https://api.minimaxi.com/v1/t2a_v2（主端点；备用 api-bj.minimaxi.com）
# 鉴权：Authorization: Bearer <API_KEY> + Content-Type: application/json
# **GroupId 不需要**：它是老接口的查询参数，t2a_v2 的 body/query 里都没有它
# （2026-10-05 实测：只带 Bearer 头、不带 GroupId，直接 status_code=0 成功）。
# 仍留一个可选 group_id 配置：万一将来接口回退成要 GroupId，改配置即可，不用改代码。
MINIMAX_DEFAULT_BASE_URL = "https://api.minimaxi.com/v1"
MINIMAX_DEFAULT_MODEL = "speech-2.8-hd"
MINIMAX_VOICE_ID_DEFAULT = "Chinese (Mandarin)_Gentle_Senior"


def minimax_endpoint(base_url: str, group_id: str = "") -> str:
    """拼 t2a_v2 的 URL：base_url 缺省/带/不带尾斜杠、或直接写了完整端点都能对。

    group_id 非空时按老接口形态挂 `?GroupId=...`（当前接口不需要，留作兼容开关）。
    """
    base = (base_url or MINIMAX_DEFAULT_BASE_URL).strip().rstrip("/")
    if not base:
        base = MINIMAX_DEFAULT_BASE_URL
    url = base if base.endswith("/t2a_v2") else f"{base}/t2a_v2"
    if group_id:
        url = f"{url}?GroupId={group_id}"
    return url


def build_minimax_payload(
    text: str, voice_id: str, speed: float, model: str
) -> Dict[str, Any]:
    """t2a_v2 请求体（纯函数，方便单测逐字段钉住）。

    字段依据官方文档（同步 HTTP）：`model` / `text` 必填；`voice_setting.voice_id`
    必填，`speed` 范围 [0.5, 2]；`audio_setting` 给 mp3 单声道。
    `output_format` 默认就是 `hex`（音频以十六进制字符串塞在 JSON 里），
    这里显式写上，避免默认值哪天变了我们这边静默解析失败。
    `language_boost="auto"`：中英混读（pre / DDL 这类）让模型自己识别语种，
    所有者已实测这个音色混读正确、无需预处理。
    """
    return {
        "model": model or MINIMAX_DEFAULT_MODEL,
        "text": text,
        "stream": False,
        "output_format": "hex",
        "language_boost": "auto",
        "voice_setting": {
            "voice_id": voice_id or MINIMAX_VOICE_ID_DEFAULT,
            "speed": float(speed),
            "vol": 1.0,
            "pitch": 0,
        },
        "audio_setting": {
            "sample_rate": 32000,
            "bitrate": 128000,
            "format": "mp3",
            "channel": 1,
        },
    }


def extract_minimax_audio(resp: Dict[str, Any]) -> Tuple[Optional[bytes], str]:
    """从 t2a_v2 响应里解出 mp3 字节；任何异常形态都返回 (None, 原因)。

    响应结构（官方文档）：
      data.audio     十六进制音频
      data.status    2 = 合成完成
      extra_info     usage_characters（计费字符数）/ audio_length（毫秒）等
      base_resp      status_code（0 = 成功）
    """
    if not isinstance(resp, dict):
        return None, "响应不是 JSON 对象"
    base_resp = resp.get("base_resp")
    if not isinstance(base_resp, dict):
        base_resp = {}
    code = base_resp.get("status_code")
    if code not in (0, "0", None):
        return None, f"接口返回错误 status_code={code} msg={base_resp.get('status_msg')!r}"
    data = resp.get("data")
    if not isinstance(data, dict):
        return None, "响应缺 data（可能被限流或参数不合法）"
    audio_hex = data.get("audio")
    if not audio_hex or not isinstance(audio_hex, str):
        return None, "响应里没有音频数据"
    try:
        audio = bytes.fromhex(audio_hex)
    except (ValueError, TypeError) as e:
        return None, f"音频十六进制解码失败: {e}"
    if not audio:
        return None, "音频长度为 0"
    return audio, ""


def _minimax_extra_note(resp: Dict[str, Any]) -> str:
    """把计费/时长等可查字段摘成一行日志（冒烟要报告"API 返回的计费字段"）"""
    if not isinstance(resp, dict):
        return ""
    extra = resp.get("extra_info") or {}
    if not isinstance(extra, dict):
        return ""
    keys = ("usage_characters", "audio_length", "audio_size", "word_count")
    parts = [f"{k}={extra[k]}" for k in keys if k in extra]
    trace = (resp or {}).get("trace_id")
    if trace:
        parts.append(f"trace_id={trace}")
    return " ".join(parts)

# ============================================================================
# 作息场景闸门
# ============================================================================
# 任务书给的词表：上课/合排/合练/排练/图书馆/考试/讲座/熄灯。
# 但角色卡的 daily_routine 往往不写这些通用词，而写它自己那套叫法（校园/工作场景
# 各有各的说法）——照抄任务书词表的后果是**闸门永远打不响**，等于装了个死开关。
# 所以按"卡里可能出现的作息用词"补齐，宁可多拦不可漏拦。
# 换卡后若新卡的作息用词不同，按同样思路把这个元组补全即可（词表是通用的
# "不方便说话"场景词，不绑定任何一张具体角色卡）。
BLOCK_KEYWORDS: Tuple[str, ...] = (
    # 任务书原词表
    "上课", "合排", "合练", "排练", "图书馆", "考试", "讲座", "熄灯",
    # 课程类专业术语（每条都对应"不方便说话"）
    "必修", "选修课", "研讨课", "专业课",
    "自习", "背单词", "写小论文", "刷文献",      # 自习/背单词/写论文
    "基础馆", "借还书",                         # 部分卡把"图书馆"叫别的名字
    "静音", "睡下", "就寝", "午睡",            # 手机静音 / 睡下 / 准备就寝
    "大排练", "练琴",                          # 排练厅 / 练琴
)

# 例外白名单：命中屏蔽词但**卡里明说空闲**的活动。
#
# ⚠ 白名单词必须取**卡里的具体措辞**，不能取"回寝室"这种宽泛词——
# 终审抓出来的误伤：一条活动同时含"回寝室"和"午睡"时会被白名单误放行，可她明明在午睡；
# 反过来卡里明说"精力充沛很适合闲聊"的活动，却被"背单词/写小论文"拦下。
# 两头都要对，词就只能取"卡里明说此刻空着"的那类表达。
ALLOW_OVERRIDES: Tuple[str, ...] = (
    "手机在手边",      # 卡里明说手机就在手边
    "较为空闲",
    "很适合闲聊",      # 卡里明说适合闲聊
    "看手机聊天",      # 卡里明说在刷手机聊天
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
    现定为 90 字 ≈ 30 秒（所有者 2026-10-05 二次拍板从 60 字上调；
    NapCat >35s 有失败报告，QQ 上限 60s，90 字仍离失败区有一段距离，别再加）。
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

    provider（`[tts].provider`）决定走哪家合成：
      · `"edge"`（默认）——edge-tts，分钟级零成本；
      · `"minimax"`——海螺 t2a_v2，鉴权 key 从**现有** `[models.minimax]` 档案读
        （`minimax_preset` 由 main 传进来；不新增密钥字段、不新建密钥文件）。

    与 voice.VoiceProcessor 的对称点：
      · 延迟加载（edge_tts 只在第一次真的要用时才 import，模块缺失不影响主流程）
      · 失败一律降级不抛（这里返回 None，调用方发文字）
      · 临时文件先落盘再发送、发完 finally 删
    """

    def __init__(
        self,
        config: TTSConfig,
        db: Any = None,
        out_dir: str = "data/voice_out",
        minimax_preset: Optional[ModelPreset] = None,
    ):
        self.config = config
        self.db = db
        self.out_dir = out_dir
        self.minimax_preset = minimax_preset
        os.makedirs(self.out_dir, exist_ok=True)

    @property
    def provider(self) -> str:
        """归一化后的 provider 名（缺省/空串按 edge，大小写不敏感）"""
        return (self.config.provider or "edge").strip().lower()

    def _minimax_credentials(self) -> Tuple[str, str]:
        """MiniMax 鉴权材料：(api_key, base_url)，都取自现有 [models.minimax] 档案"""
        preset = self.minimax_preset
        api_key = (getattr(preset, "api_key", "") or "").strip() if preset else ""
        base_url = (getattr(preset, "base_url", "") or "").strip() if preset else ""
        return api_key, base_url or MINIMAX_DEFAULT_BASE_URL

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
        provider 只在这里分流；闸门与计数两家共用（切换供应商不动账目口径）。
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

        provider = self.provider
        if provider == "minimax":
            # 兜底安全网：_synthesize_minimax 内部已把每条失败路径收干（超时/网络/
            # 错误码/坏 hex/写盘），这里再包一层是纪律要求——
            # **绝不因为合成器出任何幺蛾子把她这句话弄丢**（调用方没有 except）。
            try:
                return await self._synthesize_minimax(text)
            except Exception as e:
                logger.warning(f"[TTS] MiniMax 合成未预期地失败，语音降级为文字: {e}")
                return None
        if provider == "edge":
            return await self._synthesize_edge(text)
        # 配置写错（拼错的 provider 名）时不许静默降级成"能发出去"：
        # 报一句警告、按发不出处理，所有者能立刻从日志看出是配置问题。
        logger.warning(f"[TTS] 未知 provider={self.config.provider!r}（只认 edge/minimax），语音降级为文字")
        return None

    async def _synthesize_edge(self, text: str) -> Optional[str]:
        """阶段 A 路径：edge-tts 预置音色"""
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
            f"[TTS] 合成成功: provider=edge voice={self.config.voice} "
            f"rate={self.config.rate} chars={len(text)} size={os.path.getsize(out_path)}B"
        )
        return out_path

    async def _synthesize_minimax(self, text: str) -> Optional[str]:
        """阶段 B 路径：海螺（MiniMax）t2a_v2 预置音色

        接口事实（2026-10-05 核实 + 实测）：
          · `POST {base_url}/t2a_v2`（本机配置 base_url=https://api.minimaxi.com/v1）
          · `Authorization: Bearer <key>`，key 取自现有 `[models.minimax]` 档案
          · **不需要 GroupId**（那是老接口的查询参数）
          · 音频以 hex 塞在 `data.audio` 里，落盘前 `bytes.fromhex` 解回二进制
          · 计费看 `extra_info.usage_characters`，日志里留痕便于对账

        失败纪律与 edge 路径一字不差：超时/异常/错误码/空音频一律返回 None，
        调用方按文字发；临时文件只在**真的拿到音频**之后才写盘。
        """
        api_key, base_url = self._minimax_credentials()
        if not api_key:
            logger.warning(
                "[TTS] MiniMax 档案缺 api_key（[models.minimax].api_key），语音降级为文字"
            )
            return None
        url = minimax_endpoint(base_url, self.config.group_id)
        payload = build_minimax_payload(
            text,
            self.config.voice_id,
            self.config.speed,
            self.config.model or MINIMAX_DEFAULT_MODEL,
        )
        try:
            resp = await asyncio.wait_for(
                self._minimax_post(url, payload, api_key), timeout=SYNTH_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.warning(f"[TTS] MiniMax 合成超时（{SYNTH_TIMEOUT}s），语音降级为文字")
            return None
        except Exception as e:
            # aiohttp 网络异常 / 非 JSON 响应 / 任何想不到的错，都在这里兜住
            logger.warning(f"[TTS] MiniMax 合成异常，语音降级为文字: {e}")
            return None

        audio, why = extract_minimax_audio(resp)
        if audio is None:
            logger.warning(f"[TTS] MiniMax 未拿到音频（{why}），语音降级为文字")
            return None

        out_path = os.path.join(self.out_dir, f"tts_{uuid.uuid4().hex[:12]}.mp3")
        try:
            with open(out_path, "wb") as f:
                f.write(audio)
        except Exception as e:
            logger.warning(f"[TTS] MiniMax 音频写盘失败，语音降级为文字: {e}")
            return None
        if os.path.getsize(out_path) == 0:
            logger.warning("[TTS] MiniMax 合成产物为空，语音降级为文字")
            self.cleanup(out_path)
            return None
        logger.info(
            f"[TTS] 合成成功: provider=minimax voice={self.config.voice_id} "
            f"speed={self.config.speed} model={payload['model']} "
            f"chars={len(text)} size={os.path.getsize(out_path)}B "
            f"| {_minimax_extra_note(resp)}"
        )
        return out_path

    async def _minimax_post(
        self, url: str, payload: Dict[str, Any], api_key: str
    ) -> Dict[str, Any]:
        """真正发 HTTP 的那一层（单独拆出来：单测可用替身注入、超时在外面包死）

        aiohttp 走项目既有依赖，不引新包。
        两类失败都往上抛、由调用方统一降级：HTTP 状态码非 200（带状态码与响应体），
        以及 200 但响应体不是 JSON（网关塞了 HTML 这种，json 抛 ValueError）。
        """
        import aiohttp  # noqa: PLC0415 - 与 edge 的延迟 import 同款，本地依赖

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=SYNTH_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                raw = await resp.text()
                status = resp.status
        if status != 200:
            # HTTP 层不 200（401 key 不对 / 429 限流 / 5xx）：把状态码与响应体带进错误里，
            # 日志能直接看出病因（这里**先于** JSON 解析，免得网关返回 HTML 时
            # 只报一句 json 解析失败、把真正有用的 401 吞掉）。
            raise RuntimeError(f"HTTP {status}: {str(raw)[:200]}")
        return json.loads(raw)

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
