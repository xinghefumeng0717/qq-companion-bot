"""LLM 网关 (gateway.py)
收敛所有对 DeepSeek / OpenAI 兼容接口的文本、视觉、流式调用与计费落库。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional
import aiohttp

from companion.config import LLMConfig
from companion.db import Database, now_str

logger = logging.getLogger(__name__)


def parse_llm_json(raw_text: str) -> Any:
    """容错解析 LLM 返回的 JSON 字符串。
    优先直接 json.loads，若包含 Markdown 代码块标记 (```json ... ```) 则剥离后再解析。
    """
    try:
        return json.loads(raw_text)
    except Exception:
        clean = re.sub(r"^```json\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
        return json.loads(clean)


def apply_thinking(payload: Dict[str, Any], enabled: bool, effort: str, base_url: str = "") -> Dict[str, Any]:
    """按 DeepSeek V4 规范注入思考模式参数。非 DeepSeek 或未开启思考时安全处理。"""
    if base_url and "deepseek" not in base_url.lower():
        payload.pop("thinking", None)
        payload.pop("reasoning_effort", None)
        return payload
    if enabled:
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = effort
        payload.pop("temperature", None)
    else:
        payload["thinking"] = {"type": "disabled"}
    return payload


def apply_provider_params(
    payload: Dict[str, Any],
    provider: str,
    enabled: bool,
    effort: str,
    base_url: str = "",
) -> Dict[str, Any]:
    """按 provider 分流注入推理与思考参数：
    - deepseek: 注入 thinking 参数 (apply_thinking)
    - minimax: 不传 thinking，加 "reasoning_split": True
    - openai (generic): 不附带任何推理参数
    """
    if provider == "deepseek":
        return apply_thinking(payload, enabled, effort, base_url=base_url)
    elif provider == "minimax":
        payload.pop("thinking", None)
        payload.pop("reasoning_effort", None)
        payload["reasoning_split"] = True
        return payload
    else:  # openai / generic
        payload.pop("thinking", None)
        payload.pop("reasoning_effort", None)
        payload.pop("reasoning_split", None)
        return payload


class LLMGateway:
    def __init__(self, config: LLMConfig, db: Optional[Database] = None):
        self.config = config
        self.db = db
        self._session: Optional[aiohttp.ClientSession] = None

    async def get_session(self) -> aiohttp.ClientSession:
        current_auth = f"Bearer {self.config.api_key}"
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": current_auth,
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=90),
            )
        elif self._session.headers.get("Authorization") != current_auth:
            await self._session.close()
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": current_auth,
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=90),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _estimate_cost(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
        at: Optional[datetime] = None,
        model: str = "",
    ) -> float:
        """按 DeepSeek 峰谷+缓存命中定价估算调用费用（元），价目按模型分别取。
        缓存字段缺失时全部按未命中计。"""
        if cache_hit_tokens == 0 and cache_miss_tokens == 0:
            cache_miss_tokens = prompt_tokens
        rate = self.config.pricing.rate_for(model or self.config.text_model)
        if self.config.pricing.is_peak(at):
            hit_price = rate.cache_hit_peak
            miss_price = rate.cache_miss_peak
            out_price = rate.output_peak
        else:
            hit_price = rate.cache_hit_offpeak
            miss_price = rate.cache_miss_offpeak
            out_price = rate.output_offpeak
        cost = (
            cache_hit_tokens * hit_price
            + cache_miss_tokens * miss_price
            + completion_tokens * out_price
        ) / 1_000_000.0
        return round(cost, 6)

    async def _log_call(
        self,
        purpose: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
    ) -> None:
        """将调用统计写入 llm_calls 表"""
        if not self.db:
            return
        try:
            cost = self._estimate_cost(
                prompt_tokens, completion_tokens, cache_hit_tokens, cache_miss_tokens, model=model
            )
            await self.db.execute(
                """
                INSERT INTO llm_calls (purpose, model, prompt_tokens, completion_tokens,
                                       cache_hit_tokens, cache_miss_tokens, cost_estimate, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (purpose, model, prompt_tokens, completion_tokens,
                 cache_hit_tokens, cache_miss_tokens, cost, now_str()),
            )
        except Exception as e:
            logger.error(f"[Gateway] 记录 llm_calls 失败: {e}")

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        json_mode: bool = False,
        purpose: str = "chat",
        max_retries: int = 2,
    ) -> str:
        """非流式调用，返回回复文本"""
        active = self.config.active()
        target_model = model or active.tasks or active.chat
        payload: Dict[str, Any] = {
            "model": target_model,
            "messages": messages,
            "temperature": temperature,
        }
        apply_provider_params(
            payload,
            provider=active.provider,
            enabled=self.config.thinking_for_purpose(purpose),
            effort=self.config.thinking_effort_tasks,
            base_url=active.base_url,
        )
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        url = f"{active.base_url.rstrip('/')}/chat/completions"
        session = await self.get_session()

        for attempt in range(max_retries + 1):
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        err_text = await resp.text()
                        raise RuntimeError(f"HTTP {resp.status}: {err_text}")

                    data = await resp.json()
                    content = data["choices"][0]["message"]["content"]
                    usage = data.get("usage", {})
                    p_tokens = usage.get("prompt_tokens", 0)
                    c_tokens = usage.get("completion_tokens", 0)
                    hit_tokens = usage.get("prompt_cache_hit_tokens", 0)
                    miss_tokens = usage.get("prompt_cache_miss_tokens", 0)
                    await self._log_call(purpose, target_model, p_tokens, c_tokens, hit_tokens, miss_tokens)
                    return content
            except Exception as e:
                if attempt < max_retries:
                    wait_sec = 2 ** attempt
                    logger.warning(f"[Gateway] 调用失败 (attempt {attempt+1}/{max_retries+1}): {e}，将在 {wait_sec}s 后重试")
                    await asyncio.sleep(wait_sec)
                else:
                    logger.error(f"[Gateway] 调用重试耗尽: {e}")
                    raise

        return ""

    async def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        purpose: str = "main_chat",
        max_retries: int = 2,
    ) -> AsyncIterator[str]:
        """流式调用，产出文本增量"""
        active = self.config.active()
        target_model = model or active.chat
        payload = {
            "model": target_model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        apply_provider_params(
            payload,
            provider=active.provider,
            enabled=self.config.thinking_chat,
            effort=self.config.thinking_effort_chat,
            base_url=active.base_url,
        )

        url = f"{active.base_url.rstrip('/')}/chat/completions"
        session = await self.get_session()

        for attempt in range(max_retries + 1):
            try:
                attempt_chunks = []
                p_tokens = 0
                c_tokens = 0
                hit_tokens = 0
                miss_tokens = 0

                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        err_text = await resp.text()
                        raise RuntimeError(f"HTTP {resp.status}: {err_text}")

                    async for line in resp.content:
                        line_str = line.decode("utf-8").strip()
                        if not line_str.startswith("data:"):
                            continue
                        data_part = line_str[5:].strip()
                        if data_part == "[DONE]":
                            break

                        try:
                            chunk = json.loads(data_part)
                        except json.JSONDecodeError:
                            continue

                        # 捕获 usage 信息
                        if "usage" in chunk and chunk["usage"]:
                            p_tokens = chunk["usage"].get("prompt_tokens", p_tokens)
                            c_tokens = chunk["usage"].get("completion_tokens", c_tokens)
                            hit_tokens = chunk["usage"].get("prompt_cache_hit_tokens", hit_tokens)
                            miss_tokens = chunk["usage"].get("prompt_cache_miss_tokens", miss_tokens)

                        choices = chunk.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            content_piece = delta.get("content", "")
                            if content_piece:
                                attempt_chunks.append(content_piece)

                # 本次尝试完整成功，此时才将文本块输出给上层
                for piece in attempt_chunks:
                    yield piece

                # 如果流式接口未返回 usage，则按字数粗略估算一个保底 token 数（全部按缓存未命中计）
                if p_tokens == 0 and c_tokens == 0:
                    c_tokens = max(1, len("".join(attempt_chunks)))
                    # 提示词字数保底估算
                    p_tokens = sum(len(str(m.get("content", ""))) for m in messages) // 2
                    hit_tokens = 0
                    miss_tokens = p_tokens

                await self._log_call(purpose, target_model, p_tokens, c_tokens, hit_tokens, miss_tokens)
                return
            except Exception as e:
                if attempt < max_retries:
                    wait_sec = 2 ** attempt
                    logger.warning(f"[Gateway] 流式调用失败 (attempt {attempt+1}/{max_retries+1}): {e}，{wait_sec}s 后重试")
                    await asyncio.sleep(wait_sec)
                else:
                    logger.error(f"[Gateway] 流式重试耗尽: {e}")
                    raise
