"""TTS 阶段 B 真实 API 冒烟 (scripts/smoke/smoke_tts_stageb.py)

验的是单测验不了的东西：**海螺（MiniMax）t2a_v2 这条真链路到底通不通**。
单测全用替身，能钉住请求体字段与降级分支，但"真 key + 真端点 + 真音色"
只有实打实调一次才知道。本项目死律：新代码必须至少一次真实 API 验证。

A 段（真合成，会产生 3 次计费）：用项目现有 `[models.minimax]` 档案的 key
调 t2a_v2，把三条样音落到 `data/voice_out/stageB_1.mp3 ~ stageB_3.mp3`，
逐条打印：实际耗时 / 文件大小 / 接口返回的计费字段（usage_characters 等）/
是否发生过一次失败重试。

B 段（发送管道，零 API）：拿 A 段的成品 mp3 走一次真实 `_send_chunk_to_onebot`
→ mock OneBot，验 record 段结构与临时文件清理口径。

`--from-cache`：不联网、不计费——只读上次的报告 JSON 复算并重印，
校验三个 mp3 仍在原地（结果可复算，符合本项目"留原文、可复跑"的评测纪律）。

key 来源优先级：`data/test_keys.toml` 的 minimax（测试 key，gitignored）
→ 回退 `config.toml` 的 `[models.minimax].api_key`。
报告里只打 key 前缀（6 字），**绝不打印密钥全文**。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
import time
import tomllib
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from companion.config import ModelPreset, TTSConfig  # noqa: E402
from companion.replier import VOICE_RECORD_PREFIX  # noqa: E402
from companion.tts import TTSManager, build_minimax_payload  # noqa: E402

REPORT_FILE = "data/smoke_tts_stageb_report.json"
OUT_DIR = "data/voice_out"
TEST_KEYS = "data/test_keys.toml"

# 三条样音：日常语气 / 带情绪收尾 / 中英混读（所有者实测过这个音色混读没问题，再验一遍）
SAMPLES: List[Tuple[str, str]] = [
    ("stageB_1", "刚练完琴，手有点酸。你吃了没？"),
    ("stageB_2", "好呀，那你先去忙，家里的事要紧"),
    ("stageB_3", "这篇 pre 的 DDL 是下周三，我先把文献过一遍。"),
]

MODEL = "speech-2.8-hd"
VOICE_ID = "Chinese (Mandarin)_Gentle_Senior"
SPEED = 1.0


def _load_key() -> Tuple[str, str]:
    """返回 (api_key, 来源说明)。只认这两处，不瞎试别的路径。"""
    if os.path.exists(TEST_KEYS):
        try:
            with open(TEST_KEYS, "rb") as f:
                key = str(tomllib.load(f).get("minimax", "")).strip()
            if key:
                return key, f"{TEST_KEYS} 的 minimax 字段"
        except Exception as e:
            print(f"  [!] 读 {TEST_KEYS} 失败：{e}")
    if os.path.exists("config.toml"):
        try:
            with open("config.toml", "rb") as f:
                key = str(tomllib.load(f).get("models", {}).get("minimax", {}).get("api_key", "")).strip()
            if key:
                return key, "config.toml 的 [models.minimax].api_key"
        except Exception as e:
            print(f"  [!] 读 config.toml 失败：{e}")
    return "", "未找到可用的 minimax key"


def _make_manager(key: str, max_chars: int = 60) -> TTSManager:
    preset = ModelPreset(
        provider="minimax",
        base_url="https://api.minimaxi.com/v1",
        api_key=key,
        chat="MiniMax-M3",
    )
    cfg = TTSConfig(
        enabled=True, provider="minimax", voice_id=VOICE_ID, speed=SPEED,
        model=MODEL, max_chars=max_chars,
    )
    return TTSManager(cfg, None, out_dir=OUT_DIR, minimax_preset=preset)


# ==========================================
# A 段：真实合成
# ==========================================


async def section_a(key: str) -> Dict[str, Any]:
    os.makedirs(OUT_DIR, exist_ok=True)
    mgr = _make_manager(key)

    # 用 spy 包住真正的 HTTP 层：既走生产路径，又能拿到接口返回的计费字段
    http_calls: List[Dict[str, Any]] = []
    orig_post = mgr._minimax_post

    async def _spy(url: str, payload: Dict[str, Any], api_key: str) -> Dict[str, Any]:
        t0 = time.time()
        rec: Dict[str, Any] = {"url": url, "payload_voice": payload["voice_setting"],
                               "payload_model": payload["model"], "text": payload["text"]}
        try:
            resp = await orig_post(url, payload, api_key)
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"
            rec["http_seconds"] = round(time.time() - t0, 2)
            http_calls.append(rec)
            raise
        rec["http_seconds"] = round(time.time() - t0, 2)
        rec["base_resp"] = resp.get("base_resp")
        rec["usage"] = resp.get("extra_info")
        rec["trace_id"] = resp.get("trace_id")
        http_calls.append(rec)
        return resp

    mgr._minimax_post = _spy  # type: ignore[assignment]

    results: List[Dict[str, Any]] = []
    print(f"  provider=minimax  model={MODEL}  voice_id={VOICE_ID}  speed={SPEED}")
    for label, text in SAMPLES:
        target = os.path.join(OUT_DIR, f"{label}.mp3")
        attempts = 0
        retried = False
        t0 = time.time()
        tmp: Optional[str] = None
        while attempts < 2:
            attempts += 1
            tmp = await mgr.synthesize(text)
            if tmp:
                break
            if attempts == 1:
                retried = True
                print(f"      [!] 第一发失败，重试一次")
        elapsed = round(time.time() - t0, 2)
        calls_this = http_calls[-attempts:] if http_calls else []
        if not tmp:
            print(f"  [X] {label}：合成失败（{attempts} 次尝试）")
            results.append({"label": label, "text": text, "ok": False,
                            "attempts": attempts, "failed_retry": retried})
            continue
        if os.path.exists(target):
            os.remove(target)
        shutil.move(tmp, target)
        size = os.path.getsize(target)
        usage = next((c.get("usage") for c in reversed(calls_this) if c.get("usage")), None)
        rec = {
            "label": label,
            "text": text,
            "ok": True,
            "path": os.path.abspath(target),
            "size_bytes": size,
            "elapsed_seconds": elapsed,
            "attempts": attempts,
            "failed_retry": retried,          # 是否发生过一次失败重试
            "billing": usage,                 # 接口返回的计费字段（usage_characters 等）
            "http_calls": calls_this,
        }
        results.append(rec)
        print(f"  · {label}：{text}")
        print(f"      {rec['path']}")
        print(f"      大小 {size} 字节 | 耗时 {elapsed}s | 尝试 {attempts} 次"
              f"（失败重试：{'有' if retried else '无'}）")
        if usage:
            print(f"      计费字段：{json.dumps(usage, ensure_ascii=False)}")

    ok = all(r["ok"] for r in results) and len(results) == len(SAMPLES)
    leftovers = [f for f in os.listdir(OUT_DIR) if f.startswith("tts_")]
    if leftovers:
        print(f"  [!] 残留临时文件：{leftovers}")
    return {
        "ok": ok and not leftovers,
        "provider": "minimax",
        "model": MODEL,
        "voice_id": VOICE_ID,
        "speed": SPEED,
        "results": results,
        "http_calls": http_calls,
        "temp_files_left": leftovers,
        "note": "stageB_1~3.mp3 是给所有者亲耳复核的样音（音色定稿前的最后一道人手关）",
    }


# ==========================================
# B 段：发送管道（零 API，用 A 段成品）
# ==========================================


class _RecordingOneBot:
    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []

    async def send_private_msg(self, user_id, message_segments, max_retries=2):
        self.sent.append({"user_id": user_id, "message": message_segments})
        return True


class _StubBot:
    """只借 CompanionBot 的发送逻辑（不装配任何生产组件）"""

    def __init__(self, onebot, tts):
        self.config = type("C", (), {"account": type("A", (), {"allowed_user_id": 10001})()})()
        self.onebot = onebot
        self.tts = tts


class _FileTTS:
    """合成替身：直接返回已存在的 mp3（B 段不该再烧一次 API）"""

    def __init__(self, path: str):
        self.path = path

    async def synthesize(self, text: str):
        return self.path

    async def bump_daily(self) -> int:
        return 1

    def cleanup(self, path):
        return None


async def section_b(first_mp3: Optional[str]) -> Dict[str, Any]:
    from companion.main import CompanionBot

    if not first_mp3 or not os.path.exists(first_mp3):
        return {"ok": False, "reason": "A 段没有产出可复用的 mp3"}

    ob = _RecordingOneBot()
    bot = _StubBot(ob, _FileTTS(first_mp3))
    bot._send_chunk_to_onebot = CompanionBot._send_chunk_to_onebot.__get__(bot)
    bot._send_voice_chunk = CompanionBot._send_voice_chunk.__get__(bot)

    chunk = {"type": "voice", "content": "刚练完琴，手有点酸。你吃了没？"}
    await bot._send_chunk_to_onebot(chunk)

    outbound = []
    for s in ob.sent:
        segs = []
        for seg in s["message"]:
            if seg.get("type") == "record":
                f = seg["data"]["file"]
                segs.append({"type": "record", "data": {"file": f"{f[:26]}…({len(f)} chars)"}})
            else:
                segs.append(seg)
        outbound.append(segs)
    print(f"  出站报文：{json.dumps(outbound, ensure_ascii=False)}")
    ok = bool(outbound) and outbound[0][0]["type"] == "record"
    return {
        "ok": ok,
        "outbound": outbound,
        "record_prefix": VOICE_RECORD_PREFIX,
        "note": "零 API：复用 A 段成品 mp3，只验 record 段与发送链路",
    }


# ==========================================
# --from-cache：零成本复算
# ==========================================


def from_cache() -> int:
    if not os.path.exists(REPORT_FILE):
        print(f"没有缓存报告：{REPORT_FILE}；先跑一次真实冒烟")
        return 1
    with open(REPORT_FILE, "r", encoding="utf-8") as f:
        report = json.load(f)
    a = report.get("A_real_synthesis", {})
    print("=" * 70)
    print("TTS 阶段 B 冒烟 · --from-cache 复算（零 API、零计费）")
    print("=" * 70)
    print(f"  报告时间：{report.get('generated_at')}")
    print(f"  provider={a.get('provider')} model={a.get('model')} "
          f"voice_id={a.get('voice_id')} speed={a.get('speed')}")
    ok = True
    for r in a.get("results", []):
        exists = os.path.exists(r.get("path", ""))
        size_now = os.path.getsize(r["path"]) if exists else 0
        size_match = exists and size_now == r.get("size_bytes")
        ok = ok and bool(r.get("ok")) and size_match
        print(f"  · {r.get('label')}: {r.get('path')}")
        print(f"      ok={r.get('ok')} 报告大小={r.get('size_bytes')} "
              f"现存={size_now if exists else '文件缺失'} 大小一致={size_match} "
              f"耗时={r.get('elapsed_seconds')}s 尝试={r.get('attempts')} "
              f"计费={json.dumps(r.get('billing'), ensure_ascii=False)}")
    print(f"  复算结论：{'一致' if ok else '有出入'}")
    return 0 if ok else 1


# ==========================================


async def main() -> int:
    ap = argparse.ArgumentParser(description="TTS 阶段 B 冒烟（海螺 MiniMax t2a_v2）")
    ap.add_argument("--from-cache", action="store_true",
                    help="不联网、只复算上次报告（零计费）")
    args = ap.parse_args()
    logging.getLogger("companion.tts").setLevel(logging.WARNING)

    if args.from_cache:
        return from_cache()

    print("=" * 70)
    print("TTS 阶段 B 冒烟 · A 段：真实 t2a_v2 合成（3 次，走套餐额度）")
    print("=" * 70, flush=True)
    key, source = _load_key()
    if not key:
        print("  [X] 没有可用的 minimax key（data/test_keys.toml / config.toml），停止。")
        return 1
    print(f"  key 来源：{source}（前缀 {key[:6]}，长度 {len(key)}）")
    a = await section_a(key)

    print()
    print("=" * 70)
    print("TTS 阶段 B 冒烟 · B 段：发送管道（零 API）")
    print("=" * 70, flush=True)
    first = next((r["path"] for r in a["results"] if r.get("ok")), None)
    b = await section_b(first)

    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "key_source": source,
        "key_prefix": key[:6],
        "A_real_synthesis": a,
        "B_send_pipeline": b,
        "pass": bool(a["ok"] and b["ok"]),
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print()
    print("=" * 70)
    print(f"A 段（真实合成）：{a['ok']}   ← 样音在 {OUT_DIR}/stageB_1~3.mp3")
    print(f"B 段（发送管道）：{b['ok']}")
    print(f"报告：{REPORT_FILE}")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
