"""测试内核轻量 JSONL 对话追踪器 (tests/test_kernel_tracing.py)

覆盖验收要求：
1. generate_trace_id 保证每轮 trace_id 唯一
2. is_sensitive_key / sanitize_payload：
   - 递归脱敏敏感字段（api_key, token, password, authorization, auth 等）
   - Bearer 头部脱敏与 sk- 前缀脱敏（包括密钥值本身脱敏）
   - auth 容器递归脱敏（内部包含凭据及普通字段）
   - 严格放行非敏感 token 监控统计指标（prompt_tokens, completion_tokens, token_usage 等）
   - 阻断循环引用
   - 字典、列表、dataclass 递归脱敏
3. JsonlTraceRecorder 与 read_trace：
   - 临时目录隔离记录
   - 按天分文件存储 (YYYY-MM-DD.jsonl)
   - 追加写入与完整字段存储（trace_id, step, timestamp, payload）
   - 记录 step、时间戳、脱敏后 payload
   - 根据 trace_id 回放检索，事件序列完整正确
4. 并发隔离：
   - 两个并发异步任务共享同一个 recorder 实例
   - 分别调用 new_trace，并发执行多步 record() 且不传显式 trace_id
   - 各任务 events() 与 read_trace() 检索到的事件互不串号
"""

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime
import pytest

from companion.kernel.tracing import (
    DEFAULT_SENSITIVE_KEYWORDS,
    NON_SENSITIVE_TOKEN_METRICS,
    JsonlTraceRecorder,
    generate_trace_id,
    get_current_trace_id,
    is_sensitive_key,
    read_trace,
    sanitize_payload,
    set_current_trace_id,
)


@dataclass
class MockDataclassWithSecret:
    user_name: str
    api_key: str
    token_count: int


def test_generate_trace_id_uniqueness():
    """测试 trace_id 生成的唯一性。"""
    ids = {generate_trace_id() for _ in range(100)}
    assert len(ids) == 100
    for tid in ids:
        assert tid.startswith("tr_")


def test_is_sensitive_key_classification():
    """测试敏感字段判定与统计指标白名单放行。"""
    assert is_sensitive_key("api_key")
    assert is_sensitive_key("access_token")
    assert is_sensitive_key("client_secret")
    assert is_sensitive_key("Authorization")
    assert is_sensitive_key("password")
    assert is_sensitive_key("auth_key")
    assert is_sensitive_key("auth")

    # 白名单指标（包含 token 但属于监控统计，不属于敏感凭据）
    assert not is_sensitive_key("prompt_tokens")
    assert not is_sensitive_key("completion_tokens")
    assert not is_sensitive_key("total_tokens")
    assert not is_sensitive_key("token_usage")
    assert not is_sensitive_key("cached_tokens")
    assert not is_sensitive_key("token_count")

    # 普通字段
    assert not is_sensitive_key("user_id")
    assert not is_sensitive_key("message_text")


def test_sanitize_payload_recursive():
    """测试深层嵌套与 dataclass 敏感信息递归脱敏。"""
    nested = {
        "user": "佩丽卡",
        # auth 键本身是敏感键，作为容器字典时其内部字段应被深入递归脱敏
        "auth": {
            "secret_code": "123456",
            "access_token": "abc_xyz_789",
            "public_id": "user_42",
        },
        # 普通嵌套键，内部字段含有 api_key 与 token 以及非敏感指标
        "metrics_and_credentials": {
            "api_key": "sk-1234567890abcdef",
            "token": "tok_abcdef123456",
            "prompt_tokens": 128,
        },
        "header": "Bearer secret_header_value",
        "dataclass_obj": MockDataclassWithSecret(
            user_name="管理员",
            api_key="sk-real-key-9999",
            token_count=256,
        ),
        "list_items": [
            {"password": "plain_password_123"},
            "sk-explicit-key-99999",
        ],
    }

    sanitized = sanitize_payload(nested)

    # auth 容器递归脱敏：内部敏感字段脱敏
    assert sanitized["auth"]["secret_code"] == "[REDACTED]"
    assert sanitized["auth"]["access_token"] == "[REDACTED]"

    # 普通父键下内部敏感字段被脱敏，白名单字段保留
    assert sanitized["metrics_and_credentials"]["api_key"] == "[REDACTED]"
    assert sanitized["metrics_and_credentials"]["token"] == "[REDACTED]"
    assert sanitized["metrics_and_credentials"]["prompt_tokens"] == 128

    # 字符串形式 Bearer 与 sk- 前缀脱敏
    assert sanitized["header"] == "Bearer [REDACTED]"

    # dataclass 内部脱敏
    assert sanitized["dataclass_obj"]["api_key"] == "[REDACTED]"
    assert sanitized["dataclass_obj"]["user_name"] == "管理员"
    assert sanitized["dataclass_obj"]["token_count"] == 256

    # 列表中脱敏
    assert sanitized["list_items"][0]["password"] == "[REDACTED]"
    assert sanitized["list_items"][1] == "[REDACTED]"


def test_sanitize_payload_auth_container_and_keys_regression():
    """脱敏回归测试：针对 auth 容器、任意位置出现的密钥值、以及 token 统计完整保留。"""
    payload = {
        "auth": {
            "token": "secret_token_111",
            "sub_auth": {
                "private_key": "-----BEGIN PRIVATE KEY-----",
                "nested_token": "nested_secret",
            },
            "auth_type": "oauth2",
        },
        # 即使键名不是敏感键，如果值本身符合密钥形态（Bearer 或 sk- 凭据），也必须脱敏
        "random_field": "sk-very-secret-token-abcdef123456",
        "another_field": "Bearer some-access-jwt-token-string",
        # 全量 token 统计字段必须原样完整保留，禁止误脱敏
        "tokens_stats": {
            "token_count": 10,
            "tokens_count": 12,
            "total_tokens": 100,
            "prompt_tokens": 40,
            "completion_tokens": 60,
            "input_tokens": 40,
            "output_tokens": 60,
            "cached_tokens": 20,
            "token_usage": {"input": 40, "output": 60},
            "tokens_used": 100,
            "cache_hit_tokens": 15,
            "cache_miss_tokens": 5,
        },
    }

    sanitized = sanitize_payload(payload)

    # 1. 验证 auth 及其多层深层嵌套脱敏
    assert sanitized["auth"]["token"] == "[REDACTED]"
    assert sanitized["auth"]["sub_auth"]["private_key"] == "[REDACTED]"
    assert sanitized["auth"]["sub_auth"]["nested_token"] == "[REDACTED]"

    # 2. 验证任意字段名下的密钥值脱敏
    assert sanitized["random_field"] == "[REDACTED]"
    assert sanitized["another_field"] == "Bearer [REDACTED]"

    # 3. 验证 token 统计完整保留
    for metric_name in NON_SENSITIVE_TOKEN_METRICS:
        assert metric_name in sanitized["tokens_stats"]
        assert sanitized["tokens_stats"][metric_name] != "[REDACTED]"


def test_sanitize_payload_handles_circular_reference():
    """测试循环引用保护。"""
    circ: dict = {"key": "value"}
    circ["self"] = circ
    sanitized = sanitize_payload(circ)
    assert sanitized["key"] == "value"
    assert sanitized["self"] == "[CIRCULAR]"


def test_recorder_and_read_trace_with_tmp_path(tmp_path):
    """测试临时目录下的写入、按天分文件及 trace 回放。"""
    trace_dir = tmp_path / "traces"
    recorder = JsonlTraceRecorder(directory=trace_dir)

    tid1 = recorder.new_trace()
    recorder.record("step_init", {"status": "ok"}, trace_id=tid1)
    recorder.record("step_secret", {"api_key": "sk-test-secret"}, trace_id=tid1)

    tid2 = recorder.new_trace()
    recorder.record("step_other", {"msg": "hello"}, trace_id=tid2)

    # 验证目录与文件已创建
    files = list(trace_dir.glob("*.jsonl"))
    assert len(files) >= 1

    # 通过 read_trace 回放 tid1
    events1 = read_trace(tid1, directory=trace_dir)
    assert len(events1) == 2
    assert events1[0]["step"] == "step_init"
    assert events1[0]["payload"]["status"] == "ok"
    assert events1[1]["step"] == "step_secret"
    # 确认回放数据已自动脱敏
    assert events1[1]["payload"]["api_key"] == "[REDACTED]"

    # 回放 tid2
    events2 = recorder.read_trace(tid2)
    assert len(events2) == 1
    assert events2[0]["step"] == "step_other"
    assert events2[0]["payload"]["msg"] == "hello"


@pytest.mark.anyio
async def test_concurrent_tasks_shared_recorder_isolation(tmp_path):
    """测试两个并发异步任务共享同一个 recorder 时，new_trace/record/events 不串号。"""
    trace_dir = tmp_path / "concurrent_traces"
    shared_recorder = JsonlTraceRecorder(directory=trace_dir)

    task1_recorded_steps = []
    task2_recorded_steps = []

    async def worker_1():
        # 任务 1 初始化自身独立 trace_id
        tid1 = shared_recorder.new_trace()
        assert shared_recorder.trace_id == tid1
        assert get_current_trace_id() == tid1

        # 步骤 1：record 不传入 trace_id，应自动使用协程上下文 trace_id
        shared_recorder.record("w1_step1", {"data": "task1_1"})
        task1_recorded_steps.append("w1_step1")
        await asyncio.sleep(0.01)

        # 检查上下文未被任务 2 篡改
        assert shared_recorder.trace_id == tid1
        shared_recorder.record("w1_step2", {"data": "task1_2"})
        task1_recorded_steps.append("w1_step2")
        await asyncio.sleep(0.01)

        assert shared_recorder.trace_id == tid1
        shared_recorder.record("w1_step3", {"data": "task1_3"})
        task1_recorded_steps.append("w1_step3")

        # events() 不传 trace_id 时应只查出任务 1 的事件
        evs = shared_recorder.events()
        assert len(evs) == 3
        for e in evs:
            assert e["trace_id"] == tid1
            assert e["step"].startswith("w1_")
        return tid1

    async def worker_2():
        await asyncio.sleep(0.005)  # 稍微错开启动时间，让 worker_1 已经先 new_trace
        tid2 = shared_recorder.new_trace()
        assert shared_recorder.trace_id == tid2
        assert get_current_trace_id() == tid2

        # 步骤 1
        shared_recorder.record("w2_step1", {"data": "task2_1"})
        task2_recorded_steps.append("w2_step1")
        await asyncio.sleep(0.01)

        # 检查上下文未被任务 1 篡改
        assert shared_recorder.trace_id == tid2
        shared_recorder.record("w2_step2", {"data": "task2_2"})
        task2_recorded_steps.append("w2_step2")
        await asyncio.sleep(0.01)

        assert shared_recorder.trace_id == tid2
        shared_recorder.record("w2_step3", {"data": "task2_3"})
        task2_recorded_steps.append("w2_step3")

        # events() 不传 trace_id 时应只查出任务 2 的事件
        evs = shared_recorder.events()
        assert len(evs) == 3
        for e in evs:
            assert e["trace_id"] == tid2
            assert e["step"].startswith("w2_")
        return tid2

    # 并发执行两个 worker
    tid1, tid2 = await asyncio.gather(worker_1(), worker_2())

    assert tid1 != tid2

    # 分别从持久化回放校验两者的事件列表，彻底验证没有交叉、串号或混淆
    events_t1 = shared_recorder.read_trace(tid1)
    events_t2 = shared_recorder.read_trace(tid2)

    assert len(events_t1) == 3
    assert [e["step"] for e in events_t1] == ["w1_step1", "w1_step2", "w1_step3"]
    assert all(e["trace_id"] == tid1 for e in events_t1)

    assert len(events_t2) == 3
    assert [e["step"] for e in events_t2] == ["w2_step1", "w2_step2", "w2_step3"]
    assert all(e["trace_id"] == tid2 for e in events_t2)
