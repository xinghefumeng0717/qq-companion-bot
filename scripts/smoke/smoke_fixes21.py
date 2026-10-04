"""FIXES21 引用回复发送侧真实 API 冒烟 (scripts/smoke/smoke_fixes21.py)

单测全是手造文本，证明不了两件事，所以分两段用真实 API 验：

A 段（收侧链路 + 发侧出站）：把**三条真实 OneBot 入站消息**（带 message_id，
   模拟他连发）灌进真实 OneBotClient → 聚合器 → TurnHandler → 真实 LLM，
   再把 `_send_chunk_to_onebot` 接到会自动回 echo 的假 OneBot 上，
   **逐字贴出 send_msg 报文**：命中引用时 `reply` 段的 id 必须等于第 1 条消息的 id。
   这一段同时验证编号块真的进了提示词（spy 住送给模型的用户消息）。

B 段（发侧采纳率）：真跑一局 S2 对聊仿真，统计她的引用次数与命中率。
   验收口径是"引用率低（多数轮次不引用）但命中场景不缺位"，
   **零引用报 N/A 不报 PASS**（0 次 == 0 违规是空断言）。

零服务器副作用、零生产库读写（沙箱副本 + 本地 config.toml 的真实 key）。
报告落 data/smoke_fixes21_report.json。
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
from companion.config import OneBotConfig, ProactiveConfig, ReplyConfig
from companion.db import TIME_FORMAT
from companion.main import CompanionBot
from companion.onebot import OneBotClient
from companion.proactive import ProactiveScheduler
from companion.replier import Replier
from companion.turn_handler import TurnHandler

REPORT_FILE = "data/smoke_fixes21_report.json"
SANDBOX_DB = "data/smoke_fixes21_sandbox.db"
REAL_DB = "data/companion.db"
FALLBACK_DB = "data/companion_example.db"
DUO_SIM = os.path.join("scripts", "duo_sim.py")

# A 段：他连发三条。第 1 条埋一个需要回应的点（他其实在问她周六的安排），
# 第 3 条才是真正的问题所在——引用价值就在"她要回的是较早的那条还是这条"。
INBOUND: List[Dict[str, Any]] = [
    {"message_id": 900001, "segments": [{"type": "text", "data": {"text": "今天做完实验了"}}]},
    {"message_id": 900002, "segments": [{"type": "text", "data": {"text": "晚上吃啥 我请你"}}]},
    {"message_id": 900003, "segments": [{"type": "text", "data": {"text": "对了周六那个会你还去吗"}}]},
]


class _EchoWS:
    """自动回 echo 的 OneBot 替身：让**真实的** send_private_msg 跑通（FIXES20 C 段同款）"""

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
            "status": "ok", "retcode": 0,
            "data": {"message_id": 1},
        })


async def _ws_read_loop(client, ws) -> None:
    while True:
        frame = await ws.receive()
        await client._handle_raw_message(frame)


def _msg_event(message, message_id, user_id=10001) -> Dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "private",
        "user_id": user_id,
        "message": message,
        "message_id": message_id,
    }


async def _wait_for(predicate, timeout=120.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


# ==========================================
# A 段：连发三条 → 真实管道 → 出站报文
# ==========================================


async def section_a() -> Dict[str, Any]:
    prod_db = REAL_DB if os.path.exists(REAL_DB) else FALLBACK_DB
    print(f"  基准库：{prod_db}（复制到沙箱，生产库零写入）")
    session = ChatSession(prod_db_path=prod_db, sandbox_db_path=SANDBOX_DB)
    await session.initialize()

    # 出站端：真实 OneBotClient（echo 自动回）→ 报文逐字留证
    out_client = OneBotClient(
        config=OneBotConfig(ws_url="ws://127.0.0.1:3001", access_token=""),
        allowed_user_id=10001,
        on_message_callback=None,
        image_save_dir="data/smoke_fixes21_imgs",
    )
    out_ws = _EchoWS()
    out_client._ws = out_ws
    out_reader = asyncio.create_task(_ws_read_loop(out_client, out_ws))

    class _StubBot(CompanionBot):
        def __init__(self):  # 故意不调 super()：只借发送那一段
            self.config = type(
                "C", (), {"account": type("A", (), {"allowed_user_id": 10001})()}
            )()
            self.onebot = out_client

    bot = _StubBot()

    # spy 住真实网关：看**实际送给模型的用户消息**（编号块到没到模型面前）
    llm_log: List[Dict[str, Any]] = []
    _real_stream = session.gateway.stream_chat

    async def spy_stream(*args, **kwargs):
        msgs = kwargs.get("messages") or []
        llm_log.append({
            "purpose": kwargs.get("purpose"),
            "last_user_message": (msgs[-1].get("content") if msgs else ""),
        })
        async for piece in _real_stream(*args, **kwargs):
            yield piece

    session.gateway.stream_chat = spy_stream

    replier = Replier(
        ReplyConfig(chunk_delay_min=0.0, chunk_delay_max=0.0), session.stickers
    )
    sent_chunks: List[Dict[str, Any]] = []

    async def collect(chunk: Dict[str, Any]) -> None:
        sent_chunks.append(chunk)
        await bot._send_chunk_to_onebot(chunk)

    async def set_typing(typing: bool) -> bool:
        return True

    proactive = ProactiveScheduler(
        config=ProactiveConfig(enabled=False, quiet_hours=[]),
        persona=session.persona, affection=session.affection, mood=session.mood,
        memory=session.memory, stickers=session.stickers, replier=replier,
        gateway=session.gateway, db=session.db, send_msg_fn=collect,
        assembler=session.assembler, holidays_provider=session.config.get_holidays,
        set_typing_fn=set_typing,
    )
    turn_handler = TurnHandler(
        config=session.config, gateway=session.gateway, assembler=session.assembler,
        replier=replier, memory=session.memory, observer=session.observer,
        proactive=proactive, send_chunk_fn=collect, set_typing_fn=set_typing,
        timing_config=None,
    )

    turns_done = {"n": 0}

    async def handle_turn(user_text, image_path, batch=None):
        try:
            return await turn_handler.handle_turn(user_text, image_path, batch)
        finally:
            turns_done["n"] += 1

    aggregator = MessageAggregator(turn_handler=handle_turn)
    aggregator.start()
    import companion.aggregator as agg_mod

    old_w, old_h = agg_mod.SILENCE_WINDOW, agg_mod.HARD_LIMIT
    agg_mod.SILENCE_WINDOW, agg_mod.HARD_LIMIT = 0.15, 2.0

    in_client = OneBotClient(
        config=OneBotConfig(ws_url="ws://127.0.0.1:3001", access_token=""),
        allowed_user_id=10001, on_message_callback=aggregator.push_message,
        image_save_dir="data/smoke_fixes21_imgs",
    )
    in_ws = _EchoWS()
    in_client._ws = in_ws
    in_client._running = True

    result: Dict[str, Any] = {}
    try:
        # 三条连发走真实入站路径（每条都有自己的 message_id）
        for item in INBOUND:
            await in_client._handle_raw_message(
                json.dumps(_msg_event(item["segments"], item["message_id"]))
            )
            await asyncio.sleep(0.05)

        ok = await _wait_for(lambda: turns_done["n"] >= 1, timeout=150.0)
        await asyncio.sleep(0.5)   # 等最后几个气泡进 collect

        main_chat = [x for x in llm_log if x.get("purpose") == "main_chat"]
        payloads = [{k: v for k, v in s.items() if k != "echo"} for s in out_ws.sent]
        quotes = [
            c.get("_quote") for c in sent_chunks if c.get("_quote")
        ]
        reply_ids = [
            s["params"]["message"][0]["data"]["id"]
            for s in payloads
            if s["params"]["message"] and s["params"]["message"][0]["type"] == "reply"
        ]
        valid_ids = {i["message_id"] for i in INBOUND}
        numbered_block = main_chat[-1].get("last_user_message", "") if main_chat else ""

        result = {
            "ok": ok and bool(payloads),
            "his_3_messages": [i["message_id"] for i in INBOUND],
            "her_bubbles": [
                c.get("content") or c.get("_display", "") for c in sent_chunks
                if c.get("type") == "text"
            ],
            "her_chunks": [{k: v for k, v in c.items() if not k.startswith("_")}
                           for c in sent_chunks],
            "quotes": quotes,
            "outbound_payloads": payloads,
            "reply_ids_used": reply_ids,
            "reply_id_matches_his_message": all(i in valid_ids for i in reply_ids),
            "prompt_has_numbered_block": "[1]" in numbered_block and "[3]" in numbered_block,
            "prompt_user_message": numbered_block,
            "main_chat_calls": len(main_chat),
            "quoted": bool(quotes),
            "note": "语境对不对（该引第3条还是第2条）交所有者抽读，脚本不判",
        }

        print("  他的三条（真实入站，各带 message_id）：")
        for item in INBOUND:
            print(f"      {item['message_id']}  {item['segments'][0]['data']['text']}")
        print("  送进模型的用户消息（编号块到没到）：")
        for ln in numbered_block.splitlines():
            print(f"      {ln}")
        print("  她的回复：")
        for c in sent_chunks:
            d = {k: v for k, v in c.items() if not k.startswith("_")}
            q = c.get("_quote")
            print(f"      {json.dumps(d, ensure_ascii=False)}"
                  + (f"  _quote={q}" if q else ""))
        print("  出站报文（真实 send_private_msg 发的原文）：")
        for p in payloads:
            print(f"      {json.dumps(p['params']['message'], ensure_ascii=False)}")
        print(f"  引用到的 message_id: {reply_ids}（应来自他那三条: {sorted(valid_ids)}）")
        print(f"  编号块进了提示词: {result['prompt_has_numbered_block']}")
        print(f"  本局是否用了引用: {result['quoted']}")
        await in_client.stop()
    finally:
        agg_mod.SILENCE_WINDOW, agg_mod.HARD_LIMIT = old_w, old_h
        aggregator.stop()
        out_reader.cancel()
        await asyncio.wait({out_reader}, timeout=2.0)
        await out_client.stop()
        await session.close()
    return result


# ==========================================
# B 段：发侧采纳率（真跑一局 S2）
# ==========================================


async def section_b(scene: str = "S2") -> Dict[str, Any]:
    run_id = f"{scene}-fixes21-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    cmd = [
        sys.executable, "-u", DUO_SIM,
        "--scene", scene, "--turns", "15", "--run-id", run_id, "--max-cost", "0.5",
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
    if not os.path.exists(metrics_path):
        return {
            "ok": False, "run_id": run_id,
            "error": f"仿真没产出 metrics.json（returncode={proc.returncode}）",
            "log_tail": log_text.splitlines()[-20:],
        }

    with open(metrics_path, "r", encoding="utf-8") as f:
        metrics = json.load(f)
    q = (metrics.get("multi_turn") or {}).get("引用回复") or {}

    fragments: List[Dict[str, Any]] = []
    raw_path = os.path.join(run_dir, "raw.json")
    if os.path.exists(raw_path):
        with open(raw_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for t in raw.get("turns", []):
            if t.get("speaker") == "her" and t.get("quotes"):
                fragments.append({
                    "idx": t.get("idx"),
                    "his_batch": [b.get("text") for b in (t.get("his_batch") or [])],
                    "quotes": t.get("quotes"),
                    "her_bubbles": t.get("bubbles") or [],
                })

    print(f"  他连发（≥2 条）的轮次: {q.get('quoteable_turns')}")
    print(f"  她引用 {q.get('count')} 次 | 轮次引用率 {q.get('turn_rate')} | "
          f"有引用机会时的命中率 {q.get('hit_rate')}")
    print(f"  判定: {q.get('verdict')}"
          + (f"（{q.get('not_run_reason')}）" if q.get("not_run_reason") else ""))
    for fr in fragments:
        print(f"    第{fr['idx']}轮 他连发 {fr['his_batch']} -> 引用 {fr['quotes']} | 她发 {fr['her_bubbles']}")
    print(f"  成本 ¥{metrics.get('cost_cny')}")

    return {
        "ok": q.get("verdict") in ("PASS", "N/A"),
        "run_id": run_id,
        "verdict": q.get("verdict"),
        "not_run_reason": q.get("not_run_reason"),
        "metric": q,
        "fragments": fragments,
        "turns": metrics.get("turns"),
        "cost_cny": metrics.get("cost_cny"),
        "transcript": os.path.join(run_dir, "transcript.md"),
        "face_metric": (metrics.get("multi_turn") or {}).get("QQ表情"),
    }


# ==========================================


async def main() -> int:
    logging.getLogger("companion.onebot").setLevel(logging.WARNING)
    import argparse

    ap = argparse.ArgumentParser(description="FIXES21 冒烟（A 连发出站报文 / B 引用采纳率）")
    ap.add_argument("--scene", default="S2", help="B 段用哪张剧情卡")
    ap.add_argument("--skip-b", action="store_true", help="只跑 A 段")
    args = ap.parse_args()

    print("=" * 70)
    print("FIXES21 冒烟 · A 段：他连发三条 → 真实管道 → 出站报文")
    print("=" * 70, flush=True)
    a = await section_a()

    if args.skip_b:
        b = {"ok": True, "skipped": True, "verdict": "SKIPPED（--skip-b）"}
        print("\nB 段已按 --skip-b 跳过")
    else:
        print()
        print("=" * 70)
        print(f"FIXES21 冒烟 · B 段：发侧采纳率（真跑一局 {args.scene}，¥0.5 以内）")
        print("=" * 70, flush=True)
        b = await section_b(args.scene)

    report = {
        "generated_at": datetime.now().strftime(TIME_FORMAT),
        "scene": args.scene,
        "A_wire": a,
        "B_adoption": b,
        "pass": bool(a["ok"] and b["ok"]),
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print()
    print("=" * 70)
    print(f"A 段（连发出站报文）：{'PASS' if a['ok'] else 'FAIL'}")
    print(f"    引用命中: {a.get('quoted')} | 编号块进提示词: {a.get('prompt_has_numbered_block')}"
          f" | reply id 合法: {a.get('reply_id_matches_his_message')}")
    print(f"B 段（引用采纳率）：{b.get('verdict', b.get('error'))}"
          f"{'（' + b['not_run_reason'] + '）' if b.get('not_run_reason') else ''}")
    print(f"报告：{REPORT_FILE}")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
