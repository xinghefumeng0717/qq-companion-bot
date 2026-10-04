"""FIXES20 QQ 系统表情双向真实 API 冒烟 (scripts/smoke/smoke_fixes20.py)

单测全是手造文本，**证明不了三件事**，所以分三段用真实 API 验：

A 段（收侧，最要紧）：`onebot._process_incoming_message` 到底有没有把 face 段翻译成
   标签送进提示词。单测能证明"我拼出了 '你真棒[旺柴]'"，证明不了"模型真收到了这句"。
   这里把带 face 段的 OneBot 消息**从进站事件灌进真实 OneBotClient**，经聚合器、
   TurnHandler 到真实 LLM，把 spy 到的实际提示词与她的真实回复原样贴出。
   重点看：她读不读得出"你真棒[旺柴]"是损不是夸（语气判断交所有者抽读，脚本不判）。

B 段（发侧）：真跑一局对聊仿真，统计她 face 的使用次数与三种发法分布。
   **零使用报 N/A 不报 PASS**（0 违规 == 合规 是空断言，报告读起来像"验过了"是假的）。

C 段（发送结构）：她输出 -> parse_reply -> `main._send_chunk_to_onebot` -> OneBot 报文，
   逐字贴出 send_msg 的 message 段数组，验证混排是**一条**消息而不是两条。

零服务器副作用、零生产库读写（沙箱副本 + 本地 config.toml 的真实 key）。
报告落 data/smoke_fixes20_report.json。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict, List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from companion.aggregator import MessageAggregator
from companion.chat import ChatSession
from companion.config import OneBotConfig
from companion.db import TIME_FORMAT
from companion.onebot import OneBotClient
from companion.replier import Replier, chunk_text_weight
from companion.turn_handler import TurnHandler

REPORT_FILE = "data/smoke_fixes20_report.json"
SANDBOX_DB = "data/smoke_fixes20_sandbox.db"
REAL_DB = "data/companion.db"
FALLBACK_DB = "data/companion_example.db"
DUO_SIM = os.path.join("scripts", "duo_sim.py")

# 收侧三条用例：狗头（损）、捂脸（尴尬）、纯脸无字（过去会被整条吞掉）
INBOUND_CASES: List[Dict[str, Any]] = [
    {
        "name": "文字+狗头（最典型的损）",
        "segments": [
            {"type": "text", "data": {"text": "你真棒"}},
            {"type": "face", "data": {"id": 179}},
        ],
        "expect_text": "你真棒[doge]",
    },
    {
        "name": "文字+捂脸（自嘲式尴尬）",
        "segments": [
            {"type": "text", "data": {"text": "我又把ddl拖到明天了"}},
            {"type": "face", "data": {"id": 264}},
        ],
        "expect_text": "我又把ddl拖到明天了[捂脸]",
    },
    {
        "name": "纯脸无字（过去整条消息被吞掉）",
        "segments": [{"type": "face", "data": {"id": 34}}],
        "expect_text": "[晕]",
    },
]


class _EchoWS:
    """能自动回 echo 的 WebSocket 替身：让**真实的** OneBotClient.send_private_msg 跑通。

    C 段刻意不用"自己拼一个 send_msg 字典"的假记录器：那样验的是我手写的报文，
    不是生产真正发出去的那份。这里接的是 companion/onebot.py 里那套真发送逻辑
    （含 echo 等待与重试），贴出来的 payload 就是线上原文。
    """

    def __init__(self) -> None:
        self._inbound: asyncio.Queue = asyncio.Queue()
        self.sent: List[Dict[str, Any]] = []
        self.closed = False

    def feed(self, frame) -> None:
        self._inbound.put_nowait(json.dumps(frame))

    async def receive(self):
        return await self._inbound.get()

    async def close(self) -> None:
        self.closed = True

    async def send_str(self, payload: str) -> None:
        data = json.loads(payload)
        self.sent.append(data)
        self.feed({
            "echo": data.get("echo"),
            "status": "ok",
            "retcode": 0,
            "data": {"message_id": 1},
        })


async def _ws_read_loop(client, ws) -> None:
    while True:
        frame = await ws.receive()
        await client._handle_raw_message(frame)


class _FakeWS:
    """最小 WebSocket 替身：只够 OneBotClient 收进站消息（不发 action）"""

    def __init__(self) -> None:
        import asyncio as _a

        self._inbound: _a.Queue = _a.Queue()
        self.closed = False
        self.sent: List[Dict[str, Any]] = []

    def feed(self, frame) -> None:
        self._inbound.put_nowait(json.dumps(frame))

    async def receive(self):
        return await self._inbound.get()

    async def close(self) -> None:
        self.closed = True

    async def send_str(self, payload: str) -> None:
        data = json.loads(payload)
        if data.get("action") == "send_msg":
            self.sent.append(data)


def _msg_event(segments: List[Dict[str, Any]], user_id: int = 10001) -> Dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "private",
        "user_id": user_id,
        "message": segments,
    }


async def _wait_for(predicate, timeout=60.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


# ==========================================
# A 段：收侧（mock 进站 -> 真实管道 -> 真实 LLM）
# ==========================================


async def section_a() -> Dict[str, Any]:
    prod_db = REAL_DB if os.path.exists(REAL_DB) else FALLBACK_DB
    print(f"  基准库：{prod_db}（复制到沙箱，生产库零写入）")

    session = ChatSession(prod_db_path=prod_db, sandbox_db_path=SANDBOX_DB)
    await session.initialize()

    # spy 住真实网关：把**实际送进模型的消息**与她的原始输出留证。
    # 没有这层，A 段只能证明"提示词里有这句"，证明不了"模型真收到了这句"。
    llm_log: List[Dict[str, Any]] = []
    _real_stream = session.gateway.stream_chat
    _real_chat = session.gateway.chat

    async def spy_stream(*args, **kwargs):
        msgs = kwargs.get("messages") or []
        llm_log.append({
            "purpose": kwargs.get("purpose"),
            "last_user_message": (msgs[-1].get("content") if msgs else ""),
        })
        async for piece in _real_stream(*args, **kwargs):
            yield piece

    async def spy_chat(*args, **kwargs):
        resp = await _real_chat(*args, **kwargs)
        llm_log.append({"purpose": kwargs.get("purpose"), "response": str(resp)[:400]})
        return resp

    session.gateway.stream_chat = spy_stream
    session.gateway.chat = spy_chat

    # 段间延迟与聚合窗口都调 0：冒烟不该干等，节奏不是被测对象
    replier = Replier(
        session.config.reply.__class__(chunk_delay_min=0.0, chunk_delay_max=0.0),
        session.stickers,
    )
    sent_chunks: List[Dict[str, Any]] = []

    async def collect(chunk: Dict[str, Any]) -> None:
        sent_chunks.append(chunk)

    async def set_typing(typing: bool) -> bool:
        return True

    from companion.config import ProactiveConfig
    from companion.proactive import ProactiveScheduler

    proactive = ProactiveScheduler(
        config=ProactiveConfig(enabled=False, quiet_hours=[]),
        persona=session.persona,
        affection=session.affection,
        mood=session.mood,
        memory=session.memory,
        stickers=session.stickers,
        replier=replier,
        gateway=session.gateway,
        db=session.db,
        send_msg_fn=collect,
        assembler=session.assembler,
        holidays_provider=session.config.get_holidays,
        set_typing_fn=set_typing,
    )

    turn_handler = TurnHandler(
        config=session.config,
        gateway=session.gateway,
        assembler=session.assembler,
        replier=replier,
        memory=session.memory,
        observer=session.observer,
        # TurnHandler 每轮都会调 proactive.reset_unanswered_count()，
        # 传 None 会直接 AttributeError——这里给真实例（只调计数，不 start 定时器）
        proactive=proactive,
        send_chunk_fn=collect,
        set_typing_fn=set_typing,
        timing_config=None,   # 沙箱不演 typing，零外呼
    )

    # 轮次完成信号：**不能靠"收到第一个气泡"判断一轮结束**——
    # 一轮会连发好几个气泡，第一个到的时候后面几个还在路上，
    # 下一条用例的消息会跟上一轮的尾巴缠在一起（第一版就踩了这个坑，
    # 报出来的"她的回复"其实是上一轮的后续气泡）。
    turns_done = {"n": 0}

    async def handle_turn(user_text, image_path):
        try:
            return await turn_handler.handle_turn(user_text, image_path)
        finally:
            turns_done["n"] += 1

    aggregator = MessageAggregator(turn_handler=handle_turn)
    aggregator.start()
    # 聚合静默窗从 6 秒压到 0：只影响"多久算一轮"，不影响这一轮的内容与走向
    import companion.aggregator as agg_mod

    orig_window = agg_mod.SILENCE_WINDOW
    orig_hard = agg_mod.HARD_LIMIT
    agg_mod.SILENCE_WINDOW = 0.05
    agg_mod.HARD_LIMIT = 1.0

    received: List[Dict[str, Any]] = []
    cases: List[Dict[str, Any]] = []

    try:
        client = OneBotClient(
            config=OneBotConfig(ws_url="ws://127.0.0.1:3001", access_token=""),
            allowed_user_id=10001,
            on_message_callback=aggregator.push_message,
            image_save_dir="data/smoke_fixes20_imgs",
        )
        client._ws = _FakeWS()
        client._running = True

        for case in INBOUND_CASES:
            sent_chunks.clear()
            llm_log.clear()
            done_before = turns_done["n"]
            # 真实进站路径：消息事件 -> _handle_raw_message -> 段解析 -> 聚合器
            await client._handle_raw_message(json.dumps(_msg_event(case["segments"])))
            ok = await _wait_for(
                lambda: turns_done["n"] > done_before, timeout=120.0
            )
            await asyncio.sleep(0.3)  # 等最后一个气泡进 collect
            her_text = "\n".join(
                c.get("content", "") for c in sent_chunks if c.get("type") == "text"
            )
            her_faces = [
                p.get("tag") for c in sent_chunks
                for p in ([c] if c.get("type") == "face" else c.get("parts", []))
                if p.get("type") == "face"
            ]
            main_chat_logs = [x for x in llm_log if x.get("purpose") == "main_chat"]
            case_result = {
                "name": case["name"],
                "raw_segments": case["segments"],
                "expect_text": case["expect_text"],
                "her_sent_chunks": [
                    {k: v for k, v in c.items() if not k.startswith("_")} for c in sent_chunks
                ],
                "her_reply_text": her_text,
                "her_reply_faces": her_faces,
                "main_chat_calls": len(main_chat_logs),
                # 主聊请求里**确实带着这条标签**才算链路真的跑到模型面前。
                # 只看"提示词里有这句"不算——那证明不了模型收到的是这一轮。
                "tag_reached_llm": any(
                    case["expect_text"] in str(x.get("last_user_message") or "")
                    for x in main_chat_logs
                ),
                "saw_user_message": (
                    main_chat_logs[-1].get("last_user_message")
                    if main_chat_logs else None
                ),
                "replied": ok,
            }
            cases.append(case_result)
            received.append(case_result)

            print(f"  · {case['name']}")
            print(f"      进站段: {json.dumps(case['segments'], ensure_ascii=False)}")
            print(f"      她收到的: {case['expect_text']!r}")
            print(f"      她的回复: {her_text!r}" + (f" + 脸{her_faces}" if her_faces else ""))
            print(f"      标签送达模型: {case_result['tag_reached_llm']}")

        await client.stop()
    finally:
        agg_mod.SILENCE_WINDOW = orig_window
        agg_mod.HARD_LIMIT = orig_hard
        aggregator.stop()
        await session.close()

    return {
        "ok": all(c["tag_reached_llm"] and c["replied"] for c in cases),
        "note": "语气对不对（她读没读出'是损不是夸'）交所有者抽读，脚本不判",
        "cases": cases,
    }


# ==========================================
# B 段：发侧（真跑一局对聊仿真）
# ==========================================


async def section_b(scene: str = "S1") -> Dict[str, Any]:
    run_id = f"{scene}-fixes20-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    cmd = [
        sys.executable, "-u", DUO_SIM,
        "--scene", scene, "--turns", "15", "--run-id", run_id,
        "--max-cost", "0.5",
    ]
    print(f"  起仿真：{' '.join(cmd)}", flush=True)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")),
    )
    out, _ = await proc.communicate()
    log_text = out.decode("utf-8", errors="replace")

    run_dir = os.path.join("data", "duo_sim", run_id)
    metrics_path = os.path.join(run_dir, "metrics.json")
    transcript_path = os.path.join(run_dir, "transcript.md")
    if not os.path.exists(metrics_path):
        return {
            "ok": False,
            "run_id": run_id,
            "error": f"仿真没产出 metrics.json（returncode={proc.returncode}）",
            "log_tail": log_text.splitlines()[-20:],
        }

    with open(metrics_path, "r", encoding="utf-8") as f:
        metrics = json.load(f)
    face_metric = (metrics.get("multi_turn") or {}).get("QQ表情") or {}

    # 从 raw.json 把用到脸的气泡原文挑出来（语境判断交所有者，脚本只留证据）
    fragments: List[Dict[str, Any]] = []
    raw_path = os.path.join(run_dir, "raw.json")
    if os.path.exists(raw_path):
        with open(raw_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for t in raw.get("turns", []):
            if t.get("speaker") != "her":
                continue
            faces = t.get("faces") or []
            if faces:
                fragments.append({
                    "idx": t.get("idx"),
                    "time": t.get("time"),
                    "he_said": (t.get("snapshot") or {}).get("user_text", ""),
                    "bubbles": t.get("bubbles") or [],
                    "faces": faces,
                })

    print(f"  她发脸 {face_metric.get('count', 0)} 次 / {face_metric.get('unique', 0)} 个不同标签")
    print(f"  三种发法: {json.dumps(face_metric.get('forms', {}), ensure_ascii=False)}")
    print(f"  判定: {face_metric.get('verdict')}（{face_metric.get('not_run_reason') or '有观测样本'}）")
    for fr in fragments:
        print(f"    第{fr['idx']}轮 他说{fr['he_said']!r} -> 她发 {fr['bubbles']}")
    print(f"  成本 ¥{metrics.get('cost_cny')}")

    return {
        "ok": face_metric.get("verdict") in ("PASS", "WARN"),
        "run_id": run_id,
        "verdict": face_metric.get("verdict"),
        "not_run_reason": face_metric.get("not_run_reason"),
        "metric": face_metric,
        "fragments": fragments,
        "turns": metrics.get("turns"),
        "cost_cny": metrics.get("cost_cny"),
        "transcript": transcript_path,
        "overall": metrics.get("overall"),
    }


# ==========================================
# C 段：发送结构（真实发送管道 -> mock OneBot 报文）
# ==========================================


async def section_c() -> Dict[str, Any]:
    """她输出 -> parse_reply -> `main._send_chunk_to_onebot` -> **真实** OneBotClient 发送。

    不用"手拼一个 send_msg 字典"的假记录器：那样验的是我自己写的报文。
    这里把 OneBot 端换成会自动回 echo 的假 WebSocket，于是 companion/onebot.py
    里那套真发送逻辑（含 echo 等待、超时降级）真的跑一遍，
    贴出来的 payload 就是线上原文。
    """
    from companion.main import CompanionBot

    real_client = OneBotClient(
        config=OneBotConfig(ws_url="ws://127.0.0.1:3001", access_token=""),
        allowed_user_id=10001,
        on_message_callback=None,
        image_save_dir="data/smoke_fixes20_imgs",
    )
    ws = _EchoWS()
    real_client._ws = ws
    reader = asyncio.create_task(_ws_read_loop(real_client, ws))

    class _Stub(CompanionBot):
        def __init__(self):  # 故意不调 super()：只借发送那一段
            self.config = type(
                "C", (), {"account": type("A", (), {"allowed_user_id": 10001})()}
            )()
            self.onebot = real_client

    bot = _Stub()
    replier = Replier(
        type("RC", (), {"max_chunks": 5, "chunk_delay_min": 0.0, "chunk_delay_max": 0.0})(),
        type("S", (), {"match_sticker": lambda self, d: f"/fake/{d}.png"})(),
    )

    cases: List[Dict[str, Any]] = []
    try:
        for name, raw in (
            ("混排句尾（主形态）", "你真棒[face:doge]"),
            ("纯表情气泡", "[face:吃瓜]"),
            ("同款二连（一条消息两个脸）", "[face:流泪][face:流泪]"),
            ("脸 + 表情包同轮", "在呢[sticker:猫猫][face:贴贴]"),
            ("清单外降级成文字", "你真棒[face:微笑]"),
        ):
            chunks, record = replier.parse_reply(raw)
            ws.sent.clear()
            for chunk in chunks:
                await bot._send_chunk_to_onebot(chunk)
            payloads = [{k: v for k, v in s.items() if k != "echo"} for s in ws.sent]
            case = {
                "name": name,
                "her_raw_output": raw,
                "record": record,
                "chunks": [{k: v for k, v in c.items() if not k.startswith("_")} for c in chunks],
                "onebot_payloads": payloads,
                "bubble_count": len(payloads),
            }
            cases.append(case)
            print(f"  · {name}")
            print(f"      她的原始输出: {raw!r}")
            print(f"      落库记录: {record!r}")
            for p in payloads:
                print(f"      → send_msg message: {json.dumps(p['params']['message'], ensure_ascii=False)}")
    finally:
        reader.cancel()
        await asyncio.wait({reader}, timeout=2.0)
        await real_client.stop()

    # 断言：混排与二连必须各自只发一条消息
    by_name = {c["name"]: c for c in cases}
    assertions = {
        "混排是一条消息": by_name["混排句尾（主形态）"]["bubble_count"] == 1,
        "混排那条同时含text段与face段": (
            {s["type"] for s in by_name["混排句尾（主形态）"]["onebot_payloads"][0]["params"]["message"]}
            == {"text", "face"}
        ),
        "二连是一条消息两个face段": (
            by_name["同款二连（一条消息两个脸）"]["bubble_count"] == 1
            and len(by_name["同款二连（一条消息两个脸）"]["onebot_payloads"][0]["params"]["message"]) == 2
        ),
        "纯脸气泡只有face段": (
            by_name["纯表情气泡"]["onebot_payloads"][0]["params"]["message"][0]["type"] == "face"
        ),
        "face段带的是NapCat的id不是名字": (
            by_name["纯表情气泡"]["onebot_payloads"][0]["params"]["message"][0]["data"] == {"id": 271}
        ),
        "降级那条是纯文字消息": all(
            s["type"] == "text" for s in by_name["清单外降级成文字"]["onebot_payloads"][0]["params"]["message"]
        ),
        "脸不与表情包合并": by_name["脸 + 表情包同轮"]["bubble_count"] == 3,
    }
    for k, v in assertions.items():
        print(f"      {'✓' if v else '✗'} {k}")

    return {"ok": all(assertions.values()), "assertions": assertions, "cases": cases}


# ==========================================


async def main() -> int:
    logging.getLogger("companion.onebot").setLevel(logging.WARNING)
    import argparse

    ap = argparse.ArgumentParser(description="FIXES20 冒烟（A 收侧 / B 发侧仿真 / C 发送结构）")
    ap.add_argument("--scene", default="S1", help="B 段用哪张剧情卡（S1 喊累/S2 分享开心事/S3 敷衍已读不回）")
    ap.add_argument("--skip-b", action="store_true", help="只跑 A/C（省 API 钱的重跑方式）")
    ap.add_argument(
        "--only-b",
        action="store_true",
        help="只重跑 B 段并把它并回上一份报告（A/C 证据沿用上次那轮，省 API 钱）",
    )
    args = ap.parse_args()

    if args.only_b:
        prev: Dict[str, Any] = {}
        if os.path.exists(REPORT_FILE):
            with open(REPORT_FILE, "r", encoding="utf-8") as f:
                prev = json.load(f)
        print("=" * 70)
        print(f"FIXES20 冒烟 · B 段：发侧（只重跑 B，场景 {args.scene}）")
        print("=" * 70, flush=True)
        b = await section_b(args.scene)
        report = {
            "generated_at": datetime.now().strftime(TIME_FORMAT),
            "scene": args.scene,
            "A_receive": prev.get("A_receive", {}),
            "B_send": b,
            "C_wire": prev.get("C_wire", {}),
            "A_C_evidence_from": prev.get("generated_at", "(缺失)"),
            "pass": bool(
                prev.get("A_receive", {}).get("ok")
                and prev.get("C_wire", {}).get("ok")
                and b["ok"]
            ),
        }
        with open(REPORT_FILE, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print()
        print(f"B 段（发侧 face 使用）：{b.get('verdict', b.get('error'))}")
        print(f"报告：{REPORT_FILE}（A/C 证据沿用 {report['A_C_evidence_from']}）")
        return 0 if report["pass"] else 1

    print("=" * 70)
    print("FIXES20 冒烟 · A 段：收侧（mock 进站 → 真实管道 → 真实 LLM）")
    print("=" * 70, flush=True)
    a = await section_a()

    print()
    print("=" * 70)
    print("FIXES20 冒烟 · C 段：发送结构（真实发送管道 → mock OneBot）")
    print("=" * 70, flush=True)
    c = await section_c()

    if args.skip_b:
        b = {"ok": True, "skipped": True, "verdict": "SKIPPED（--skip-b）"}
        print()
        print("B 段已按 --skip-b 跳过")
    else:
        print()
        print("=" * 70)
        print(f"FIXES20 冒烟 · B 段：发侧（真跑一局 {args.scene} 对聊仿真，¥0.5 以内）")
        print("=" * 70, flush=True)
        b = await section_b(args.scene)

    report = {
        "generated_at": datetime.now().strftime(TIME_FORMAT),
        "scene": args.scene,
        "A_receive": a,
        "B_send": b,
        "C_wire": c,
        "pass": bool(a["ok"] and c["ok"] and b["ok"]),
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print()
    print("=" * 70)
    print(f"A 段（收侧送达模型）：{'PASS' if a['ok'] else 'FAIL'}")
    print(f"B 段（发侧 face 使用）：{b.get('verdict', b.get('error'))}"
          f"{'（未跑：' + b['not_run_reason'] + '）' if b.get('not_run_reason') else ''}")
    print(f"C 段（OneBot 报文结构）：{'PASS' if c['ok'] else 'FAIL'}")
    print(f"报告：{REPORT_FILE}")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
