"""轻量 JSONL 对话追踪器 (tracing.py)。

提供离线、纯文本、按天分文件的轮次追踪记录与回放。
每轮 trace_id 唯一，支持递归脱敏敏感键与凭据，禁止记录密钥明文。
使用 contextvars.ContextVar 管理当前协程/任务上下文中的 trace_id，确保并发安全。
严格遵循第一阶段离线内核契约，不引入第三方依赖，不硬编码任何具体角色与世界观名称。
"""
from __future__ import annotations

import contextvars
import dataclasses
import json
import os
import re
import uuid
from collections.abc import Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Union

# 当前协程 / 任务上下文绑定的 trace_id
_CURRENT_TRACE_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_trace_id", default=None
)

# 默认敏感字段关键词（不区分大小写）
DEFAULT_SENSITIVE_KEYWORDS: Set[str] = {
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "private_key",
    "client_secret",
    "secret_key",
    "access_key",
    "auth_token",
    "access_token",
    "refresh_token",
    "bot_token",
}

# 属于模型统计与监控用途、包含 token 但不属于凭据敏感信息的常见安全键名
NON_SENSITIVE_TOKEN_METRICS: Set[str] = {
    "token_count",
    "tokens_count",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "token_usage",
    "tokens_used",
    "cache_hit_tokens",
    "cache_miss_tokens",
}

# 匹配常见显式密钥值模式（如 sk-xxx）
_SK_KEY_PATTERN = re.compile(r"^sk-[A-Za-z0-9_-]{6,}$")


def generate_trace_id(prefix: str = "tr") -> str:
    """生成全局唯一的 trace_id（时间戳前缀 + 随机散列，保证可排序且唯一）。"""
    now_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    rand_hex = uuid.uuid4().hex[:8]
    return f"{prefix}_{now_str}_{rand_hex}"


def get_current_trace_id() -> Optional[str]:
    """获取当前协程上下文绑定的 trace_id。"""
    return _CURRENT_TRACE_ID.get()


def set_current_trace_id(trace_id: Optional[str]) -> contextvars.Token[Optional[str]]:
    """设置当前协程上下文绑定的 trace_id 并返回 Token 供恢复。"""
    return _CURRENT_TRACE_ID.set(trace_id)


def is_sensitive_key(
    key: Any,
    custom_sensitive_keys: Optional[Iterable[str]] = None,
) -> bool:
    """判定字段名是否为敏感键或凭证密钥键。"""
    if not isinstance(key, str):
        key = str(key)
    normalized = key.strip().lower().replace("-", "_")

    # 1. 自定义敏感键检查
    if custom_sensitive_keys:
        for custom in custom_sensitive_keys:
            c_norm = custom.strip().lower().replace("-", "_")
            if c_norm and (c_norm == normalized or c_norm in normalized):
                return True

    # 2. 统计监控指标中的 token 词根白名单放行（优先于 token 关键词拦截）
    if any(metric in normalized for metric in NON_SENSITIVE_TOKEN_METRICS):
        return False

    # 3. 核心敏感词根检查（api_key / token / secret / password / authorization 等）
    if any(keyword in normalized for keyword in DEFAULT_SENSITIVE_KEYWORDS):
        return True

    # 4. 常见的凭据及配置键名特征
    if normalized in {"auth", "credential", "credentials", "config_key"}:
        return True
    if normalized.endswith("_key") and any(
        k in normalized for k in ("auth", "access", "private", "secret", "api", "admin", "sign")
    ):
        return True

    return False


def _is_structured_value(value: Any) -> bool:
    return (
        isinstance(value, (Mapping, list, tuple, set))
        or (dataclasses.is_dataclass(value) and not isinstance(value, type))
        or hasattr(value, "__dict__")
    )


def sanitize_payload(
    payload: Any,
    sensitive_keys: Optional[Iterable[str]] = None,
    redacted_placeholder: str = "[REDACTED]",
    seen: Optional[Set[int]] = None,
) -> Any:
    """递归脱敏载荷中的敏感字段与凭据。

    支持嵌套字典、列表、元组、集合、dataclass 实例及带 __dict__ 的对象。
    对于敏感键对应的非结构化标量直接置为占位符；对于敏感容器（如 auth 字典）深入递归脱敏；
    对于显式密钥值（如 Bearer 头部、sk- 开头凭证）无论字段名一律脱敏；
    对 token 统计监控指标予以保留；
    对于循环引用进行阻断；异常不静默吞掉。
    """
    if seen is None:
        seen = set()

    # 基础类型直接返回或格式化
    if payload is None or isinstance(payload, (int, float, bool)):
        return payload

    if isinstance(payload, str):
        # 字符串形式的 Bearer 头部脱敏
        if payload.startswith("Bearer "):
            return f"Bearer {redacted_placeholder}"
        # 常见显式 sk- 形式密钥字符串脱敏
        if _SK_KEY_PATTERN.match(payload) or (payload.startswith("sk-") and len(payload) > 8):
            return redacted_placeholder
        return payload

    if isinstance(payload, (datetime, date)):
        return payload.isoformat()

    if isinstance(payload, bytes):
        return f"<bytes: len={len(payload)}>"

    # 复合对象的循环引用检查
    obj_id = id(payload)
    if obj_id in seen:
        return "[CIRCULAR]"

    # dataclass 实例处理
    if dataclasses.is_dataclass(payload) and not isinstance(payload, type):
        seen.add(obj_id)
        try:
            result = {}
            for field_def in dataclasses.fields(payload):
                field_name = field_def.name
                val = getattr(payload, field_name)
                if is_sensitive_key(field_name, sensitive_keys) and not _is_structured_value(val):
                    result[field_name] = redacted_placeholder
                else:
                    result[field_name] = sanitize_payload(
                        val,
                        sensitive_keys=sensitive_keys,
                        redacted_placeholder=redacted_placeholder,
                        seen=seen,
                    )
            return result
        finally:
            seen.remove(obj_id)

    # 字典/映射类型处理
    if isinstance(payload, Mapping):
        seen.add(obj_id)
        try:
            result = {}
            for k, v in payload.items():
                key_str = str(k)
                if is_sensitive_key(key_str, sensitive_keys) and not _is_structured_value(v):
                    result[key_str] = redacted_placeholder
                else:
                    result[key_str] = sanitize_payload(
                        v,
                        sensitive_keys=sensitive_keys,
                        redacted_placeholder=redacted_placeholder,
                        seen=seen,
                    )
            return result
        finally:
            seen.remove(obj_id)

    # 列表、元组、集合处理
    if isinstance(payload, (list, tuple, set)):
        seen.add(obj_id)
        try:
            return [
                sanitize_payload(
                    item,
                    sensitive_keys=sensitive_keys,
                    redacted_placeholder=redacted_placeholder,
                    seen=seen,
                )
                for item in payload
            ]
        finally:
            seen.remove(obj_id)

    # 普通自定义对象（拥有 __dict__）
    if hasattr(payload, "__dict__"):
        seen.add(obj_id)
        try:
            result = {}
            for k, v in payload.__dict__.items():
                if k.startswith("_"):
                    continue
                if is_sensitive_key(k, sensitive_keys) and not _is_structured_value(v):
                    result[k] = redacted_placeholder
                else:
                    result[k] = sanitize_payload(
                        v,
                        sensitive_keys=sensitive_keys,
                        redacted_placeholder=redacted_placeholder,
                        seen=seen,
                    )
            return result
        finally:
            seen.remove(obj_id)

    # 默认兜底字符串转换
    return str(payload)


class JsonlTraceRecorder:
    """轻量 JSONL 轮次追踪记录器。

    特性：
    - 每轮 trace_id 唯一；
    - 使用 contextvars.ContextVar 管理当前协程上下文 trace_id，确保多协程并发安全不串号；
    - 按天分文件存储（YYYY-MM-DD.jsonl）；
    - 追加写入（append 模式），UTF-8 编码，ensure_ascii=False；
    - 记录 step、时间戳（ISO 8601）、脱敏后的 payload；
    - 异常不静默吞掉；
    - 提供 record(step, payload)、events(trace_id) 与 read_trace(trace_id) 接口供回放与验证。
    """

    def __init__(
        self,
        directory: Union[str, Path] = "data/traces",
        trace_id: Optional[str] = None,
        sensitive_keys: Optional[Iterable[str]] = None,
        redacted_placeholder: str = "[REDACTED]",
    ) -> None:
        self.directory = str(directory)
        self._default_trace_id: str = trace_id or generate_trace_id()
        if trace_id:
            _CURRENT_TRACE_ID.set(trace_id)
        self.sensitive_keys = set(sensitive_keys) if sensitive_keys else None
        self.redacted_placeholder = redacted_placeholder

    @property
    def trace_id(self) -> str:
        """获取当前协程上下文的 trace_id，若未设置则回退到 recorder 的缺省 trace_id。"""
        ctx_tid = _CURRENT_TRACE_ID.get()
        if ctx_tid is not None:
            return ctx_tid
        return self._default_trace_id

    @trace_id.setter
    def trace_id(self, val: str) -> None:
        """设置当前上下文与 recorder 的 trace_id。"""
        self._default_trace_id = val
        _CURRENT_TRACE_ID.set(val)

    def new_trace(self, trace_id: Optional[str] = None) -> str:
        """开启新轮次追踪，生成并在当前协程上下文中绑定新的 trace_id。"""
        new_id = trace_id or generate_trace_id()
        _CURRENT_TRACE_ID.set(new_id)
        self._default_trace_id = new_id
        return new_id

    def record(
        self,
        step: str,
        payload: Any = None,
        *,
        trace_id: Optional[str] = None,
        timestamp: Optional[datetime] = None,
        **extra_payload: Any,
    ) -> Dict[str, Any]:
        """记录一个追踪步骤事件并持久化写入当天的 JSONL 文件。

        参数：
        - step: 当前执行步骤标识（如 turn_input, snapshot, draft, plan, kernel_result, validation 等）
        - payload: 步骤数据载荷（dict, dataclass, primitive 等，自动递归脱敏）
        - trace_id: 可选覆盖当前 recorder 的 trace_id
        - timestamp: 可选事件时间戳（默认为当前本地时间带时区）
        - extra_payload: 键值对扩展数据，与 payload 智能合并

        返回：
        - 写入存储的事件数据字典
        """
        current_trace_id = trace_id or self.trace_id
        if not current_trace_id:
            current_trace_id = self.new_trace()

        now = timestamp or datetime.now().astimezone()
        ts_str = now.isoformat()

        # 处理 payload 与 extra_payload
        if payload is None:
            raw_payload = extra_payload
        elif isinstance(payload, dict) and extra_payload:
            raw_payload = {**payload, **extra_payload}
        elif extra_payload:
            raw_payload = {"payload": payload, **extra_payload}
        else:
            raw_payload = payload

        # 递归脱敏处理
        sanitized_payload = sanitize_payload(
            raw_payload,
            sensitive_keys=self.sensitive_keys,
            redacted_placeholder=self.redacted_placeholder,
        )

        event: Dict[str, Any] = {
            "trace_id": current_trace_id,
            "step": str(step),
            "timestamp": ts_str,
            "time": ts_str,
            "payload": sanitized_payload,
        }

        # 按天分文件存储 (YYYY-MM-DD.jsonl)
        date_str = now.strftime("%Y-%m-%d")
        filename = f"{date_str}.jsonl"
        os.makedirs(self.directory, exist_ok=True)
        filepath = os.path.join(self.directory, filename)

        # 追加写入，异常不静默吞掉
        line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
        with open(filepath, mode="a", encoding="utf-8") as f:
            f.write(line)
            f.flush()

        return event

    def read_trace(self, trace_id: str) -> List[Dict[str, Any]]:
        """从存储目录读取指定 trace_id 的所有按时间序排列的追踪事件。"""
        if not os.path.exists(self.directory):
            return []

        matched_events: List[Dict[str, Any]] = []
        entries = sorted(os.listdir(self.directory))
        for entry in entries:
            if not entry.endswith(".jsonl"):
                continue
            filepath = os.path.join(self.directory, entry)
            if not os.path.isfile(filepath):
                continue
            with open(filepath, mode="r", encoding="utf-8") as f:
                for line in f:
                    line_str = line.strip()
                    if not line_str:
                        continue
                    record = json.loads(line_str)
                    if record.get("trace_id") == trace_id:
                        matched_events.append(record)
        return matched_events

    def events(self, trace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取指定或当前 trace_id 的全部事件列表。"""
        target_trace_id = trace_id or self.trace_id
        if not target_trace_id:
            return []
        return self.read_trace(target_trace_id)

    def __enter__(self) -> "JsonlTraceRecorder":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass


def read_trace(
    trace_id: str,
    directory: Union[str, Path] = "data/traces",
) -> List[Dict[str, Any]]:
    """模块级便捷函数：根据 trace_id 回放检索事件列表。"""
    recorder = JsonlTraceRecorder(directory=directory, trace_id=trace_id)
    return recorder.read_trace(trace_id)


__all__ = [
    "DEFAULT_SENSITIVE_KEYWORDS",
    "JsonlTraceRecorder",
    "generate_trace_id",
    "get_current_trace_id",
    "is_sensitive_key",
    "read_trace",
    "sanitize_payload",
    "set_current_trace_id",
]
