"""FIXES22 语音回复（阶段 A）真实 API 冒烟 (scripts/smoke/smoke_fixes22.py)

单测全是手造文本，验不了"这个音色到底像不像她"——那只有耳朵能判。所以分三段：

A 段（真合成）：edge-tts 真合成几句话 → 打印 mp3 大小与时长，
   **文件保留在 data/voice_out/smoke_*.mp3 供所有者亲耳试听**（DoD 的放行必要条件：
   音色不像她，功能宁可不开）。
B 段（发送管道）：真实 TTSManager 合成 → 走 `_send_chunk_to_onebot` 到 mock OneBot，
   逐字贴出站报文（record 段结构）与落库记录形态。
C 段（闸门）：构造"她在上课/合练"的活动状态，确认 voice 被降级成文字；
   再验证日上限与开关两条。

零服务器副作用、零生产库读写（沙箱副本 + 本地 config.toml 的真实 key，
`[tts]` 段只在内存里开，**不写回任何文件**）。
报告落 data/smoke_fixes22_report.json。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _ROOT)
# tests/helpers.py（沙箱库/引擎堆夹具）不在包路径上，冒烟要用它
if os.path.join(_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "tests"))

from companion.config import ReplyConfig, TTSConfig
from companion.db import TIME_FORMAT
from companion.main import CompanionBot
from companion.replier import Replier
from companion.tts import TTSManager, activity_allows_voice

REPORT_FILE = "data/smoke_fixes22_report.json"
OUT_DIR = "data/voice_out"

# A 段试听文本：三句不同语气的短句（试听要能听出音色/语速/压不压播音腔）
TRYOUT_TEXTS = [
    ("睡着时", "睡啦，刚躺下，手指还有点酸。明天再找你。"),
    ("随手一句", "刚练完琴，在琴房坐着歇会儿。"),
    ("调侃他", "你那数据是蒙的吧，别是运气好了一次。"),
]


def _mp3_seconds(path: str, bitrate_kbps: int = 48) -> float:
    """粗估时长：edge-tts 的 mp3 固定 24kHz/48kbps 单声道（≈6KB/秒）"""
    return round(os.path.getsize(path) / 6000.0, 1)


class _RecordingOneBot:
    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []

    async def send_private_msg(self, user_id, message_segments, max_retries=2):
        self.sent.append({"user_id": user_id, "message": message_segments})
        return True


class _StubBot(CompanionBot):
    def __init__(self, onebot, tts=None):  # noqa: D107 - 只借发送逻辑
        self.config = type(
            "C", (), {"account": type("A", (), {"allowed_user_id": 10001})()}
        )()
        self.onebot = onebot
        self.tts = tts


# ==========================================
# A 段：真实合成（供所有者试听）
# ==========================================


async def section_a() -> Dict[str, Any]:
    os.makedirs(OUT_DIR, exist_ok=True)
    tts = TTSManager(TTSConfig(enabled=True), None, out_dir=OUT_DIR)
    results: List[Dict[str, Any]] = []

    print(f"  音色: {tts.config.voice} | 语速: {tts.config.rate}")
    for label, text in TRYOUT_TEXTS:
        # 存成 smoke_*.mp3 **不删**：这是给所有者亲耳试听的文件
        name = f"smoke_{label}_{datetime.now().strftime('%H%M%S')}.mp3"
        path = os.path.join(OUT_DIR, name)
        t0 = time.time()
        tmp = await tts.synthesize(text)
        elapsed = round(time.time() - t0, 2)
        if not tmp:
            print(f"  [X] {label}：合成失败")
            results.append({"label": label, "text": text, "ok": False})
            continue
        # 改名成人类可读的名字留档
        try:
            if os.path.exists(path):
                os.remove(path)
            os.rename(tmp, path)
        except Exception:
            path = tmp
        size = os.path.getsize(path)
        print(f"  · {label}：{text}")
        print(f"      {path}  {size} 字节 / 约 {_mp3_seconds(path)} 秒 / 合成耗时 {elapsed}s")
        results.append({
            "label": label, "text": text, "ok": True,
            "path": os.path.abspath(path), "size": size,
            "seconds_est": _mp3_seconds(path), "synth_seconds": elapsed,
        })

    ok = all(r["ok"] for r in results) and len(results) == len(TRYOUT_TEXTS)
    return {
        "ok": ok,
        "voice": tts.config.voice,
        "rate": tts.config.rate,
        "results": results,
        "note": "**这三个 mp3 是给所有者亲耳试听的，音色不像她就别开开关**",
    }


# ==========================================
# B 段：真实合成 + 真实发送管道 → mock OneBot
# ==========================================


async def section_b() -> Dict[str, Any]:
    from helpers import close_db, make_db

    db = await make_db()
    tts = TTSManager(TTSConfig(enabled=True), db, out_dir=OUT_DIR)
    ob = _RecordingOneBot()
    bot = _StubBot(ob, tts)
    replier = Replier(ReplyConfig(), _StubStickers())
    cases: List[Dict[str, Any]] = []

    try:
        for name, raw in (
            ("句中一段（主形态）", "在呢[voice:刚练完 手指都快断了[/voice]你说啥"),
            ("整条就是语音", "[voice:睡啦 明天聊[/voice]"),
            ("语音+face 同轮", "在呢[face:憨笑][voice:刚醒[/voice]"),
        ):
            ob.sent.clear()
            chunks, record = replier.parse_reply(raw, voice_allowed=True)
            for c in chunks:
                await bot._send_chunk_to_onebot(c)
            payloads = []
            for s in ob.sent:
                segs = []
                for seg in s["message"]:
                    if seg.get("type") == "record":
                        f = seg["data"]["file"]
                        segs.append({
                            "type": "record",
                            "data": {"file": f"{f[:22]}…(base64, {len(f)} 字符)"},
                        })
                    else:
                        segs.append(seg)
                payloads.append(segs)
            used, limit = await tts.daily_status()
            case = {
                "name": name,
                "her_raw": raw,
                "chunks": [{k: v for k, v in c.items() if not k.startswith("_")} for c in chunks],
                "record_text": record,
                "outbound": payloads,
                "bubble_count": len(ob.sent),
                "daily_used": used,
                "daily_limit": limit,
            }
            cases.append(case)
            print(f"  · {name}")
            print(f"      她的原始输出: {raw!r}")
            print(f"      落库记录: {record!r}")
            for p in payloads:
                print(f"      → send_msg message: {json.dumps(p, ensure_ascii=False)}")
            print(f"      今日语音: {used}/{limit}")
    finally:
        await close_db(db)

    # 落盘目录不该留临时文件（发送完必须删）
    leftovers = [
        f for f in os.listdir(OUT_DIR)
        if f.startswith("tts_") and os.path.isfile(os.path.join(OUT_DIR, f))
    ]
    print(f"  发送后残留的临时文件: {leftovers or '无（已清理）'}")
    return {
        "ok": all(c["outbound"] for c in cases) and not leftovers,
        "cases": cases,
        "temp_files_left": leftovers,
    }


class _StubStickers:
    def match_sticker(self, desc: str):
        return f"/fake/{desc}.png"

    def get_prompt_sticker_list(self):
        return ["猫猫"]


# ==========================================
# C 段：三道闸门（开关 / 日上限 / 作息）
# ==========================================


async def section_c() -> Dict[str, Any]:
    from helpers import close_db, make_db

    db = await make_db()
    # 每条检查都显式写 **expect**（期望的放行/拦截结果）。
    # 踩过的坑：第一版用 all(allowed) 判总结果，而"开关关 → 拦截"本来 allowed 就是
    # False —— 于是"闸门工作正常"被报成 FAIL。**判据必须有零点和方向**，
    # 不能拿"全都要 True"去卡一组一半期望 False 的检查。
    checks: List[Dict[str, Any]] = []

    def add(name: str, expect: bool, got: Any, why: str = ""):
        checks.append({
            "name": name, "expect": expect, "got": got, "why": why,
            "ok": (bool(got) == expect),
        })

    try:
        # C1 开关关 → 期望拦截
        off = TTSManager(TTSConfig(enabled=False), db)
        ok_off, why_off = await off.check_gate("回寝室洗漱，看手机聊天")
        add("开关关", False, ok_off, why_off)
        print(f"  · 开关关 → {'放行' if ok_off else '拦截'}（期望拦截）| {why_off}")

        # C2 作息闸门（用本卡真实文案）
        on = TTSManager(TTSConfig(enabled=True), db)
        for label, act, expect in (
            ("在合练（手机静音）", "蒙民伟楼大排练厅进行文琴交响乐团全团合练，手机静音", False),
            ("在上专业必修", "紫金港西教连上专业必修（古代汉语与文学经典精读），课间看一眼手机", False),
            ("在基础馆自习", "在基础馆自习室刷文献、做笔记", False),
            ("已经睡下", "已经睡下了，在浙大宿舍安静的梦乡中", False),
            ("在回寝室看手机", "回寝室洗漱，吃点水果，看手机聊天", True),
        ):
            ok_act, why_act = await on.check_gate(act)
            add(f"作息:{label}", expect, ok_act, why_act)
            mark = "OK" if bool(ok_act) == expect else "!!"
            print(f"  [{mark}] {label} → {'放行' if ok_act else '拦截'}"
                  f"（期望{'放行' if expect else '拦截'}）| {why_act}")

        # C3 日上限 → 期望拦截
        capped = TTSManager(TTSConfig(enabled=True, daily_limit=2), db)
        await capped.bump_daily()
        await capped.bump_daily()
        ok_cap, why_cap = await capped.check_gate("回寝室洗漱，看手机聊天")
        add("日上限到", False, ok_cap, why_cap)
        print(f"  · 日上限到 → {'放行' if ok_cap else '拦截'}（期望拦截）| {why_cap}")

        # C4 机制层兜底：闸门关着时 [voice:] **剥掉标记只发正文**
        replier = Replier(ReplyConfig(), _StubStickers())
        raw = "在呢[voice:刚练完 手指都快断了[/voice]你说啥"
        expect_degraded = "在呢刚练完 手指都快断了你说啥"
        chunks, record = replier.parse_reply(raw, voice_allowed=False)
        degraded_ok = (
            [c["type"] for c in chunks] == ["text"] and record == expect_degraded
        )
        add("降级为文字（剥标记、内容一字不少）", True, degraded_ok, record)
        print(f"  · 闸门关时降级为文字 → {'是' if degraded_ok else '否'}（期望剥掉标记只发正文）"
              f"| {record!r}")
        if not degraded_ok:
            print(f"      实际: {[c['type'] for c in chunks]} / {record!r}")

        # C5 超长语音必须在发送前被截断（max_chars 不是死配置）
        long_text = "我今天真的特别特别累，从早到晚没停过。现在只想瘫着。明天还要早起。" * 2
        l_chunks, l_record = replier.parse_reply(
            f"[voice:]{long_text}[/voice]", voice_allowed=True, voice_max_chars=60
        )
        clipped = l_chunks[0]["content"] if l_chunks else ""
        clip_ok = (
            l_chunks and l_chunks[0]["type"] == "voice"
            and len(clipped) <= 60 and clipped != long_text
            and l_record == f"（语音消息）{clipped}"
        )
        add("超长语音被截断且记录与实发一致", True, clip_ok,
            f"原 {len(long_text)} 字 → 合成 {len(clipped)} 字 / 记录={l_record!r}")
        print(f"  · 超长截断 → {'OK' if clip_ok else 'NG'}"
              f"（原 {len(long_text)} → {len(clipped)} 字，期望 ≤60 且记录=实发）")

        # C5 提示词双保险：关着时模型不该认识 [voice:/开着才认识
        # 找的是前缀 "[voice:" 而不是 "[voice:]"：纪律句里的示范是
        # [voice:刚练完…[/voice]（冒号后不带右括号），拿后者去 find 会永远落空，
        # 又是一次"判据找错字符串"的自伤。
        from helpers import make_engine_stack

        stack = make_engine_stack(db, persona_path=os.path.join("characters", "qingzi"))
        _m, prompt_off = await stack.assembler.assemble_messages("在吗", None, [], False)
        _m2, prompt_on = await stack.assembler.assemble_messages("在吗", None, [], True)
        off_has = "[voice:" in prompt_off
        on_has = "[voice:" in prompt_on
        prompt_ok = (not off_has) and on_has
        add("提示词双保险（关着不注入/开着才注入）", True, prompt_ok,
            f"off_has_voice={off_has}, on_has_voice={on_has}")
        print(f"  · 提示词双保险 → 关着有 [voice: {off_has} / "
              f"开着有 [voice: {on_has}（期望 False/True）")
    finally:
        await close_db(db)

    failed = [c["name"] for c in checks if not c["ok"]]
    return {
        "ok": not failed,
        "failed": failed,
        "checks": checks,
    }


# ==========================================


async def main() -> int:
    logging.getLogger("companion.onebot").setLevel(logging.WARNING)
    import argparse

    ap = argparse.ArgumentParser(description="FIXES22 冒烟（A 真合成/B 发送管道/C 闸门）")
    ap.add_argument("--skip-a", action="store_true", help="跳过真合成（不烧公网流量）")
    args = ap.parse_args()

    print("=" * 70)
    print("FIXES22 冒烟 · C 段：三道闸门（开关 / 日上限 / 作息）")
    print("=" * 70, flush=True)
    c = await section_c()

    print()
    print("=" * 70)
    print("FIXES22 冒烟 · B 段：真实合成 + 真实发送管道 → mock OneBot")
    print("=" * 70, flush=True)
    b = await section_b()

    if args.skip_a:
        a = {"ok": True, "skipped": True}
        print("\nA 段已按 --skip-a 跳过")
    else:
        print()
        print("=" * 70)
        print("FIXES22 冒烟 · A 段：真实合成（mp3 保留供试听）")
        print("=" * 70, flush=True)
        a = await section_a()

    report = {
        "generated_at": datetime.now().strftime(TIME_FORMAT),
        "A_real_synthesis": a,
        "B_send_pipeline": b,
        "C_gates": c,
        "pass": bool(a["ok"] and b["ok"] and c["ok"]),
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print()
    print("=" * 70)
    print(f"A 段（真实合成）：{a['ok']}  ← 试听文件在 {OUT_DIR}/smoke_*.mp3")
    print(f"B 段（发送管道）：{b['ok']}")
    print(f"C 段（三道闸门）：{c['ok']}")
    print(f"报告：{REPORT_FILE}")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
