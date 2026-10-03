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
    DEFAULT_THINKING_PURPOSES = ["diary_archive", "vision_perception", "proactive_message"]

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
class VoiceConfig:
    enabled: bool = True
    model_dir: str = "data/models/sensevoice"


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
    voice: VoiceConfig = field(default_factory=VoiceConfig)
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

        voice_data = data.get("voice", {})
        voice = VoiceConfig(
            enabled=bool(voice_data.get("enabled", True)),
            model_dir=str(voice_data.get("model_dir", "data/models/sensevoice")),
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
            voice=voice,
            admin=admin,
        )
