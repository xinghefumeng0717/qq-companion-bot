"""第四阶段只读记忆视图的离线测试 (tests/test_kernel_memory_view.py)

覆盖验收要求：
1) FakeMemory 的各只读接口分别返回 facts / impressions / self / diaries /
   followups / desires，FakeArcs 返回生活剧本；collect_memory_blocks 组装出的
   每个块标题、括号说明与条目顺序正确，契约字段顺序正确（trace 不涉及）。
2) self_memory / impressions 两个开关默认关闭：不取数也不出块。
3) 单块采集异常时只省略该块，其余块照常返回。
4) 用临时 Database 建表并插入过期 followup 与过期 desire，调用只读接口后
   逐字核对行仍在、done 不变，证明只读接口没有 UPDATE / DELETE。
5) desire 之后又有用户消息时不再返回，但数据库行保留。
6) FakeGateway 存在时断言从未被调用（全程离线，不碰 API）。

所有数据都是测试自造的虚构内容，不读取也不修改生产数据、角色卡与 config。
"""

from dataclasses import fields
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pytest

from companion.db import TIME_FORMAT, Database
from companion.kernel.contracts import MemoryPromptBlocks
from companion.kernel.memory_view import collect_memory_blocks
from companion.memory import MemoryManager
from companion.prompts import CRISIS_PROMPT


# ==========================================
# 假实现：全部只读、全部虚构、全部离线
# ==========================================

class FakeMemory:
    """只读记忆假实现；记录被调用过的方法名，用于断言开关确实生效。"""

    def __init__(self, *, raise_on: Optional[str] = None):
        self.calls: List[str] = []
        self.raise_on = raise_on

    def _maybe_raise(self, name: str) -> None:
        if self.raise_on == name:
            raise RuntimeError(f"假实现故障：{name}")

    async def get_facts_for_prompt(self, query: str = "") -> List[str]:
        self.calls.append("get_facts_for_prompt")
        self._maybe_raise("get_facts_for_prompt")
        return ["他说他周五要去面试（10-08 记下）"]

    async def get_impressions(self) -> List[Dict[str, Any]]:
        self.calls.append("get_impressions")
        self._maybe_raise("get_impressions")
        return [{"content": "他说话很实在", "confidence": 2, "evidence": "虚构依据"}]

    async def get_self_facts_for_prompt(self, query: str = "") -> List[str]:
        self.calls.append("get_self_facts_for_prompt")
        self._maybe_raise("get_self_facts_for_prompt")
        return ["我住在城西（10-08 说的）"]

    async def get_active_diaries(self, current_valence: float = 2.0, now=None) -> List[str]:
        self.calls.append("get_active_diaries")
        self._maybe_raise("get_active_diaries")
        return ["清晰地记得: 我们在食堂吃的火锅"]

    async def get_open_followups_readonly(self, due_only: bool = False, limit: int = 5, now=None) -> List[Dict[str, Any]]:
        self.calls.append("get_open_followups_readonly")
        self._maybe_raise("get_open_followups_readonly")
        return [
            {
                "id": 1,
                "topic": "他说的周五面试",
                "remind_after": "2026-10-09 09:00",
                "created_at": "2026-10-08 09:00",
            }
        ]

    async def get_live_desires_readonly(self, limit: int = 5, now=None) -> List[Dict[str, Any]]:
        self.calls.append("get_live_desires_readonly")
        self._maybe_raise("get_live_desires_readonly")
        return [{"id": 1, "content": "我想问他是不是累了", "created_at": "2026-10-08 20:00"}]

    @staticmethod
    def format_followup(row: Dict[str, Any]) -> str:
        """与生产同一套格式：带上“哪天记下的”。"""
        return MemoryManager.format_followup(row)


class FakeArcs:
    """生活剧本假实现：只回一段剧本区块。"""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def build_prompt_block(self, as_script: bool = False, now=None) -> str:
        self.calls.append({"as_script": as_script, "now": now})
        header = (
            "\n【生活剧本】（这是系统允许创作的生活剧本，可自然带上进展；"
            "它不用于证明他做过什么或语音等真实动作是否发生）\n"
        )
        return header + "- （临近）乐团节目审查：低音部那段还没合齐（明天）"


class FakeGateway:
    """任何模型调用都算失败：只读记忆视图不该碰模型。"""

    def __init__(self) -> None:
        self.calls: List[Any] = []

    async def chat(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append(("chat", args, kwargs))
        raise AssertionError("只读记忆视图不应调用模型")

    async def stream_chat(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("stream_chat", args, kwargs))
        raise AssertionError("只读记忆视图不应调用模型")


# ==========================================
# 1) 各块标题与顺序
# ==========================================

@pytest.mark.anyio
async def test_collect_memory_blocks_builds_every_block_with_expected_titles():
    """每个来源都出块，标题、括号说明与条目顺序都对。"""
    memory = FakeMemory()
    arcs = FakeArcs()
    now = datetime(2026, 10, 9, 20, 0)

    blocks = await collect_memory_blocks(
        memory,
        arcs=arcs,
        query="今天真累",
        valence=2.0,
        now=now,
        self_memory=True,
        impressions=True,
    )

    assert isinstance(blocks, MemoryPromptBlocks)

    assert blocks.facts == (
        "【关于他】（括号里是记下的日期，里面的「明天/今天」按那天算）\n"
        "  - 他说他周五要去面试（10-08 记下）"
    )
    assert blocks.impressions == (
        "【你对他的看法】（你自己的感觉，可能看错。用来决定怎么接他的话；不要说出口，也不要分析他）\n"
        "  - 他说话很实在（比较确定）"
    )
    assert blocks.self_facts == "【你的历史自述】\n  - 我住在城西（10-08 说的）"
    assert blocks.diaries == "【她的记忆｜历史聊天摘要】\n  - 清晰地记得: 我们在食堂吃的火锅"
    assert blocks.followups == "【待跟进】\n  - 他说的周五面试（10-08 记下）"
    assert blocks.desires == "【欲言又止｜历史念头】\n  - 我想问他是不是累了"

    # 生活剧本块来自 arcs，且是 as_script=True 的剧本形态
    assert "【生活剧本】" in blocks.arcs
    assert "乐团节目审查" in blocks.arcs
    assert arcs.calls and arcs.calls[0]["as_script"] is True
    assert arcs.calls[0]["now"] == now

    # 安全块：普通消息不触发
    assert blocks.safety == ""

    # 契约字段顺序固定（只约束这些记忆块的相对先后，契约日后新增字段不影响本断言）
    names = [f.name for f in fields(MemoryPromptBlocks)]
    required = [
        "facts",
        "impressions",
        "self_facts",
        "diaries",
        "followups",
        "desires",
        "arcs",
        "safety",
    ]
    assert all(name in names for name in required)
    positions = [names.index(name) for name in required]
    assert positions == sorted(positions)


@pytest.mark.anyio
async def test_model_sourced_memory_is_unverified_but_text_is_preserved():
    """模型自述和摘要保留原文，但不能包装成已核实的地点、计划或经历。"""
    memory = FakeMemory()
    memory.get_self_facts_for_prompt = lambda query="": _self_fact_text()
    memory.get_active_diaries = lambda current_valence=2.0, now=None: _diary_text()
    memory.get_live_desires_readonly = lambda limit=5, now=None: _desire_rows()

    blocks = await collect_memory_blocks(memory, self_memory=True)

    assert blocks.self_facts.startswith("【你的历史自述】")
    assert "再聊到要对得上" not in blocks.self_facts
    assert "她逛完街后打算顺路去一趟越山" in blocks.self_facts
    assert blocks.diaries.startswith("【她的记忆｜历史聊天摘要】")
    assert "清晰地记得: 我顺路去越山" in blocks.diaries
    assert blocks.desires.startswith("【欲言又止｜历史念头】")
    assert "想问他是不是还在忙，顺便提一句自己快到越山了" in blocks.desires


async def _self_fact_text():
    return ["她逛完街后打算顺路去一趟越山（10-24 说的）"]


async def _diary_text():
    return ["清晰地记得: 我顺路去越山"]


async def _desire_rows():
    return [{"id": 1, "content": "想问他是不是还在忙，顺便提一句自己快到越山了"}]


@pytest.mark.anyio
async def test_collect_memory_blocks_safety_block_appears_on_crisis_message():
    """命中危机关键词时安全块出现在最后，标题为【安全边界】。"""
    blocks = await collect_memory_blocks(
        FakeMemory(), query="我想死了", now=datetime(2026, 10, 9, 20, 0)
    )
    assert blocks.safety == f"【安全边界】\n{CRISIS_PROMPT}"


# ==========================================
# 2) self_memory / impressions 默认关闭
# ==========================================

@pytest.mark.anyio
async def test_self_memory_and_impressions_switches_default_off():
    """默认既不取数也不出块。"""
    memory = FakeMemory()

    blocks = await collect_memory_blocks(memory, now=datetime(2026, 10, 9, 20, 0))

    assert blocks.impressions == ""
    assert blocks.self_facts == ""
    assert "get_impressions" not in memory.calls
    assert "get_self_facts_for_prompt" not in memory.calls
    # 其余块照常出
    assert blocks.facts and blocks.diaries and blocks.followups and blocks.desires


@pytest.mark.anyio
async def test_self_memory_and_impressions_switches_can_be_enabled_independently():
    """两个开关各自独立生效。"""
    memory = FakeMemory()
    now = datetime(2026, 10, 9, 20, 0)

    blocks = await collect_memory_blocks(memory, now=now, impressions=True)
    assert blocks.impressions.startswith("【你对他的看法】")
    assert blocks.self_facts == ""
    assert "get_impressions" in memory.calls
    assert "get_self_facts_for_prompt" not in memory.calls

    memory2 = FakeMemory()
    blocks2 = await collect_memory_blocks(memory2, now=now, self_memory=True)
    assert blocks2.self_facts.startswith("【你的历史自述】")
    assert blocks2.impressions == ""
    assert "get_impressions" not in memory2.calls
    assert "get_self_facts_for_prompt" in memory2.calls


# ==========================================
# 3) 单块异常只省略该块
# ==========================================

@pytest.mark.anyio
async def test_single_block_failure_only_omits_that_block():
    """日记块采集炸了：日记块为空，其余块照常。"""
    memory = FakeMemory(raise_on="get_active_diaries")
    now = datetime(2026, 10, 9, 20, 0)

    blocks = await collect_memory_blocks(memory, now=now, self_memory=True, impressions=True)

    assert blocks.diaries == ""
    assert blocks.facts and blocks.impressions and blocks.self_facts
    assert blocks.followups and blocks.desires


@pytest.mark.anyio
async def test_impressions_failure_omits_only_impressions_block():
    """看法块炸了不牵连自我记忆块。"""
    memory = FakeMemory(raise_on="get_impressions")
    now = datetime(2026, 10, 9, 20, 0)

    blocks = await collect_memory_blocks(memory, now=now, self_memory=True, impressions=True)

    assert blocks.impressions == ""
    assert blocks.self_facts.startswith("【你的历史自述】")
    assert blocks.facts and blocks.diaries


# ==========================================
# 4) 只读接口不改库：过期 followup / 过期 desire
# ==========================================

async def _make_temp_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "memory_view_test.db"))
    await db.init_tables()
    return db


async def _snapshot(db: Database, sql: str) -> List[tuple]:
    rows = await db.fetchall(sql)
    return [tuple(r) for r in rows]


@pytest.mark.anyio
async def test_expired_followup_stays_untouched_after_readonly_read(tmp_path):
    """过期待跟进不进入提示词，但原行与 done 均不被只读接口修改。"""
    db = await _make_temp_db(tmp_path)
    try:
        now = datetime(2026, 10, 9, 20, 0)
        created = (now - timedelta(hours=72)).strftime(TIME_FORMAT)
        remind = (now - timedelta(hours=70)).strftime(TIME_FORMAT)
        await db.execute(
            "INSERT INTO followups (topic, remind_after, done, created_at) VALUES (?, ?, 0, ?)",
            ("他说的七天前的考试", remind, created),
        )
        valid_created = (now - timedelta(hours=2)).strftime(TIME_FORMAT)
        valid_remind = (now - timedelta(hours=1)).strftime(TIME_FORMAT)
        await db.execute(
            "INSERT INTO followups (topic, remind_after, done, created_at) VALUES (?, ?, 0, ?)",
            ("他说的最近终端结果", valid_remind, valid_created),
        )
        before = await _snapshot(db, "SELECT id, topic, remind_after, done, created_at FROM followups")

        memory = MemoryManager(db)
        rows = await memory.get_open_followups_readonly(due_only=True, limit=5, now=now)

        assert len(rows) == 1
        assert rows[0]["topic"] == "他说的最近终端结果"
        assert rows[0]["created_at"] == valid_created

        after = await _snapshot(db, "SELECT id, topic, remind_after, done, created_at FROM followups")
        assert after == before
        assert after[0][3] == 0  # done 未被改成过期标记
    finally:
        await db.close()


@pytest.mark.anyio
async def test_expired_desire_is_not_returned_but_row_is_kept(tmp_path):
    """过期念头（超过 TTL）不再返回，但行不被删除。"""
    db = await _make_temp_db(tmp_path)
    try:
        now = datetime(2026, 10, 9, 20, 0)
        created = (now - timedelta(hours=72)).strftime(TIME_FORMAT)
        await db.execute(
            "INSERT INTO suppressed_desires (content, created_at) VALUES (?, ?)",
            ("想问他那次面试顺不顺利", created),
        )
        before = await _snapshot(db, "SELECT id, content, created_at FROM suppressed_desires")

        memory = MemoryManager(db)
        rows = await memory.get_live_desires_readonly(limit=5, now=now)

        assert rows == []

        after = await _snapshot(db, "SELECT id, content, created_at FROM suppressed_desires")
        assert after == before
        assert len(after) == 1
    finally:
        await db.close()


# ==========================================
# 5) 之后又有用户消息：desire 不返回，行仍在
# ==========================================

@pytest.mark.anyio
async def test_desire_dropped_after_later_user_message_but_row_kept(tmp_path):
    """念头之后他又找她聊过天：不再取用，行保留。"""
    db = await _make_temp_db(tmp_path)
    try:
        now = datetime(2026, 10, 9, 20, 0)
        desire_created = (now - timedelta(hours=1)).strftime(TIME_FORMAT)
        user_created = (now - timedelta(minutes=10)).strftime(TIME_FORMAT)
        await db.execute(
            "INSERT INTO suppressed_desires (content, created_at) VALUES (?, ?)",
            ("我本来想问他是不是不开心", desire_created),
        )
        await db.execute(
            "INSERT INTO turns (role, content, proactive, has_image, created_at) VALUES ('user', ?, 0, 0, ?)",
            ("在吗", user_created),
        )
        before = await _snapshot(db, "SELECT id, content, created_at FROM suppressed_desires")

        memory = MemoryManager(db)
        rows = await memory.get_live_desires_readonly(limit=5, now=now)

        assert rows == []

        after = await _snapshot(db, "SELECT id, content, created_at FROM suppressed_desires")
        assert after == before
        assert after[0][1] == "我本来想问他是不是不开心"
    finally:
        await db.close()


# ==========================================
# 6) 全程离线：FakeGateway 从未被调用
# ==========================================

@pytest.mark.anyio
async def test_collect_memory_blocks_with_real_memory_never_calls_gateway(tmp_path):
    """真 MemoryManager + 假网关：只读采集全程不调模型。"""
    db = await _make_temp_db(tmp_path)
    try:
        now = datetime(2026, 10, 9, 20, 0)
        created = (now - timedelta(hours=1)).strftime(TIME_FORMAT)
        await db.execute(
            "INSERT INTO suppressed_desires (content, created_at) VALUES (?, ?)",
            ("想说但没说的那句话", created),
        )
        await db.execute(
            "INSERT INTO followups (topic, remind_after, done, created_at) VALUES (?, ?, 0, ?)",
            ("他说的面试", created, created),
        )

        gateway = FakeGateway()
        memory = MemoryManager(db, gateway=gateway)

        blocks = await collect_memory_blocks(memory, now=now, self_memory=True, impressions=True)

        assert blocks.desires.startswith("【欲言又止｜历史念头】")
        assert blocks.followups.startswith("【待跟进】")
        assert gateway.calls == []
    finally:
        await db.close()


class FakeMeetups:
    async def pending(self):
        return [
            {"when_at": "2026-10-10 18:00", "title": "未来约定"},
            {"when_at": "2026-10-08 18:00", "title": "过去约定"},
        ]
    @staticmethod
    def describe(row):
        return row["title"]


class FakeMeetupsAllInvalid:
    """when_at 全是脏数据的见面清单。"""

    async def pending(self):
        return [
            {"when_at": "不是时间", "title": "脏约定甲"},
            {"when_at": "", "title": "脏约定乙"},
            {"when_at": None, "title": "脏约定丙"},
            {"when_at": "2026-13-45 99:99", "title": "脏约定丁"},
        ]

    @staticmethod
    def describe(row):
        return row["title"]


class FakeMeetupsMixed:
    """有效日期与无效日期混在一起的见面清单。"""

    async def pending(self):
        return [
            {"when_at": "2026-10-08 18:00", "title": "过去约定"},
            {"when_at": "下周三", "title": "脏约定"},
            {"when_at": "2026-10-10 18:00", "title": "未来约定"},
            {"when_at": "", "title": "空约定"},
        ]

    @staticmethod
    def describe(row):
        return row["title"]


@pytest.mark.anyio
async def test_meetups_skips_rows_with_unparsable_when_at():
    """when_at 解析不了的脏数据既不归入 upcoming，也不当成已经发生。"""
    blocks = await collect_memory_blocks(
        FakeMemory(), now=datetime(2026, 10, 9, 20, 0), meetups=FakeMeetupsAllInvalid(),
    )
    # 一条有效条目都没有：整块省略，不能只剩一个空标题
    assert blocks.meetups == ""
    assert "【约好的见面】" not in blocks.meetups
    assert "还没到的" not in blocks.meetups
    assert "已经过了的" not in blocks.meetups
    assert "脏约定" not in blocks.meetups


@pytest.mark.anyio
async def test_meetups_keeps_valid_rows_and_drops_invalid_ones():
    """混合清单里有效条目照旧分栏，无效条目整条跳过。"""
    blocks = await collect_memory_blocks(
        FakeMemory(), now=datetime(2026, 10, 9, 20, 0), meetups=FakeMeetupsMixed(),
    )
    assert blocks.meetups.startswith("【约好的见面】")
    assert "还没到的" in blocks.meetups and "未来约定" in blocks.meetups
    assert "已经过了的" in blocks.meetups and "过去约定" in blocks.meetups
    # 无效条目不出现在任何一栏
    assert "脏约定" not in blocks.meetups
    assert "空约定" not in blocks.meetups
    # 有效条目的原有顺序与说明保持
    assert blocks.meetups.index("还没到的") < blocks.meetups.index("未来约定")
    assert blocks.meetups.index("已经过了的") < blocks.meetups.index("过去约定")


@pytest.mark.anyio
async def test_meetups_empty_when_no_rows_at_all():
    """没有任何待办见面时整块省略。"""

    class FakeMeetupsEmpty:
        async def pending(self):
            return []

        @staticmethod
        def describe(row):
            return ""

    blocks = await collect_memory_blocks(
        FakeMemory(), now=datetime(2026, 10, 9, 20, 0), meetups=FakeMeetupsEmpty(),
    )
    assert blocks.meetups == ""


@pytest.mark.anyio
async def test_meetups_split_upcoming_and_past_without_mutation():
    blocks = await collect_memory_blocks(
        FakeMemory(), now=datetime(2026, 10, 9, 20, 0), meetups=FakeMeetups(),
    )
    assert blocks.meetups.startswith("【约好的见面】")
    assert "还没到的" in blocks.meetups and "未来约定" in blocks.meetups
    assert "已经过了的" in blocks.meetups and "过去约定" in blocks.meetups
