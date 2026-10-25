"""全流程沙箱 (scripts/sim/full_sandbox.py)

和 duo_sim / final_rehearsal 的区别：这里直接起**生产同一个 CompanionBot**（main.py 的完整接线：
聚合器、主聊、观察者、自我记忆、事实分层、看法、见面补记、主动消息、语音闸门与发送、
一步看图、表情包……），只把三样外部的东西换成假的：

  - QQ（OneBot）：假客户端，记下她发出的每条文字/语音/表情，可注入"发送失败"；
  - 语音合成：不真花钱合成，写个空文件当音频，可注入"合成失败"；
  - 时钟：所有 companion 模块的 datetime/date 换成可拨动的假钟，三天剧情一次跑完。

模拟的"他"（管理员）由 flash 扮演，按剧本每一拍的指令说话；关键拍用固定台词。
中途会**重启一次**（新起一个 CompanionBot 接同一个库），检查记忆和状态接得上。

产出 data/full_sandbox/<run_id>/：transcript.md（给所有者读）、checks.json（自动检查表）、
raw.json（全部原始数据）、sandbox.db、bot.log。

用法：
  venv/bin/python scripts/sim/full_sandbox.py --max-cost 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
import types
from dataclasses import dataclass, field
from datetime import date as _real_date
from datetime import datetime as _real_datetime
from datetime import timedelta
from typing import Any, Dict, List, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

# ---------------------------------------------------------------- 假钟


class Clock:
    now: _real_datetime = _real_datetime(2026, 10, 24, 12, 0)


class FakeDatetime(_real_datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: D401
        # 返回子类实例：模块里的 datetime 名字已经换成本类，isinstance(x, datetime) 才对得上
        return cls.combine(Clock.now.date(), Clock.now.time(), tzinfo=tz)

    @classmethod
    def today(cls):
        return cls.now()


class FakeDate(_real_date):
    @classmethod
    def today(cls):
        return Clock.now.date()


def patch_clock() -> None:
    # 先把 companion 下所有模块都导入，再统一换钟——否则后来才导入的模块（观察者、见面…）
    # 还在用真实时间（第一轮沙箱就栽在这：明天七点被记成了后天）
    import importlib
    import pkgutil

    import companion

    for info in pkgutil.iter_modules(companion.__path__):
        try:
            importlib.import_module(f"companion.{info.name}")
        except Exception:
            pass

    for name, mod in list(sys.modules.items()):
        if not name.startswith("companion") or mod is None:
            continue
        if getattr(mod, "datetime", None) is _real_datetime:
            mod.datetime = FakeDatetime
        if getattr(mod, "date", None) is _real_date:
            mod.date = FakeDate


# 打字表演、首条静默、聚合静默窗都是真 sleep：沙箱里压成一眨眼
_real_sleep = asyncio.sleep


async def _fast_sleep(delay=0, result=None):
    await _real_sleep(0 if delay <= 0 else 0.01)
    return result


def patch_sleep() -> None:
    fast = types.SimpleNamespace(**{k: getattr(asyncio, k) for k in dir(asyncio) if not k.startswith("__")})
    fast.sleep = _fast_sleep
    for name in ("companion.aggregator", "companion.turn_handler", "companion.replier",
                 "companion.main", "companion.proactive"):
        mod = sys.modules.get(name)
        if mod is not None and getattr(mod, "asyncio", None) is asyncio:
            mod.asyncio = fast


# ---------------------------------------------------------------- 假 QQ / 假合成


@dataclass
class Sent:
    at: str
    kind: str          # text / voice / sticker / face / combo / other
    content: str
    quote: Optional[int] = None


# 真实发生过的语音故障（剧情时间）：她说"没发出去"必须落在某次故障之后
VOICE_FAIL_AT: List[_real_datetime] = []
RUN_STARTED_REAL = _real_datetime.now()


class FakeOneBot:
    def __init__(self, log: List[Sent]):
        self.log = log
        self.fail_next_record = 0
        self.qq_rejected = 0
        self.next_message_id = 5000

    @property
    def is_connected(self) -> bool:
        return True

    async def set_input_status(self, user_id: int, typing: bool) -> bool:
        return True

    async def send_private_msg(self, user_id: int, segs: List[Dict[str, Any]]) -> bool:
        kinds = [s.get("type") for s in segs]
        if "record" in kinds and self.fail_next_record > 0:
            self.fail_next_record -= 1
            self.qq_rejected += 1
            VOICE_FAIL_AT.append(Clock.now)
            self.log.append(Sent(Clock.now.strftime("%m-%d %H:%M"), "voice_failed", "（语音发送被 QQ 拒绝：注入故障）"))
            return False
        quote = None
        parts = []
        kind = "text"
        for s in segs:
            t, d = s.get("type"), s.get("data", {})
            if t == "reply":
                quote = d.get("id")
            elif t == "text":
                parts.append(d.get("text", ""))
            elif t == "record":
                kind = "voice"
                parts.append(_VOICE_TEXT.get(d.get("file", ""), "（语音）"))
            elif t == "image":
                kind = "sticker"
                parts.append(f"（表情包 {_sticker_name(str(d.get('file', '')))}）")
            elif t == "face":
                parts.append(f"（QQ表情 {_face_name(d.get('id'))}）")
        self.log.append(Sent(Clock.now.strftime("%m-%d %H:%M"), kind, "".join(parts), quote))
        return True


_VOICE_TEXT: Dict[str, str] = {}


def _sticker_name(file_field: str) -> str:
    """发送层把表情包转成 base64://，按内容反查文件名，transcript 里好读。"""
    import base64
    import glob
    import hashlib

    if file_field.startswith("base64://"):
        digest = hashlib.md5(base64.b64decode(file_field[9:])).hexdigest()
        search_dirs = [os.path.join("characters", "example", "stickers")]
        if os.path.exists("characters"):
            for c in os.listdir("characters"):
                sdir = os.path.join("characters", c, "stickers")
                if os.path.isdir(sdir) and sdir not in search_dirs:
                    search_dirs.append(sdir)
        for sdir in search_dirs:
            for path in glob.glob(os.path.join(sdir, "*")):
                try:
                    with open(path, "rb") as f:
                        if hashlib.md5(f.read()).hexdigest() == digest:
                            return os.path.splitext(os.path.basename(path))[0]
                except Exception:
                    pass
        return "?"
    return os.path.basename(file_field)


def install_fake_tts(bot, out_dir: str, state: Dict[str, Any]) -> None:
    async def synth(text: str) -> Optional[str]:
        state["synth_calls"] += 1
        if state.get("fail_next_synth", 0) > 0:
            state["fail_next_synth"] -= 1
            state["synth_failed"] += 1
            VOICE_FAIL_AT.append(Clock.now)
            return None
        import base64

        path = os.path.join(out_dir, f"voice_{state['synth_calls']:03d}.mp3")
        blob = f"ID3-fake-{state['synth_calls']}".encode()
        with open(path, "wb") as f:
            f.write(blob)
        # 发送层会把本地文件转成 base64:// 段，按内容反查是哪句话
        _VOICE_TEXT["base64://" + base64.b64encode(blob).decode()] = text
        _VOICE_TEXT["file://" + path] = text
        _VOICE_TEXT[path] = text
        return path

    bot.tts.synthesize = synth
    bot.tts.cleanup = lambda p: None


# ---------------------------------------------------------------- 模拟的"他"


USER_BRIEF = """你在扮演机主——平日生活与工作很忙，跟她（常伴朋友）认识了很久，
现在互有好感但没挑明。你用 QQ 跟她聊天。你的聊天习惯：
- 一条消息很短，常常一次连发两三条；口语化，爱用"～""嘞""勒""嘛""啧啧啧""诶别啊""哎呦"；
- 喜欢逗她、偶尔开开玩笑；很喜欢听她的声音，常讨语音；
- 会发 QQ 表情，写成 [吃糖] [惊恐] [doge] [[好]] 这种方括号；
- 有时只回"好～""嗯嗯"或一个表情；不长篇大论，不说教，不用书面语。
只输出你要发的消息本身，多条用换行分开，最多 3 条；不要加引号、旁白、动作描写。"""


async def user_say(gateway, history: List[str], instruction: str) -> List[str]:
    messages = [
        {"role": "system", "content": USER_BRIEF},
        {"role": "user", "content": "最近的聊天（旧→新）：\n" + ("\n".join(history[-14:]) or "（还没聊）")
         + f"\n\n这一拍你要做的：{instruction}\n直接写你发给她的消息。"},
    ]
    try:
        text = await gateway.chat(messages=messages, purpose="sim_user", temperature=0.9)
    except Exception as e:
        logging.getLogger("sandbox").warning(f"模拟用户生成失败: {e}")
        return ["嗯嗯"]
    lines = [ln.strip().strip("“”\"") for ln in (text or "").split("\n") if ln.strip()]
    return lines[:3] or ["嗯嗯"]


# ---------------------------------------------------------------- 剧本


@dataclass
class Beat:
    at: str                        # "D1 12:10"
    kind: str = "chat"             # chat / proactive / restart / note
    say: Optional[List[str]] = None  # 固定台词（不走模拟用户）
    goal: str = ""                 # 交给模拟用户的指令
    image: Optional[str] = None
    fail_synth: int = 0            # 本拍前注入：下 N 次语音合成失败
    fail_record: int = 0           # 本拍前注入：下 N 次语音发送被 QQ 拒绝
    tag: str = ""                  # 检查表用的标签


def build_script(sticker_img: Optional[str] = None) -> List[Beat]:
    if sticker_img is None:
        example_stickers = [
            os.path.join("characters", "example", "stickers", f)
            for f in ("猫猫探头.png", "开心.png", "发呆.png")
            if os.path.exists(os.path.join("characters", "example", "stickers", f))
        ]
        img = example_stickers[0] if example_stickers else None
    else:
        img = sticker_img
    return [
        Beat("D1 12:15", "proactive", tag="开场主动"),
        Beat("D1 12:16", goal="刚吃完饭有空，回她一下，顺便说好久没听她声音了，想听", tag="讨要语音"),
        Beat("D1 12:18", goal="接她的话，继续逗她"),
        Beat("D1 12:20", say=["快发条语音听听嘛～"], fail_synth=1, tag="讨要语音-合成失败"),
        Beat("D1 12:21", say=["发条语音我就不闹了怎么样"], tag="讨要语音"),
        Beat("D1 12:22", say=["成交"], tag="讨要语音-延续"),
        Beat("D1 12:23", goal="如果刚才真收到了语音就夸一句；如果只看到文字就说没收到、问她是不是耍赖", tag="讨要语音-延续"),
        Beat("D1 12:25", say=["对了我最近想学做锅底", "你教我呗"], tag="埋记忆:学锅底"),
        Beat("D1 12:28", goal="说要去开会了，跟她道别"),
        Beat("D1 12:29", say=["好～"], tag="道别拖尾"),
        Beat("D1 12:30", say=["[吃糖][吃糖]"], tag="道别拖尾"),
        Beat("D1 20:40", goal="忙完了回来找她，问她今天下午在干嘛"),
        Beat("D1 20:43", say=["你看这个", "像不像你"], image=img, tag="发图"),
        Beat("D1 20:46", goal="顺着聊，然后约她明天晚上七点在武陵城一起吃新开的锅子店，要明确说时间地点"),
        Beat("D1 20:48", goal="她答应了就确认一下'那说定了，明晚七点武陵城'；没答应就再软磨一下", tag="约见面"),
        Beat("D1 23:30", goal="说困了要睡了，跟她说晚安"),
        Beat("D2 09:10", "proactive", tag="早上主动"),
        Beat("D2 10:40", "proactive"),
        Beat("D2 12:10", "proactive"),
        Beat("D2 15:05", goal="上午一直在开会没看消息，现在才回她，简单解释一下；提到约会时说'今晚七点武陵城'（就是今天晚上）"),
        Beat("D2 15:08", say=["来条语音听听嘛"], fail_record=1, tag="讨要语音-QQ拒绝"),
        Beat("D2 15:10", say=["没收到诶"], tag="没收到"),
        Beat("D2 15:12", goal="随便聊两句，然后说要先去忙了，今晚七点锅子店见"),
        Beat("D2 22:30", goal="吃完锅子回来了，说今天的锅子不错，问她觉得怎么样", tag="见面之后"),
        Beat("D2 22:35", goal="接着聊几句今晚的事，然后说晚安"),
        Beat("D3 08:00", "restart", tag="重启"),
        Beat("D3 10:20", say=["早～", "考考你 还记得我前天说想学啥不"], tag="重启后记忆"),
        Beat("D3 10:23", goal="顺着聊，问她昨晚锅子店哪道菜她最喜欢", tag="重启后见面记忆"),
        Beat("D3 10:26", say=["好～"], tag="道别拖尾"),
        Beat("D3 10:27", say=["嗯嗯"], tag="道别拖尾"),
    ]


def beat_time(beat: Beat, day1: _real_datetime) -> _real_datetime:
    d, hm = beat.at.split()
    h, m = hm.split(":")
    return day1.replace(hour=int(h), minute=int(m)) + timedelta(days=int(d[1:]) - 1)


# ---------------------------------------------------------------- 运行


@dataclass
class Turn:
    at: str
    who: str        # 他 / 她 / 系统
    text: str
    kind: str = "text"
    tag: str = ""


class Sandbox:
    def __init__(self, args):
        self.args = args
        self.run_dir = os.path.join(ROOT, "data", "full_sandbox", args.run_id)
        os.makedirs(self.run_dir, exist_ok=False)
        self.db_path = os.path.join(self.run_dir, "sandbox.db")
        self.sent: List[Sent] = []
        self.turns: List[Turn] = []
        self.tts_state = {"synth_calls": 0, "synth_failed": 0, "fail_next_synth": 0}
        self.bot = None
        self.onebot = FakeOneBot(self.sent)
        self.turn_done = asyncio.Event()
        self.turn_seconds: List[float] = []
        self.errors: List[str] = []
        self.restarts = 0

    def config(self):
        from companion.config import Config

        cfg = Config.load(self.args.config)
        card_path = getattr(self.args, "card", None) or "characters/example"
        cfg.character.path = card_path
        profile_path = getattr(self.args, "profile", None)
        if profile_path and os.path.exists(profile_path):
            cfg.character.user_profile = profile_path
        elif os.path.exists("profiles/default.json"):
            cfg.character.user_profile = "profiles/default.json"
        else:
            cfg.character.user_profile = ""
        cfg.tts.enabled = True
        # 本剧本明确检查这些功能，不能依赖本机 config 是否恰好开启；仅影响沙箱实例。
        cfg.character.self_memory = True
        cfg.character.fact_layers = True
        cfg.character.impressions = True
        cfg.character.meetup_recap = True
        cfg.admin.port = 0
        return cfg

    async def boot(self) -> None:
        from companion import main as M

        bot = M.CompanionBot(self.config(), db_path=self.db_path)
        bot.onebot = self.onebot
        install_fake_tts(bot, self.run_dir, self.tts_state)
        await bot.db.init_tables()
        if getattr(bot.config.reply, "runtime_state_v1", False) is True:
            await bot.runtime_events.init()
        await bot.stickers.sync_initial_stickers()
        original = bot.turn_handler.handle_turn

        async def wrapped(*a, **kw):
            t0 = time.monotonic()
            try:
                await original(*a, **kw)
            except Exception as e:  # 一轮炸了要记下来，但沙箱继续跑
                self.errors.append(f"{Clock.now:%m-%d %H:%M} handle_turn 异常: {e!r}")
                logging.getLogger("sandbox").exception("handle_turn 异常")
            finally:
                self.turn_seconds.append(time.monotonic() - t0)
                self.turn_done.set()

        bot.aggregator.turn_handler = wrapped  # 聚合器提交的批次走包装
        if hasattr(bot.aggregator, "_turn_handler"):
            bot.aggregator._turn_handler = wrapped
        bot.aggregator.start()
        self.bot = bot

    async def shutdown(self) -> None:
        bot = self.bot
        if bot is None:
            return
        try:
            bot.aggregator.stop()
        except Exception:
            pass
        await self.drain()
        close = getattr(bot.gateway, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:
                pass
        await bot.db.close()
        self.bot = None

    async def drain(self, timeout: float = 120) -> None:
        """等观察者结算、日记归档这些 create_task 出去的后台活干完。"""
        me = asyncio.current_task()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pending = [t for t in asyncio.all_tasks() if t is not me and not t.done()
                       and "Aggregator" not in repr(t.get_coro()) and "_consume" not in repr(t.get_coro())
                       and "_run_loop" not in repr(t.get_coro())]
            if not pending:
                return
            await asyncio.wait(pending, timeout=1.0)

    def collect_sent(self, tag: str = "") -> List[Turn]:
        out = []
        while self.sent:
            s = self.sent.pop(0)
            t = Turn(s.at, "她", s.content, s.kind, tag)
            out.append(t)
            self.turns.append(t)
        return out

    def history_lines(self) -> List[str]:
        lines = []
        for t in self.turns[-20:]:
            if t.who == "系统":
                continue
            mark = "（她发来一条语音，你听到了，内容是：）" if t.kind == "voice" else ""
            lines.append(f"{t.who}：{mark}{t.text}")
        return lines

    async def cost(self) -> float:
        row = await self.bot.db.fetchone("SELECT SUM(cost_estimate) AS c FROM llm_calls")
        return float(row["c"] or 0.0) if row else 0.0

    async def run(self) -> None:
        patch_clock()
        patch_sleep()
        day1 = _real_datetime(2026, 10, 24, 0, 0)
        Clock.now = day1.replace(hour=12)
        await self.boot()
        last_day = Clock.now.date()
        for beat in build_script(getattr(self.args, "image", None)):
            target = beat_time(beat, day1)
            if target.date() != last_day:
                # 跨天：凌晨维护（生活主线补足等），按生产 04:17 的钩子
                Clock.now = target.replace(hour=4, minute=17)
                try:
                    await self.bot._daily_arcs_topup()
                except Exception as e:
                    self.errors.append(f"凌晨维护异常: {e!r}")
                last_day = target.date()
            Clock.now = target
            spent = await self.cost()
            if spent > self.args.max_cost:
                self.turns.append(Turn(f"{Clock.now:%m-%d %H:%M}", "系统", f"费用 ¥{spent:.2f} 超过上限，停止"))
                break
            if beat.kind == "restart":
                await self.shutdown()
                self.restarts += 1
                self.turns.append(Turn(f"{Clock.now:%m-%d %H:%M}", "系统", "—— 重启：新起一个机器人，接同一个库 ——", tag=beat.tag))
                await self.boot()
                continue
            if beat.kind == "proactive":
                before = len(self.turns)
                try:
                    await self.bot.proactive.trigger_cycle()
                except Exception as e:
                    self.errors.append(f"{Clock.now:%m-%d %H:%M} 主动消息异常: {e!r}")
                await self.drain()
                got = self.collect_sent(beat.tag or "主动")
                if not got:
                    self.turns.append(Turn(f"{Clock.now:%m-%d %H:%M}", "系统", "（主动消息周期：她这次没发）", tag=beat.tag))
                for t in self.turns[before:]:
                    t.tag = t.tag or "主动"
                continue
            self.tts_state["fail_next_synth"] += beat.fail_synth
            self.onebot.fail_next_record += beat.fail_record
            lines = beat.say or await user_say(self.bot.gateway, self.history_lines(), beat.goal)
            for i, line in enumerate(lines):
                self.turns.append(Turn(f"{Clock.now:%m-%d %H:%M}", "他", line, "image" if (beat.image and i == len(lines) - 1) else "text", beat.tag))
            self.turn_done.clear()
            for i, line in enumerate(lines):
                image = beat.image if (beat.image and i == len(lines) - 1) else None
                self.onebot.next_message_id += 1
                await self.bot.aggregator.push_message(line, image, self.onebot.next_message_id)
            try:
                await asyncio.wait_for(self.turn_done.wait(), timeout=240)
            except asyncio.TimeoutError:
                self.errors.append(f"{Clock.now:%m-%d %H:%M} 等回复超时")
            await self.drain()
            Clock.now = Clock.now + timedelta(seconds=40)
            self.collect_sent(beat.tag)
        await self.finish()

    # ------------------------------------------------------------ 收尾：检查表 + 产物

    async def finish(self) -> None:
        bot = self.bot
        db = bot.db
        dump = {}
        for table in ("facts", "self_facts", "impressions", "meetups", "diary", "followups",
                      "suppressed_desires", "life_arcs"):
            try:
                rows = await db.fetchall(f"SELECT * FROM {table}")
                dump[table] = [dict(r) for r in rows]
            except Exception as e:
                dump[table] = [f"读取失败: {e}"]
        calls = [dict(r) for r in await db.fetchall(
            "SELECT purpose, COUNT(*) AS n, SUM(cost_estimate) AS cost FROM llm_calls GROUP BY purpose")]
        total = await self.cost()
        tts_state = dict(self.tts_state)
        tts_state["qq_rejected"] = getattr(self.onebot, "qq_rejected", 0)
        max_cost = getattr(self.args, "max_cost", None)
        checks = run_checks(self.turns, dump, tts_state, self.errors, self.turn_seconds, total, self.restarts, max_cost=max_cost)
        await self.shutdown()
        with open(os.path.join(self.run_dir, "raw.json"), "w", encoding="utf-8") as f:
            json.dump({"turns": [t.__dict__ for t in self.turns], "db": dump, "calls": calls,
                       "tts": tts_state, "errors": self.errors, "turn_seconds": self.turn_seconds,
                       "cost": total}, f, ensure_ascii=False, indent=1, default=str)
        with open(os.path.join(self.run_dir, "checks.json"), "w", encoding="utf-8") as f:
            json.dump(checks, f, ensure_ascii=False, indent=1)
        with open(os.path.join(self.run_dir, "transcript.md"), "w", encoding="utf-8") as f:
            f.write(render_transcript(self.turns, dump, checks, calls, total))
        print(json.dumps({k: v["result"] for k, v in checks["items"].items()}, ensure_ascii=False))
        print(f"费用 ¥{total:.3f}  产物：{self.run_dir}")


# ---------------------------------------------------------------- 检查表


def _face_name(face_id: Any) -> str:
    from companion.faces import face_tag_by_id

    return face_tag_by_id(face_id).strip("[]") or str(face_id)


# "没发出去/没传过去"是如实认账，不算假装发了
CLAIM_RE = re.compile(r"(?:我|刚)[^，。！？\n没]{0,6}(?<!没)(?:发|传)(?:了|过去|出去)|传过去了|听见了吧|听到了吧|听到了没|收到就好|再发一遍")
RESIDUE_RE = re.compile(r"\[/?voice|（语音消息）|【看到|\[quote|\[沉默\]|\[SILENT|[^\[]\]$")
FAIL_CLAIM_RE = re.compile(r"没发出去|没发成|发不出去|没传过去|没发过去")
CLAUSE_RE = re.compile(r"[，。！？!?,~～…\n]+")


def _group_beats(turns: List[Turn]) -> List[Dict[str, Any]]:
    """按连续的拍子把 turns 切分成拍（每拍包含他说的、她回的、系统消息）。"""
    beats: List[Dict[str, Any]] = []
    curr_beat: Optional[Dict[str, Any]] = None
    last_who: Optional[str] = None

    for t in turns:
        # 新的一拍通常从 "他" 或 "系统"（如主动/重启）开始，或者 tag 变更
        is_new_beat = False
        if curr_beat is None:
            is_new_beat = True
        elif t.who in ("他", "系统") and last_who == "她":
            is_new_beat = True
        elif t.tag and curr_beat.get("tag") and t.tag != curr_beat.get("tag"):
            is_new_beat = True
        elif t.who in ("他", "系统") and t.at != curr_beat.get("at") and (t.who == "他" and last_who == "他" and t.tag != curr_beat.get("tag")):
            is_new_beat = True

        if is_new_beat:
            curr_beat = {"tag": t.tag, "at": t.at, "turns": [t], "him": [], "her": []}
            beats.append(curr_beat)
        else:
            curr_beat["turns"].append(t)
            if not curr_beat["tag"] and t.tag:
                curr_beat["tag"] = t.tag

        if t.who == "他":
            curr_beat["him"].append(t)
        elif t.who == "她":
            curr_beat["her"].append(t)
        last_who = t.who

    return beats


def run_checks(turns: List[Turn], dump: Dict[str, Any], tts: Dict[str, Any], errors: List[str],
               secs: List[float], cost: float, restarts: int, max_cost: Optional[float] = None) -> Dict[str, Any]:
    items: Dict[str, Dict[str, Any]] = {}

    def put(name, ok, evidence, na=False):
        items[name] = {"result": "N/A" if na else ("PASS" if ok else "FAIL"), "evidence": evidence}

    hers = [t for t in turns if t.who == "她"]
    # 1. 假装发了语音：说"发了/听见了吧/收到就好"，但这条和前一条都没有真语音
    bad = []
    for i, t in enumerate(hers):
        if t.kind == "voice":
            continue
        if CLAIM_RE.search(t.text):
            window = hers[max(0, i - 2): i]
            if not any(w.kind == "voice" for w in window):
                bad.append(f"{t.at} {t.text}")
    put("没有假装发了语音", not bad, bad or "无")
    # 2. 标记残留
    residue = [f"{t.at} {t.text}" for t in hers if t.kind != "voice" and (
        RESIDUE_RE.search(t.text.strip()) or t.text.count("]") > t.text.count("["))]
    put("没有标记残留", not residue, residue or "无")
    # 3. 讨要语音真的发出了（按非故障讨要拍逐拍检查）
    beats = _group_beats(turns)
    normal_ask_beats = [b for b in beats if b.get("tag") == "讨要语音"]
    if normal_ask_beats:
        ask_failed_beats = []
        ask_evidence = []
        for b in normal_ask_beats:
            her_turns = b.get("her", [])
            voices = [t for t in her_turns if t.kind == "voice" and t.text.strip()]
            tag_at = f"{b.get('at')} [{b.get('tag')}]"
            if voices:
                ask_evidence.append(f"{tag_at} 成功: " + "；".join(f"{v.at} {v.text}" for v in voices))
            else:
                her_desc = "；".join(f"{t.kind}:{t.text}" for t in her_turns) or "无回复"
                ask_failed_beats.append(f"{tag_at} 缺失语音（回复: {her_desc}）")
        put("讨要语音时真发出了语音", not ask_failed_beats, ask_failed_beats or ask_evidence)
    else:
        # 兼容没有明确 "讨要语音" 标签打在每拍的情况
        ask_tags = {"讨要语音", "讨要语音-延续", "讨要语音-合成失败", "讨要语音-QQ拒绝", "没收到"}
        voices_after_ask = [t for t in hers if t.kind == "voice" and t.tag in ask_tags and t.text.strip()]
        put("讨要语音时真发出了语音", len(voices_after_ask) >= 1, [f"{t.at} {t.text}" for t in voices_after_ask] or "一条都没发出")
    # 4. 复读：任意连续 6 条她的消息里，同一小句出现 ≥3 次
    rep = []
    for i in range(len(hers)):
        window = hers[i:i + 6]
        cnt: Dict[str, int] = {}
        for w in window:
            for c in CLAUSE_RE.split(w.text):
                c = re.sub(r"\W", "", c)
                if len(c) >= 4:
                    cnt[c] = cnt.get(c, 0) + 1
        rep += [c for c, n in cnt.items() if n >= 3 and c not in rep]
    put("没有复读（6 条内同句 ≥3 次）", not rep, rep or "无")
    # 5. 道别拖尾：他回"好～/表情/嗯嗯"后，她回的字数
    tail = [t for t in hers if t.tag == "道别拖尾"]
    long_tail = [f"{t.at} {t.text}" for t in tail if len(t.text) > 40]
    put("道别短收（短应答后她每条 ≤40 字）", not long_tail, long_tail or [f"{t.at} {t.text}" for t in tail] or "她没回（也算短收）")
    # 6. 发图被看见
    seen = [t for t in turns if t.tag == "发图" and t.who == "她"]
    put("发图有回应", bool(seen), [f"{t.at} {t.text}" for t in seen] or "没回")
    # 7. 约见面被记下 + 补了纪要
    meets = dump.get("meetups", [])
    recapped = [m for m in meets if isinstance(m, dict) and m.get("recap")]
    put("见面约定被记下", bool(meets), [f"{m.get('when_at')} {m.get('place')} {m.get('plan')} status={m.get('status')}" for m in meets if isinstance(m, dict)] or "没记")
    story_end = Clock.now.strftime("%Y-%m-%d %H:%M")
    due = [m for m in meets if isinstance(m, dict) and str(m.get("when_at") or "") < story_end]
    put("见面后补了纪要", bool(recapped), [m.get("recap") for m in recapped] or "没补",
        na=bool(meets) and not due)
    # 8. 重启后还记得"学锅底"
    after = [t for t in hers if t.tag == "重启后记忆"]
    put("重启后记得他说想学做锅底", any("锅底" in t.text for t in after), [f"{t.at} {t.text}" for t in after] or "没回", na=restarts == 0)
    # 9. 报错
    put("全程无异常", not errors, errors or "无")
    lo = (RUN_STARTED_REAL - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
    hi = (_real_datetime.now() + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
    real_stamps = [m.get("created_at") for m in meets if isinstance(m, dict)
                   and lo <= str(m.get("created_at", ""))[:16] <= hi]
    put("沙箱时钟生效（库里时间是剧情时间）", not real_stamps, real_stamps or "是")
    # 9b. 没编"没发出去"：她说语音没发出去时，之前 10 分钟内得真有一次故障
    made_up = []
    for t in hers:
        if FAIL_CLAIM_RE.search(t.text):
            at = _real_datetime.strptime(f"{Clock.now.year}-{t.at}", "%Y-%m-%d %H:%M")
            if not any(timedelta(0) <= at - f <= timedelta(minutes=10) for f in VOICE_FAIL_AT):
                made_up.append(f"{t.at} {t.text}")
    put("没编造“语音没发出去”", not made_up, made_up or "无")
    # 10. 每轮耗时
    slow = [round(s, 1) for s in secs if s > 30]
    put("每轮处理 ≤30 秒（不含打字表演）", not slow,
        {"最慢": round(max(secs), 1) if secs else None, "中位": round(sorted(secs)[len(secs) // 2], 1) if secs else None, "超时": slow})
    # 11. 语音故障注入生效且产生文字降级（合成失败与 QQ 拒收分别验证）
    synth_failed_count = int(tts.get("synth_failed", 0))
    qq_rejected_count = int(tts.get("qq_rejected", 0))
    if qq_rejected_count == 0:
        # 兼容直接从 turns 统计 voice_failed kind 标记
        qq_rejected_count = len([t for t in hers if t.kind == "voice_failed"])

    fault_synth_beats = [b for b in beats if b.get("tag") == "讨要语音-合成失败"]
    fault_qq_beats = [b for b in beats if b.get("tag") == "讨要语音-QQ拒绝"]

    synth_degrade_ok = True
    synth_evidence = []
    if fault_synth_beats:
        for b in fault_synth_beats:
            texts = [t for t in b.get("her", []) if t.kind == "text" and t.text.strip()]
            voices = [t for t in b.get("her", []) if t.kind == "voice"]
            if not texts or voices:
                synth_degrade_ok = False
                synth_evidence.append(f"{b.get('at')} 未成功降级为文字(texts={len(texts)}, voices={len(voices)})")
            else:
                synth_evidence.append(f"{b.get('at')} 成功降级文字: " + "；".join(t.text for t in texts))

    qq_degrade_ok = True
    qq_evidence = []
    if fault_qq_beats:
        for b in fault_qq_beats:
            has_voice_failed_mark = any(t.kind == "voice_failed" for t in b.get("her", []))
            texts = [t for t in b.get("her", []) if t.kind == "text" and t.text.strip()]
            voices = [t for t in b.get("her", []) if t.kind == "voice"]
            # QQ拒收时，既要有失败走到的痕迹（或统计），又要有降级的文字发出来
            if not texts or voices:
                qq_degrade_ok = False
                qq_evidence.append(f"{b.get('at')} 未成功降级为文字(texts={len(texts)}, voices={len(voices)})")
            elif not has_voice_failed_mark and qq_rejected_count < 1:
                qq_degrade_ok = False
                qq_evidence.append(f"{b.get('at')} 缺少QQ拒绝痕迹")
            else:
                qq_evidence.append(f"{b.get('at')} 成功降级文字: " + "；".join(t.text for t in texts))

    fault_ok = (
        synth_failed_count >= 1
        and qq_rejected_count >= 1
        and synth_degrade_ok
        and qq_degrade_ok
    )
    fault_details = {
        "synth_failed": synth_failed_count,
        "qq_rejected": qq_rejected_count,
        "synth_degraded": synth_degrade_ok,
        "qq_degraded": qq_degrade_ok,
        "synth_evidence": synth_evidence or "无",
        "qq_evidence": qq_evidence or "无",
    }
    put("语音故障注入生效", fault_ok, fault_details)

    # 12. 费用 ≤ 上限（严格判断）
    items_cost = round(cost, 3)
    if max_cost is not None and max_cost > 0:
        cost_ok = (items_cost <= max_cost)
        cost_evidence = f"¥{items_cost} (上限 ¥{max_cost:.3f})"
    else:
        # 未传 max_cost 时，保持默认阈值或提示
        cost_ok = True
        cost_evidence = f"¥{items_cost} (未指定上限)"
    put("费用 ≤ 上限", cost_ok, cost_evidence)

    overall = "FAIL" if any(v["result"] == "FAIL" for v in items.values()) else "PASS"
    return {"overall": overall, "items": items}


def render_transcript(turns: List[Turn], dump: Dict[str, Any], checks: Dict[str, Any],
                      calls: List[Dict[str, Any]], cost: float) -> str:
    out = ["# 全流程沙箱 · 原文", "", f"总评：**{checks['overall']}**　费用 ¥{cost:.3f}", "", "## 检查表", ""]
    for name, v in checks["items"].items():
        ev = v["evidence"]
        ev = "；".join(map(str, ev)) if isinstance(ev, list) else ev
        out.append(f"- [{v['result']}] {name}：{ev}")
    out += ["", "## 对话", ""]
    last_tag = None
    for t in turns:
        if t.tag and t.tag != last_tag:
            out.append(f"\n> 【{t.tag}】")
        last_tag = t.tag or last_tag
        if t.who == "系统":
            out.append(f"\n*{t.at} {t.text}*\n")
            continue
        mark = {"voice": "🔊语音 ", "image": "🖼图 ", "sticker": "", "voice_failed": "⚠ "}.get(t.kind, "")
        out.append(f"- `{t.at}` **{t.who}**：{mark}{t.text}")
    out += ["", "## 她记下的东西", ""]
    for table in ("facts", "self_facts", "impressions", "meetups", "followups", "diary", "life_arcs"):
        out.append(f"### {table}")
        for r in dump.get(table, []):
            if isinstance(r, dict):
                body = r.get("content") or r.get("recap") or r.get("topic") or r.get("title") or r
                extra = {k: r[k] for k in ("when_at", "place", "plan", "status", "remind_after", "key_date") if k in r}
                out.append(f"- {body} {extra if extra else ''}")
            else:
                out.append(f"- {r}")
        out.append("")
    out += ["## 调用与费用", ""]
    for c in calls:
        out.append(f"- {c['purpose']}: {c['n']} 次，¥{float(c['cost'] or 0):.4f}")
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--card", default="characters/example", help="角色卡目录（默认 characters/example）")
    ap.add_argument("--profile", default=None, help="用户 profile 文件路径（可选）")
    ap.add_argument("--image", default=None, help="发图环节使用的图片路径（可选）")
    ap.add_argument("--max-cost", type=float, default=3.0)
    ap.add_argument("--run-id", default=_real_datetime.now().strftime("run-%Y%m%d-%H%M%S"))
    args = ap.parse_args()
    sb = Sandbox(args)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.FileHandler(os.path.join(sb.run_dir, "bot.log"), encoding="utf-8")],
    )
    asyncio.run(sb.run())


if __name__ == "__main__":
    main()
