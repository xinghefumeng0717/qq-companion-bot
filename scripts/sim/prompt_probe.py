"""提示词对照探针：同一份真实提示词，只改一处，各跑几次，看她说话变不变。

想知道"这样写模型到底听不听"时用它，先量再改代码。它不碰数据库、不发 QQ，只调主聊模型。

用法（在项目根目录）：
    python -m scripts.sim.prompt_probe --run data \\
        --trace 20261025_150500 --variants my_variants.json --n 6

--run     含有 turn_traces/ 的目录，一般就是 data（线上每一轮都会记在 data/turn_traces/ 里）
--trace   轮次 trace_id 的一部分（比如时间戳），取那一轮真正发给模型的 messages
--variants  一个 JSON 文件，写要对比的几种改法（见下）；不给就只跑原样
--n       每种改法跑几次（默认 6）

variants 文件是一个列表，每项是一种改法，按顺序套用 ops：
[
  {"name": "原样", "ops": []},
  {"name": "把状态换成被晾了两天",
   "ops": [{"op": "replace_block", "title": "你此刻的状态",
            "text": "他已经 2 天没找你了。你憋着一口气。"}]},
  {"name": "去掉对他的看法", "ops": [{"op": "drop_block", "title": "你对他的看法"}]},
  {"name": "最后加一句", "ops": [{"op": "append_system", "text": "【你们现在】……"}]},
  {"name": "换他这句话", "ops": [{"op": "set_user", "text": "在干嘛"}]},
  {"name": "不带历史", "ops": [{"op": "no_history"}]},
  {"name": "改一处文字", "ops": [{"op": "replace_text", "old": "……", "new": "……"}]}
]

replace_block / drop_block 认的是以【标题】开头的一块：先在系统提示词里找，找不到再找
单独成条的系统消息（"你此刻的状态"就是这种）。

输出每种改法的平均字数、平均条数、祈使句占比和前几条原话。样本小，只看方向，
最后像不像她，仍然要所有者读原话判断。
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import glob
import json
import os
import re
import statistics
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from companion.config import Config  # noqa: E402
from companion.gateway import LLMGateway  # noqa: E402

_IMPERATIVE_RE = re.compile(r"别|不许|不准|记得|给我|赶紧|快去|先(?:说|吃|去|把)|听见没|老实")


def load_messages(run_dir: str, trace_part: str) -> list[dict[str, Any]]:
    for path in sorted(glob.glob(os.path.join(run_dir, "turn_traces", "*.jsonl"))):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                event = json.loads(line)
                if event.get("step") == "model_context" and trace_part in str(event.get("trace_id")):
                    return [dict(m) for m in event["payload"]["messages"]]
    raise SystemExit(f"在 {run_dir}/turn_traces 里没找到 trace_id 含 {trace_part!r} 的轮次")


def _block_re(title: str) -> re.Pattern[str]:
    return re.compile(r"【" + re.escape(title) + r"】.*?(?=\n\n【|\Z)", re.S)


def apply_ops(messages: list[dict[str, Any]], ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = copy.deepcopy(messages)
    for op in ops:
        kind = op["op"]
        if kind in {"replace_block", "drop_block"}:
            title = op["title"]
            pattern = _block_re(title)
            new_text = "" if kind == "drop_block" else f"【{title}】\n{op['text']}"
            if pattern.search(out[0]["content"]):
                out[0]["content"] = re.sub(r"\n{3,}", "\n\n", pattern.sub(lambda _m: new_text, out[0]["content"], count=1))
                continue
            for index, message in enumerate(out[1:], start=1):
                if message["role"] == "system" and message["content"].startswith(f"【{title}】"):
                    if kind == "drop_block":
                        del out[index]
                    else:
                        message["content"] = new_text
                    break
            else:
                raise SystemExit(f"没找到【{title}】这一块")
        elif kind == "append_system":
            out.insert(len(out) - 1, {"role": "system", "content": op["text"]})
        elif kind == "set_user":
            out[-1] = {"role": out[-1]["role"], "content": op["text"]}
        elif kind == "no_history":
            tail = [m for m in out[1:-1] if m["role"] == "system" and m["content"].startswith("【")]
            out = [out[0], *tail, out[-1]]
        elif kind == "replace_text":
            if op["old"] not in out[0]["content"]:
                raise SystemExit(f"系统提示词里没有这段文字：{op['old'][:30]}")
            out[0]["content"] = out[0]["content"].replace(op["old"], op["new"])
        else:
            raise SystemExit(f"不认识的 op：{kind}")
    return out


def summarize(replies: list[str]) -> str:
    lengths = [len(re.sub(r"\s", "", reply)) for reply in replies]
    bubbles = [line for reply in replies for line in reply.split("\n") if line.strip()]
    imperative = sum(bool(_IMPERATIVE_RE.search(line)) for line in bubbles) / max(1, len(bubbles))
    return (
        f"平均 {statistics.mean(lengths):.0f} 字 / {len(bubbles) / len(replies):.1f} 条"
        f"｜祈使句 {imperative:.0%}"
    )


async def run(args: argparse.Namespace) -> None:
    config = Config.load(args.config)
    gateway = LLMGateway(config.llm, None)   # 不接数据库：探针的调用不记入费用账本
    base = load_messages(args.run, args.trace)
    variants = [{"name": "原样", "ops": []}]
    if args.variants:
        with open(args.variants, encoding="utf-8") as handle:
            variants = json.load(handle)

    async def once(messages: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        async for piece in gateway.stream_chat(messages=messages, model=config.llm.text_model, purpose="main_chat"):
            parts.append(piece)
        return "".join(parts)

    try:
        print(f"他这一轮：{base[-1]['content']!r}\n")
        for variant in variants:
            messages = apply_ops(base, variant.get("ops", []))
            replies = await asyncio.gather(*[once(messages) for _ in range(args.n)])
            print(f"== {variant['name']}：{summarize(list(replies))}")
            for reply in replies[: args.show]:
                print("   · " + reply.replace("\n", " ⏎ "))
            print()
    finally:
        await gateway.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="同一份真实提示词改一处，对比她的回复")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--run", required=True)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--variants", default="")
    parser.add_argument("--n", type=int, default=6)
    parser.add_argument("--show", type=int, default=3)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
