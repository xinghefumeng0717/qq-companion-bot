"""配置加载模块 (config.py)
使用 Python 3.11+ 标准库 tomllib 解析 TOML 格式配置。
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# 北京时间固定为 UTC+8（中国不实行夏令时），避免依赖系统时区数据库
BEIJING_TZ = timezone(timedelta(hours=8))


@dataclass
class AccountConfig:
    allowed_user_id: int
    bot_qq: int = 0


@dataclass
class OneBotConfig:
    ws_url: str = "ws://127.0.0.1:3001"
    access_token: str = ""


@dataclass(frozen=True)
class PriceRate:
    """单个模型的六档价格（元/百万 tokens）"""
    cache_hit_peak: float
    cache_hit_offpeak: float
    cache_miss_peak: float
    cache_miss_offpeak: float
    output_peak: float
    output_offpeak: float


DEFAULT_PRICE_RATES: Dict[str, "PriceRate"] = {
    "deepseek-flash": PriceRate(0.04, 0.02, 2.0, 1.0, 8.0, 4.0),
    "deepseek-v4-pro": PriceRate(0.30, 0.15, 9.0, 4.5, 27.0, 13.5),
}


@dataclass
class PricingConfig:
    """DeepSeek 计费（元/百万 tokens），时段按北京时间判定，按模型分别计价"""
    holidays: List[str] = field(default_factory=list)
    # 法定节假日低谷豁免是本项目的保守假设：DeepSeek 官方峰谷规则只规定
    # 工作日 9:00-12:00 / 14:00-18:00 高峰 + 周末低谷，并无节假日条款；
    # 填了日期则这些日子强制按低谷计价（宁可少收不多收）。格式 YYYY-MM-DD
    rates: Dict[str, PriceRate] = field(default_factory=lambda: dict(DEFAULT_PRICE_RATES))

    def is_peak(self, dt: Optional[datetime] = None) -> bool:
        """判定给定时刻（默认现在）是否为高峰时段：
        周一至周五（不含节假日）的 9:00-12:00、14:00-18:00 为高峰"""
        now = (dt or datetime.now(BEIJING_TZ)).astimezone(BEIJING_TZ)
        if now.strftime("%Y-%m-%d") in self.holidays:
            return False
        if now.weekday() >= 5:
            return False
        return (9 <= now.hour < 12) or (14 <= now.hour < 18)

    def rate_for(self, model: str) -> PriceRate:
        """按模型名取价目，未知模型回退到 deepseek-flash 价格（rates 缺键时回退内置默认价）"""
        rate = self.rates.get(model)
        if rate is not None:
            return rate
        fallback = self.rates.get("deepseek-flash") or DEFAULT_PRICE_RATES["deepseek-flash"]
        logger.warning(
            f"[Pricing] 未知模型 '{model}'，计费回退到 deepseek-flash 价目"
            f"{'（rates 中无该键，使用内置默认价）' if 'deepseek-flash' not in self.rates else ''}"
        )
        return fallback


@dataclass
class ModelPreset:
    provider: str
    base_url: str
    api_key: str
    chat: str
    vision: str = ""
    tasks: str = ""


def _default_presets() -> Dict[str, ModelPreset]:
    return {
        "deepseek": ModelPreset(
            provider="deepseek",
            base_url="https://api.deepseek.com",
            api_key="",
            chat="deepseek-flash",
            vision="deepseek-flash",
            tasks="deepseek-flash",
        )
    }


class LLMConfig:
    current: str
    presets: Dict[str, ModelPreset]
    thinking_chat: bool
    thinking_effort_chat: str
    thinking_tasks: bool
    thinking_effort_tasks: str
    thinking_tasks_purposes: List[str]
    pricing: PricingConfig

    # 后台任务里"写出来的文字用户看得见"的用途，默认开 low 思考（不思考的 flash 行文会不通顺）
    # FIXES16：life_arc 也开——主线的 detail/emotional_stake/resolution 会间接进入她的话，
    # 措辞质量不是装饰。不开也不炸：服务器 config.toml 零改动，靠这个代码默认值兜底。
    DEFAULT_THINKING_PURPOSES = [
        "diary_archive",
        "vision_perception",
        "proactive_message",
        "life_arc",
    ]

    def __init__(
        self,
        current: str = "deepseek",
        presets: Optional[Dict[str, ModelPreset]] = None,
        thinking_chat: bool = True,
        thinking_effort_chat: str = "high",
        thinking_tasks: bool = False,
        thinking_effort_tasks: str = "low",
        thinking_tasks_purposes: Optional[List[str]] = None,
        pricing: Optional[PricingConfig] = None,
        # [DEPRECATED] 兼容旧版参数：构造签名保留以确保外部兼容，内部推荐使用 presets 字典
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        text_model: Optional[str] = None,
        vision_model: Optional[str] = None,
        observer_model: Optional[str] = None,
    ):
        self.current = current
        self.presets = presets if presets is not None else _default_presets()
        self.thinking_chat = thinking_chat
        self.thinking_effort_chat = thinking_effort_chat
        self.thinking_tasks = thinking_tasks
        self.thinking_effort_tasks = thinking_effort_tasks
        self.thinking_tasks_purposes = (
            thinking_tasks_purposes
            if thinking_tasks_purposes is not None
            else list(self.DEFAULT_THINKING_PURPOSES)
        )
        self.pricing = pricing if pricing is not None else PricingConfig()

        if any(x is not None for x in (api_key, base_url, text_model, vision_model, observer_model)):
            if self.current not in self.presets:
                self.presets[self.current] = ModelPreset(
                    provider="deepseek",
                    base_url="https://api.deepseek.com",
                    api_key="",
                    chat="deepseek-flash",
                )
            active = self.presets[self.current]
            if api_key is not None:
                active.api_key = api_key
            if base_url is not None:
                active.base_url = base_url
            if text_model is not None:
                active.chat = text_model
            if vision_model is not None:
                active.vision = vision_model
            if observer_model is not None:
                active.tasks = observer_model

    def active(self) -> ModelPreset:
        if self.current not in self.presets:
            available = ", ".join(self.presets.keys())
            raise ValueError(f"未知的模型预设 '{self.current}'，可用预设: [{available}]")
        return self.presets[self.current]

    def thinking_for_purpose(self, purpose: str) -> bool:
        """后台任务是否对该用途开启思考：总开关优先，否则按用途白名单。"""
        return self.thinking_tasks or purpose in self.thinking_tasks_purposes

    # 兼容属性（deprecated）
    @property
    def api_key(self) -> str:
        return self.active().api_key

    @api_key.setter
    def api_key(self, val: str) -> None:
        self.active().api_key = val

    @property
    def base_url(self) -> str:
        return self.active().base_url

    @base_url.setter
    def base_url(self, val: str) -> None:
        self.active().base_url = val

    @property
    def text_model(self) -> str:
        return self.active().chat

    @text_model.setter
    def text_model(self, val: str) -> None:
        self.active().chat = val

    @property
    def vision_model(self) -> str:
        return self.active().vision

    @vision_model.setter
    def vision_model(self, val: str) -> None:
        self.active().vision = val

    @property
    def observer_model(self) -> str:
        return self.active().tasks

    @observer_model.setter
    def observer_model(self, val: str) -> None:
        self.active().tasks = val


@dataclass
class CharacterConfig:
    path: str = "characters/example"


@dataclass
class ReplyConfig:
    max_chunks: int = 5
    chunk_delay_min: float = 0.8
    chunk_delay_max: float = 2.2


@dataclass
class ProactiveConfig:
    enabled: bool = True
    wake_interval_min: int = 20
    wake_interval_max: int = 40
    quiet_hours: List[int] = field(default_factory=lambda: [0, 8])
    max_unanswered: int = 2


@dataclass
class TimingConfig:
    """回复时机人格化（FIXES15）：首条延迟 + "正在输入"视觉签名。

    两个开关互相独立：timing_enabled 只管"拿起手机"的首条延迟，
    typing_indicator_enabled 只管发送前的 typing 表演。全 False 即回到改动前行为。
    """

    timing_enabled: bool = True
    typing_indicator_enabled: bool = True
    # 首条·忙（作息表命中上课/练琴/合练/睡觉等结构化条目）：1~10 分钟
    first_reply_busy_delay_min: float = 60.0
    first_reply_busy_delay_max: float = 600.0
    # 首条·闲（回退文案"在度过属于自己的时间"/长假）：5~30 秒
    first_reply_free_delay_min: float = 5.0
    first_reply_free_delay_max: float = 30.0
    # 对话激活窗口（秒）：她在这个窗口内回过话就算"聊着呢"，后续回复一律不延迟
    active_conversation_window: float = 300.0
    # typing 展示时长：每 10 字 2 秒，钳在 3~25 秒
    typing_seconds_per_10chars: float = 2.0
    typing_min: float = 3.0
    typing_max: float = 25.0


@dataclass
class VoiceConfig:
    enabled: bool = True
    model_dir: str = "data/models/sensevoice"


@dataclass
class TTSConfig:
    """语音回复（FIXES22 阶段 A / 阶段 B provider 适配）——**默认全关**，上线后由所有者手动开

    全部字段都有代码默认值兜底：服务器 config.toml **零改动**也能起，
    `[tts]` 段不存在、或存在但不带 provider/minimax 字段时，就是
    "装着但关着、且走 edge 老路"的状态。

    provider 两档：
      · `"edge"`（默认）——阶段 A 的 edge-tts 预置音色，零成本、保留可用；
      · `"minimax"`——海螺（MiniMax）t2a_v2 预置音色。所有者盲听 5 个候选后
        亲选 `Chinese (Mandarin)_Gentle_Senior`（温柔学姐）、语速 1.0 原速。
        key 复用现有 `[models.minimax]` 档案，不新增密钥字段。
    """

    enabled: bool = False                 # 拍板：新功能一律先装死后激活
    provider: str = "edge"                # edge（阶段 A，默认）/ minimax（阶段 B）
    # ── edge 侧 ──
    voice: str = "zh-CN-XiaoxiaoNeural"    # 预置音色（所有者已否决晓晓，仅作默认占位）
    rate: str = "-8%"                     # 语速微调；+10%~+20% 会更活泼
    # ── minimax 侧（默认即所有者定稿值）──
    voice_id: str = "Chinese (Mandarin)_Gentle_Senior"   # 温柔学姐（所有者盲选定案）
    speed: float = 1.0                    # 语速 1.0 原速（0.9 慢速被淘汰）
    model: str = "speech-2.8-hd"          # t2a_v2 模型；接口只认官方枚举
    group_id: str = ""                    # 可选；t2a_v2 实测**不需要**（老接口才要）
    # ── 公共闸门 ──
    daily_limit: int = 30                 # 每日语音条数上限（所有者 2026-10-05 二次拍板：30 条≈感觉不到
                                          # 存在，但保留保险丝防模型抖动连发；不设无限）
    max_chars: int = 90                   # 单条字数上限（≈30 秒；NapCat >35s 有失败报告，别再加）


@dataclass
class AdminConfig:
    host: str = "127.0.0.1"
    port: int = 8080


@dataclass
class Config:
    account: AccountConfig
    onebot: OneBotConfig = field(default_factory=OneBotConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    character: CharacterConfig = field(default_factory=CharacterConfig)
    reply: ReplyConfig = field(default_factory=ReplyConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    timing: TimingConfig = field(default_factory=TimingConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    admin: AdminConfig = field(default_factory=AdminConfig)

    def get_holidays(self) -> List[str]:
        """法定节假日日期列表（格式 YYYY-MM-DD）。

        唯一数据源是 [llm.pricing].holidays（所有者每年手动填）。计费侧
        PricingConfig.is_peak 与行为侧（persona 作息、assembler/主动消息提示词）
        都经由这里取值，本方法只做只读转发，不写回、不做任何日期推算。
        """
        return list(self.llm.pricing.holidays)

    @classmethod
    def load(cls, config_path: str = "config.toml") -> Config:
        if not os.path.exists(config_path):
            example_path = "config.example.toml"
            if os.path.exists(example_path):
                config_path = example_path
            else:
                raise FileNotFoundError(f"配置文件未找到: {config_path}")

        with open(config_path, "rb") as f:
            data = tomllib.load(f)

        account_data = data.get("account", {})
        account = AccountConfig(
            allowed_user_id=int(account_data["allowed_user_id"]),
            bot_qq=int(account_data.get("bot_qq", 0)),
        )

        onebot_data = data.get("onebot", {})
        onebot = OneBotConfig(
            ws_url=onebot_data.get("ws_url", "ws://127.0.0.1:3001"),
            access_token=str(onebot_data.get("access_token", "")),
        )

        llm_data = data.get("llm", {})
        pricing_data = llm_data.get("pricing", {})
        rates: Dict[str, PriceRate] = {}
        for model_name, rate_data in pricing_data.items():
            if isinstance(rate_data, dict):  # 子表即某模型的六档价格
                rates[model_name] = PriceRate(
                    cache_hit_peak=float(rate_data.get("cache_hit_peak", 0.0)),
                    cache_hit_offpeak=float(rate_data.get("cache_hit_offpeak", 0.0)),
                    cache_miss_peak=float(rate_data.get("cache_miss_peak", 0.0)),
                    cache_miss_offpeak=float(rate_data.get("cache_miss_offpeak", 0.0)),
                    output_peak=float(rate_data.get("output_peak", 0.0)),
                    output_offpeak=float(rate_data.get("output_offpeak", 0.0)),
                )
        pricing = PricingConfig(
            holidays=list(pricing_data.get("holidays", [])),
            rates=rates or dict(DEFAULT_PRICE_RATES),
        )
        models_data = data.get("models", {})
        presets: Dict[str, ModelPreset] = {}
        for name, mdata in models_data.items():
            if isinstance(mdata, dict):
                presets[name] = ModelPreset(
                    provider=str(mdata.get("provider", "openai")),
                    base_url=str(mdata.get("base_url", "")),
                    api_key=str(mdata.get("api_key", "")),
                    chat=str(mdata.get("chat", "")),
                    vision=str(mdata.get("vision", "")),
                    tasks=str(mdata.get("tasks", "")),
                )

        current_name = str(llm_data.get("current", "deepseek"))
        if not presets:
            # 兼容旧版单一 [llm] 配置
            presets[current_name] = ModelPreset(
                provider="deepseek",
                base_url=str(llm_data.get("base_url", "https://api.deepseek.com")),
                api_key=str(llm_data.get("api_key", "")),
                chat=str(llm_data.get("text_model", "deepseek-flash")),
                vision=str(llm_data.get("vision_model", "deepseek-flash")),
                tasks=str(llm_data.get("observer_model", "deepseek-flash")),
            )

        llm = LLMConfig(
            current=current_name,
            presets=presets,
            thinking_chat=bool(llm_data.get("thinking_chat", True)),
            thinking_effort_chat=str(llm_data.get("thinking_effort_chat", "high")),
            thinking_tasks=bool(llm_data.get("thinking_tasks", False)),
            thinking_effort_tasks=str(llm_data.get("thinking_effort_tasks", "low")),
            thinking_tasks_purposes=llm_data.get("thinking_tasks_purposes"),
            pricing=pricing,
        )
        llm.active()

        char_data = data.get("character", {})
        character = CharacterConfig(
            path=str(char_data.get("path", "characters/example")),
        )

        reply_data = data.get("reply", {})
        reply = ReplyConfig(
            max_chunks=int(reply_data.get("max_chunks", 5)),
            chunk_delay_min=float(reply_data.get("chunk_delay_min", 0.8)),
            chunk_delay_max=float(reply_data.get("chunk_delay_max", 2.2)),
        )

        proactive_data = data.get("proactive", {})
        proactive = ProactiveConfig(
            enabled=bool(proactive_data.get("enabled", True)),
            wake_interval_min=int(proactive_data.get("wake_interval_min", 20)),
            wake_interval_max=int(proactive_data.get("wake_interval_max", 40)),
            quiet_hours=list(proactive_data.get("quiet_hours", [0, 8])),
            max_unanswered=int(proactive_data.get("max_unanswered", 2)),
        )

        # FIXES15 回复时机：整节缺失时全部走 TimingConfig 代码默认值，
        # 服务器 config.toml 零改动也能跑（所有者拍板决策 4）
        timing_data = data.get("timing", {})
        timing = TimingConfig(
            timing_enabled=bool(timing_data.get("timing_enabled", True)),
            typing_indicator_enabled=bool(timing_data.get("typing_indicator_enabled", True)),
            first_reply_busy_delay_min=float(
                timing_data.get("first_reply_busy_delay_min", 60.0)
            ),
            first_reply_busy_delay_max=float(
                timing_data.get("first_reply_busy_delay_max", 600.0)
            ),
            first_reply_free_delay_min=float(
                timing_data.get("first_reply_free_delay_min", 5.0)
            ),
            first_reply_free_delay_max=float(
                timing_data.get("first_reply_free_delay_max", 30.0)
            ),
            active_conversation_window=float(
                timing_data.get("active_conversation_window", 300.0)
            ),
            typing_seconds_per_10chars=float(
                timing_data.get("typing_seconds_per_10chars", 2.0)
            ),
            typing_min=float(timing_data.get("typing_min", 3.0)),
            typing_max=float(timing_data.get("typing_max", 25.0)),
        )

        voice_data = data.get("voice", {})
        voice = VoiceConfig(
            enabled=bool(voice_data.get("enabled", True)),
            model_dir=str(voice_data.get("model_dir", "data/models/sensevoice")),
        )
        # FIXES22：tts 段整体可缺省（服务器 config.toml 零改动也能起，默认全关）
        # 阶段 B：provider / minimax 字段同样可缺省——缺了就是 edge + 关着，
        # 老配置一个字不改也安全。
        tts_data = data.get("tts", {})
        tts = TTSConfig(
            enabled=bool(tts_data.get("enabled", False)),
            provider=str(tts_data.get("provider", "edge")),
            voice=str(tts_data.get("voice", "zh-CN-XiaoxiaoNeural")),
            rate=str(tts_data.get("rate", "-8%")),
            voice_id=str(tts_data.get("voice_id", "Chinese (Mandarin)_Gentle_Senior")),
            speed=float(tts_data.get("speed", 1.0)),
            model=str(tts_data.get("model", "speech-2.8-hd")),
            group_id=str(tts_data.get("group_id", "")),
            daily_limit=int(tts_data.get("daily_limit", 8)),
            max_chars=int(tts_data.get("max_chars", 60)),
        )
        admin_data = data.get("admin", {})
        admin = AdminConfig(
            host=str(admin_data.get("host", "127.0.0.1")),
            port=int(admin_data.get("port", 8080)),
        )

        return cls(
            account=account,
            onebot=onebot,
            llm=llm,
            character=character,
            reply=reply,
            proactive=proactive,
            timing=timing,
            voice=voice,
            tts=tts,
            admin=admin,
        )
