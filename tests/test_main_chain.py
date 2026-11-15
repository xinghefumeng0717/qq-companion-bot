"""主链行为：他说一句 → 生成 → 真实投递 → 入库 → 下一轮读到什么。

用真实数据库、真实记忆、真实事件账本、真实内核和真实执行器；
只有模型出口和发送器是脚本化的假件。不连接真实 QQ，不调用真实模型。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from companion.kernel.context import ContextBuilder
from companion.kernel.contracts import SendReceipt
from companion.kernel.delivery import (
    DeliveryExecutor,
    DeliveryRejectedError,
    UnconfirmedDeliveryError,
)
from companion.kernel.kernel import DialogueKernel
from companion.kernel.production import (
    MODEL_FAILURE_TEXTS,
    VOICE_FACT_RETRY,
    VOICE_FACT_TEXT_ONLY,
    VOICE_FACT_WILL_SEND,
    KernelTurnHandler,
    _CLOSING_WINDOW,
)
from companion.kernel.psychology import PsychologicalSnapshotBuilder
from companion.kernel.tracing import JsonlTraceRecorder
from companion.kernel.expression import ExpressionPicker
from companion.runtime_state import RuntimeEventStore
from companion.voice_planner import VoicePlanner
from companion.voice_planner import is_voice_request
from helpers import card_path, close_db, make_db, make_engine_stack

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class ScriptedGateway:
    """按顺序吐出预设回复，并记下每次真正发给模型的 messages。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.config = SimpleNamespace(observer_model="offline")
        self.last_served_model = "offline-model-0000"

    async def stream_chat(self, messages, model=None, purpose="main_chat", **_kw):
        self.calls.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        yield reply


class ScriptedSender:
    """每次发送按脚本给出 ok / reject / unconfirmed / cancel；默认 ok。"""

    def __init__(self, script=()):
        self.script = list(script)
        self.sent = []
        self.next_id = 100

    def _outcome(self, channel, text):
        outcome = self.script.pop(0) if self.script else "ok"
        if outcome == "reject":
            raise DeliveryRejectedError("offline reject")
        if outcome == "unconfirmed":
            raise UnconfirmedDeliveryError("offline unknown")
        if outcome == "cancel":
            raise asyncio.CancelledError
        self.next_id += 1
        self.sent.append((channel, text))
        return self.next_id

    async def send_text(self, text, quote_message_id=None):
        return SendReceipt(
            message_id=self._outcome("text", text), actual_quote_message_id=quote_message_id
        )

    async def send_voice(self, text, quote_message_id=None, **_options):
        return SendReceipt(message_id=self._outcome("voice", text))

    async def send_sticker(self, sticker):
        return SendReceipt(message_id=self._outcome("sticker", str(sticker)))


class Chain:
    """一套接好线的主链，测试只关心它的输入输出。"""

    def __init__(self, db, tmp_path, replies, sender_script=(), voice_allowed=True,
                 content_voice=False, first_reply=None, picks=None):
        stack = make_engine_stack(db, persona_path=card_path())
        self.db = db
        self.memory = stack.memory
        self.events = RuntimeEventStore(db)
        self.gateway = ScriptedGateway(replies)
        self.sender = ScriptedSender(sender_script)
        self.observed = []
        self.unanswered = 0
        self.typing = []          # 每次"正在输入"演的是哪些字
        self.waited_first = 0     # 首条延迟被调用了几次
        self.tracer = JsonlTraceRecorder(str(tmp_path))
        kernel = DialogueKernel(
            gateway=self.gateway,
            persona=stack.persona,
            snapshot_builder=PsychologicalSnapshotBuilder(
                affection_engine=stack.affection,
                mood_engine=stack.mood,
                persona=stack.persona,
                holidays_provider=lambda: [],
            ),
            context_builder=ContextBuilder(),
            tracer=self.tracer,
            memory=self.memory,
            runtime_events=self.events,
            model="offline",
        )

        async def gate(_user_text=""):
            return voice_allowed, "" if voice_allowed else "今天的语音额度用完了"

        async def wait_first_reply(_memory):
            self.waited_first += 1
            return bool(first_reply)

        async def play_typing(text, is_first):
            self.typing.append((text, is_first))

        runtime = SimpleNamespace(
            assembler=SimpleNamespace(meetups=None, voice_frequency="frequent"),
            config=None,
            runtime_events=self.events,
            voice_planner=(
                VoicePlanner("frequent", max_chars=60) if content_voice
                else SimpleNamespace(allows=lambda _text: True)
            ),
            wait_first_reply=wait_first_reply,
            play_typing=play_typing,
            _check_voice_gate=gate,
            _voice_max_chars=lambda: 60,
            _last_voice_sent=False,
            _last_voice_failed=False,
        )

        async def settle_turn(**kwargs):
            self.observed.append(kwargs)

        async def noop(*_a, **_k):
            return None

        async def bump():
            self.unanswered += 1

        async def unanswered_getter():
            return self.unanswered

        self.unanswered_getter = unanswered_getter

        async def voice_gate_ok():
            return False

        self.handler = KernelTurnHandler(
            kernel=kernel,
            runtime=runtime,
            memory=self.memory,
            observer=SimpleNamespace(settle_turn=settle_turn),
            proactive=SimpleNamespace(
                reset_unanswered_count=noop,
                increment_unanswered_count=bump,
                _voice_gate_ok=voice_gate_ok,
                _play_typing_indicator=noop,
            ),
            delivery_executor=DeliveryExecutor(self.sender, tracer=self.tracer),
            expression_picker=self._picker(stack, picks),
        )

    def _picker(self, stack, picks):
        """picks：每次挑表情时后台模型返回的名字（脚本化）；None 表示不装挑选器。"""
        if picks is None:
            return None
        self.pick_calls = []
        script = list(picks)

        async def chat(messages, **_kw):
            self.pick_calls.append(messages[-1]["content"])
            return '{"pick": "%s"}' % (script.pop(0) if script else "")

        return ExpressionPicker(SimpleNamespace(chat=chat), stack.stickers, self.events)

    async def say(self, text, batch=None):
        await self.handler.handle_turn(text, None, batch)
        await asyncio.sleep(0)

    async def turns(self):
        rows = await self.db.fetchall("SELECT role, content FROM turns ORDER BY id")
        return [(row["role"], row["content"]) for row in rows]

    def last_prompt(self):
        return self.gateway.calls[-1]

    def delivery_notes(self):
        """上一份提示词里跟在历史轮次后面的投递记录。"""
        return [
            m["content"] for m in self.last_prompt()
            if m["role"] == "system" and m["content"].startswith("（投递记录")
        ]

    def state_note(self):
        """上一份提示词里紧挨着他这句话的"她此刻的状态"。"""
        prompt = self.last_prompt()
        note = prompt[-2]
        assert note["role"] == "system" and note["content"].startswith("【你此刻的状态】")
        return note["content"]


@pytest.fixture
async def make_chain(tmp_path):
    opened = []

    async def _make(replies, **kwargs):
        db = await make_db()
        await RuntimeEventStore(db).init()
        opened.append(db)
        return Chain(db, tmp_path, replies, **kwargs)

    yield _make
    for db in opened:
        await close_db(db)


# ---------- 说了什么 = 发了什么 = 存了什么 ----------

async def test_plain_reply_is_sent_bubble_by_bubble_and_saved_as_delivered(make_chain):
    chain = await make_chain(["在呢\n怎么了"])
    await chain.say("在吗")
    assert chain.sender.sent == [("text", "在呢"), ("text", "怎么了")]
    assert await chain.turns() == [("user", "在吗"), ("assistant", "在呢\n怎么了")]
    assert len(chain.observed) == 1


async def test_rejected_bubble_stops_the_rest_and_history_keeps_only_what_arrived(make_chain):
    chain = await make_chain(["第一句到了\n第二句被拒\n第三句没发"], sender_script=["ok", "reject"])
    await chain.say("说点什么")
    assert chain.sender.sent == [("text", "第一句到了")]
    assert await chain.turns() == [("user", "说点什么"), ("assistant", "第一句到了")]


async def test_nothing_confirmed_keeps_his_words_and_never_resends(make_chain):
    chain = await make_chain(["这句不确定有没有到", "下一轮正常"], sender_script=["unconfirmed"])
    await chain.say("在吗")
    assert chain.sender.sent == []
    assert await chain.turns() == [("user", "在吗")]
    assert chain.observed == []
    await chain.say("喂")
    assert chain.sender.sent == [("text", "下一轮正常")]


async def test_model_failure_uses_a_system_line_and_never_calls_the_model_twice(make_chain):
    chain = await make_chain([RuntimeError("model down")])
    await chain.say("在吗")
    assert len(chain.gateway.calls) == 1
    turns = await chain.turns()
    assert turns[0] == ("user", "在吗")
    assert turns[1][1] in MODEL_FAILURE_TEXTS
    assert chain.observed == []


async def test_protocol_markup_from_the_model_is_never_delivered(make_chain):
    chain = await make_chain(["[voice:]偷偷写协议[/voice]"])
    await chain.say("在吗")
    assert all("voice" not in text for _channel, text in chain.sender.sent)
    assert (await chain.turns())[1][1] in MODEL_FAILURE_TEXTS


# ---------- 他的话先入库；停机中途不丢已发出的部分 ----------

async def test_his_message_is_stored_before_generation_and_not_duplicated_in_prompt(make_chain):
    chain = await make_chain(["嗯"])
    await chain.say("这句只该出现一次")
    prompt = chain.last_prompt()
    mentions = [m for m in prompt if m["role"] == "user" and "这句只该出现一次" in m["content"]]
    assert len(mentions) == 1


async def test_cancel_mid_delivery_saves_what_was_already_sent(make_chain):
    chain = await make_chain(["已经发出去的\n这句发到一半被停机打断"], sender_script=["ok", "cancel"])
    with pytest.raises(asyncio.CancelledError):
        await chain.say("在吗")
    assert await chain.turns() == [("user", "在吗"), ("assistant", "已经发出去的")]


# ---------- 历史窗口 ----------

async def test_history_window_is_ten_exchanges_and_starts_with_him(make_chain):
    chain = await make_chain([f"回{i}" for i in range(13)])
    for i in range(13):
        await chain.say(f"问{i}")
    history = chain.last_prompt()[1:-2]   # 去掉开头的系统提示词、末尾的状态说明和他这句话
    assert len(history) == 20
    assert history[0] == {"role": "user", "content": "问2"}
    assert history[-1] == {"role": "assistant", "content": "回11"}


# ---------- 语音：生成前告知，结果挂在那一轮上 ----------

@pytest.mark.parametrize("text, expected", [
    ("发个语音听听呗", True),
    ("来条语音听听嘛", True),
    ("好久没听你声音了", True),
    ("想听你说话", True),
    ("赏个语音呗", True),
    ("我这边就看到你打字，语音呢", True),
    ("要不要发条语音", True),
    ("别发语音", False),
    ("不用发语音", False),
    ("不要语音", False),
    ("QQ 还吃语音，真会挑时候", False),
    ("外面声音好吵", False),
    ("刚才那条语音真好听", False),
    ("说话声音小点", False),
])
def test_voice_request_means_asking_her_to_speak_not_mentioning_the_word(text, expected):
    assert is_voice_request(text) is expected


async def test_voice_request_is_told_to_her_before_she_writes_and_marked_in_history(make_chain):
    chain = await make_chain(["就这一条啊\n听完去吃饭", "嗯"])
    await chain.say("发条语音听听")
    assert VOICE_FACT_WILL_SEND in chain.last_prompt()[0]["content"]
    assert chain.sender.sent == [("voice", "就这一条啊"), ("text", "听完去吃饭")]
    assert (await chain.turns())[1] == ("assistant", "就这一条啊\n听完去吃饭")

    await chain.say("听到了")
    prompt = chain.last_prompt()
    assert VOICE_FACT_WILL_SEND not in prompt[0]["content"]
    notes = chain.delivery_notes()
    assert len(notes) == 1 and "“就这一条啊”是用语音发出的" in notes[0]
    assert prompt[prompt.index({"role": "system", "content": notes[0]}) - 1]["role"] == "assistant"


async def test_failed_voice_falls_back_to_same_text_and_next_turn_reads_it_beside_that_turn(make_chain):
    chain = await make_chain(["行吧就一条\n开免提听", "那就算了"], sender_script=["reject"])
    await chain.say("来条语音")
    assert chain.sender.sent == [("text", "行吧就一条"), ("text", "开免提听")]

    await chain.say("没收到诶")
    notes = chain.delivery_notes()
    assert len(notes) == 1 and "没有发出去" in notes[0] and "没有收到语音" in notes[0]


async def test_failed_voice_is_resent_once_on_the_next_turn_and_not_again(make_chain):
    chain = await make_chain(
        ["行吧就一条", "等着我再发一条", "这下总行了吧"], sender_script=["reject", "ok", "ok"],
    )
    await chain.say("来条语音")
    await chain.say("没收到诶")
    assert VOICE_FACT_RETRY in chain.last_prompt()[0]["content"]
    assert chain.sender.sent[-1] == ("voice", "等着我再发一条")
    await chain.say("这回听到了")
    assert "【本轮投递】" not in chain.last_prompt()[0]["content"]
    assert chain.sender.sent[-1] == ("text", "这下总行了吧")


async def test_voice_refusal_cancels_pending_retry(make_chain):
    chain = await make_chain(["行吧就一条", "好，不发了"], sender_script=["reject"])
    await chain.say("来条语音")
    await chain.say("算了，别发语音了")
    assert chain.sender.sent[-1] == ("text", "好，不发了")
    assert VOICE_FACT_RETRY not in chain.last_prompt()[0]["content"]


async def test_voice_retry_expires_after_closing_window(make_chain):
    chain = await make_chain(["行吧就一条", "早上好"], sender_script=["reject"])
    await chain.say("来条语音")
    chain.handler._runtime._voice_retry_requested_at = (
        datetime.now() - _CLOSING_WINDOW - timedelta(seconds=1)
    )
    await chain.say("早上好")
    assert chain.sender.sent[-1] == ("text", "早上好")
    assert VOICE_FACT_RETRY not in chain.last_prompt()[0]["content"]


@pytest.mark.parametrize("interruption", [RuntimeError("model down"), asyncio.CancelledError()])
async def test_voice_retry_survives_failed_or_cancelled_generation(make_chain, interruption):
    chain = await make_chain(["先给你", interruption, "这次送到了"], sender_script=["reject"])
    await chain.say("来条语音")
    if isinstance(interruption, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await chain.say("没收到")
    else:
        await chain.say("没收到")
    await chain.say("再试一次")
    assert VOICE_FACT_RETRY in chain.last_prompt()[0]["content"]
    assert chain.sender.sent[-1] == ("voice", "这次送到了")


async def test_voice_gate_closed_is_told_as_text_only_and_nothing_is_voiced(make_chain):
    chain = await make_chain(["今天嗓子歇了"], voice_allowed=False)
    await chain.say("发条语音吧")
    assert VOICE_FACT_TEXT_ONLY in chain.last_prompt()[0]["content"]
    assert chain.sender.sent == [("text", "今天嗓子歇了")]


async def test_mentioning_voice_without_asking_sends_no_voice(make_chain):
    chain = await make_chain(["它就爱挑这种时候"])
    await chain.say("QQ 还吃语音，真会挑时候")
    assert "【本轮投递】" not in chain.last_prompt()[0]["content"]
    assert chain.sender.sent == [("text", "它就爱挑这种时候")]


# ---------- 道别之后只补一个表情：这轮不回 ----------

async def test_face_only_after_goodbye_gets_no_reply_but_is_remembered(make_chain):
    chain = await make_chain(["去吧", "嗯", "新的一句"])
    await chain.say("先撤了，去开会")
    await chain.say("好～")
    calls = len(chain.gateway.calls)
    await chain.say("[吃糖][吃糖]")
    assert len(chain.gateway.calls) == calls
    assert (await chain.turns())[-1] == ("user", "[吃糖][吃糖]")
    await chain.say("开完了")
    assert chain.sender.sent[-1] == ("text", "新的一句")


async def test_face_only_in_ordinary_chat_is_answered(make_chain):
    chain = await make_chain(["下午画了幅山", "卖什么乖"])
    await chain.say("下午在干嘛")
    await chain.say("[吃糖]")
    assert chain.sender.sent[-1] == ("text", "卖什么乖")


async def test_structured_face_is_shown_with_its_meaning(make_chain):
    chain = await make_chain(["卖什么乖"])
    batch = [{"index": 1, "text": "[吃糖]", "message_id": 1, "has_image": False,
              "parts": [{"kind": "face", "text": "[吃糖]"}]}]
    await chain.say("[吃糖]", batch)
    assert "“吃糖”（卖萌、心情好）" in chain.last_prompt()[-1]["content"]


# ---------- 主动消息走同一条链 ----------

async def test_proactive_message_uses_the_same_chain_and_is_saved_only_when_delivered(make_chain):
    chain = await make_chain(["醒了没", "这条发不出去"], sender_script=["ok", "unconfirmed"])
    assert await chain.handler.handle_proactive("早上想问他醒了没") is True
    assert chain.gateway.calls[0][-1]["role"] == "system"
    assert await chain.turns() == [("assistant", "醒了没")]
    assert chain.unanswered == 1

    assert await chain.handler.handle_proactive("再问一次") is False
    assert await chain.turns() == [("assistant", "醒了没")]
    assert chain.unanswered == 1


# ---------- 追踪：同一个 trace 里既有她写的，也有实际发的 ----------

async def test_one_trace_holds_both_the_draft_and_the_real_delivery(make_chain, tmp_path):
    chain = await make_chain(["在呢"])
    await chain.say("在吗")
    lines = [line for path in tmp_path.glob("*.jsonl") for line in path.read_text("utf-8").splitlines()]
    import json

    events = [json.loads(line) for line in lines]
    raw = next(e for e in events if e["step"] == "model_raw")
    delivered = next(e for e in events if e["step"] == "delivery_result")
    assert raw["trace_id"] == delivered["trace_id"]
    assert raw["payload"]["served_model"] == "offline-model-0000"


# ---------- 回复节奏：首条先晾一会儿，发送前"正在输入" ----------

async def test_first_reply_wait_runs_before_generation_and_typing_covers_text_only(make_chain):
    chain = await make_chain(["就这一条啊\n听完去吃饭"], first_reply=True)
    await chain.say("发条语音听听")
    assert chain.waited_first == 1
    # 语音那条不是她打出来的字，只演文字气泡；首条标记原样传给节奏层
    assert chain.typing == [("听完去吃饭", True)]


async def test_no_typing_when_she_does_not_reply(make_chain):
    chain = await make_chain(["去吧"])
    await chain.say("先撤了，去开会")
    await chain.say("[吃糖]")
    assert len(chain.typing) == 1


# ---------- 他没要语音时，程序看内容挑一句念 ----------

async def test_content_voice_picks_a_whole_bubble_without_telling_her(make_chain):
    chain = await make_chain(["晚安，早点睡\n明天再说", "嗯"], content_voice=True)
    await chain.say("我去睡了")
    assert "【本轮投递】" not in chain.last_prompt()[0]["content"]
    assert chain.sender.sent == [("voice", "晚安，早点睡"), ("text", "明天再说")]
    await chain.say("到了")
    notes = chain.delivery_notes()
    assert notes and "“晚安，早点睡”是用语音发出的" in notes[0]


async def test_content_voice_failure_is_plain_text_with_no_note_and_no_retry(make_chain):
    chain = await make_chain(
        ["晚安，早点睡", "在呢，怎么了"], content_voice=True, sender_script=["reject"],
    )
    await chain.say("我去睡了")
    assert chain.sender.sent == [("text", "晚安，早点睡")]
    await chain.say("还在吗")
    prompt = chain.last_prompt()
    assert chain.delivery_notes() == []
    assert chain.sender.sent[-1] == ("text", "在呢，怎么了")


async def test_plain_information_is_not_voiced(make_chain):
    chain = await make_chain(["到了，客栈门口"], content_voice=True)
    await chain.say("到了吗")
    assert chain.sender.sent == [("text", "到了，客栈门口")]


# ---------- 系统替她挑表情 ----------

async def test_system_picked_sticker_follows_the_last_bubble_and_is_read_back(make_chain):
    chain = await make_chain(["第一句\n第二句", "嗯"], picks=["开心"])
    await chain.say("今天发工资了")
    assert [channel for channel, _ in chain.sender.sent] == ["text", "text", "sticker"]
    assert (await chain.turns())[1] == ("assistant", "第一句\n第二句")   # 表情不进正文
    await chain.say("哈哈")
    notes = chain.delivery_notes()
    assert notes and "还发了一个表情“开心”" in notes[0]


async def test_expression_cools_down_and_ignores_unknown_names(make_chain):
    chain = await make_chain(["甲", "乙", "丙"], picks=["开心", "收到", "不存在的名字"])
    await chain.say("一")
    await chain.say("二")          # 冷却中：不调用挑选，也不发
    assert len(chain.pick_calls) == 1
    assert [c for c, _ in chain.sender.sent].count("sticker") == 1


async def test_sticker_send_failure_keeps_the_words_she_said(make_chain):
    chain = await make_chain(["就这句"], picks=["开心"], sender_script=["ok", "reject"])
    await chain.say("在吗")
    assert chain.sender.sent == [("text", "就这句")]
    assert await chain.turns() == [("user", "在吗"), ("assistant", "就这句")]
    assert len(chain.observed) == 1


# ---------- 她此刻的状态：紧挨着他这句话，先说发生了什么 ----------

async def test_state_note_sits_right_before_his_words_and_not_in_the_system_prompt(make_chain):
    chain = await make_chain(["在呢"])
    await chain.say("在吗")
    assert "内心感受" in chain.state_note()
    assert "【当前内心状态】" not in chain.last_prompt()[0]["content"]
    assert chain.last_prompt()[-1] == {"role": "user", "content": "在吗"}


async def test_long_gap_is_told_as_what_happened_even_though_his_message_is_stored_first(make_chain):
    chain = await make_chain(["去吧", "哟，还知道回来"])
    await chain.say("先去忙了")
    await chain.db.execute("UPDATE turns SET created_at = '2020-01-01 08:00'")
    await chain.say("我回来了")
    assert "没找你了" in chain.state_note()
    assert "距上次聊天已过" in chain.last_prompt()[0]["content"]


async def test_unanswered_proactive_pings_are_part_of_what_happened(make_chain):
    chain = await make_chain(["醒了没", "才回啊"])
    await chain.handler.handle_proactive("想问他醒了没")
    chain.handler.proactive.get_unanswered_count = chain.unanswered_getter
    await chain.say("刚醒")
    assert "你之前主动找过他 1 次，他到现在才回" in chain.state_note()
