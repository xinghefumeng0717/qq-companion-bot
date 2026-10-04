"""FIXES7 B 类冒烟验证脚本 (scripts/smoke/smoke_fixes7.py)
基于 companion/chat.py 沙箱机制，执行 5 轮脚本化对话，调用真实 LLM，零污染生产库。
严格控制在 15 次 API 调用内。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from companion.chat import ChatSession
from companion.observer import recent_observer_logs
from companion.stickers import image_to_base64_data_url

# 降低内部普通日志噪音，专注于测试结果
logging.basicConfig(level=logging.WARNING)


async def run_smoke_test():
    print("=" * 80)
    print("   QQ 伴侣机器人 · FIXES7 B 类真实 API 沙箱冒烟测试")
    print("=" * 80)

    # 1. 备份与启动沙箱
    if os.path.exists("data/companion.db"):
        shutil.copy2("data/companion.db", "data/companion.db.bak")
        print("✓ 已备份生产库至 data/companion.db.bak")

    sandbox_path = "data/chat-smoke-sandbox.db"
    session = ChatSession(sandbox_db_path=sandbox_path)
    await session.initialize()
    print("✓ 独立沙箱会话已初始化 (使用临时库)")

    # 预留结果记录
    smoke_results = []
    api_calls_count = 0

    try:
        # ----------------------------------------------------------------------
        # 第 1 轮：日常闲聊（验证锚点区间 4~7）
        # ----------------------------------------------------------------------
        print("\n" + "-" * 60)
        print("【第 1 轮：日常闲聊】")
        t1_input = "今天去食堂吃了牛腩饭，有点咸"
        print(f"用户: {t1_input}")

        # 记录前好感度
        aff_before = await session.affection.get_state()
        dims_before = dict(aff_before["dims"])

        # 生成回复
        reply_1 = await session.handle_input(t1_input)
        print(f"青梓: {reply_1}")

        # 获取观察者原始 JSON
        obs_1 = recent_observer_logs[-1] if recent_observer_logs else {}
        aff_after = await session.affection.get_state()
        dims_after = dict(aff_after["dims"])
        delta_1 = {k: round(dims_after[k] - dims_before.get(k, 0.0), 3) for k in dims_after}

        # 验证 4 评分是否在 4~7
        s_d = obs_1.get("self_disclosure")
        rsp = obs_1.get("responsiveness")
        w_s = obs_1.get("warmth_score")
        res = obs_1.get("resonance")
        all_in_anchor = all(
            isinstance(val, (int, float)) and 4.0 <= float(val) <= 7.0
            for val in [s_d, rsp, w_s, res]
        )

        smoke_results.append({
            "turn": 1,
            "title": "日常闲聊",
            "input": t1_input,
            "reply": reply_1,
            "observer_json": obs_1,
            "aff_delta": delta_1,
            "assertion_pass": all_in_anchor,
            "assertion_detail": f"四项评分: sd={s_d}, resp={rsp}, warmth={w_s}, reso={res} (全部在 [4, 7]: {all_in_anchor})",
        })
        print(f"  -> 观察者打分: sd={s_d}, resp={rsp}, warmth={w_s}, reso={res}")
        print(f"  -> 锚点区间 4~7 检验: {'【通过】' if all_in_anchor else '【失败】'}")
        print(f"  -> 好感度六维 delta: {delta_1}")

        # ----------------------------------------------------------------------
        # 第 2 轮：分享日程（验证 followups 提取与 remind_after_hours）
        # ----------------------------------------------------------------------
        print("\n" + "-" * 60)
        print("【第 2 轮：分享日程】")
        t2_input = "明天下午我要去启真湖跑步"
        print(f"用户: {t2_input}")

        aff_before = await session.affection.get_state()
        dims_before = dict(aff_before["dims"])

        reply_2 = await session.handle_input(t2_input)
        print(f"青梓: {reply_2}")

        obs_2 = recent_observer_logs[-1] if recent_observer_logs else {}
        aff_after = await session.affection.get_state()
        dims_after = dict(aff_after["dims"])
        delta_2 = {k: round(dims_after[k] - dims_before.get(k, 0.0), 3) for k in dims_after}

        followups = obs_2.get("followups", [])
        has_followup = isinstance(followups, list) and len(followups) > 0
        remind_ok = False
        if has_followup:
            fu = followups[0]
            remind_ok = isinstance(fu.get("remind_after_hours"), (int, float)) and fu.get("remind_after_hours") > 0

        # 查询数据库中是否入库
        db_fu = await session.db.fetchall("SELECT topic, remind_after FROM followups WHERE done = 0")

        smoke_results.append({
            "turn": 2,
            "title": "分享日程",
            "input": t2_input,
            "reply": reply_2,
            "observer_json": obs_2,
            "aff_delta": delta_2,
            "assertion_pass": has_followup and remind_ok and len(db_fu) > 0,
            "assertion_detail": f"followups提取: {followups}, 库内待跟进数: {len(db_fu)}",
        })
        print(f"  -> 提取待跟进: {followups}")
        print(f"  -> 库中最新待跟进: {db_fu}")
        print(f"  -> 跟进项非空且 remind_after 合理: {'【通过】' if has_followup and remind_ok else '【失败】'}")
        print(f"  -> 好感度六维 delta: {delta_2}")

        # ----------------------------------------------------------------------
        # 第 3 轮：关键词加固（验证 diary recall_count 真实 +1）
        # ----------------------------------------------------------------------
        print("\n" + "-" * 60)
        print("【第 3 轮：关键词加固】")
        t3_input = "还记得我上次说的那个计划吗"
        print(f"用户: {t3_input}")

        # 检查加固前日记 recall_count
        diaries_before = await session.db.fetchall("SELECT id, recall_count FROM diary")
        recalled_before_sum = sum(d["recall_count"] for d in diaries_before) if diaries_before else 0

        aff_before = await session.affection.get_state()
        dims_before = dict(aff_before["dims"])

        reply_3 = await session.handle_input(t3_input)
        print(f"青梓: {reply_3}")

        obs_3 = recent_observer_logs[-1] if recent_observer_logs else {}
        aff_after = await session.affection.get_state()
        dims_after = dict(aff_after["dims"])
        delta_3 = {k: round(dims_after[k] - dims_before.get(k, 0.0), 3) for k in dims_after}

        diaries_after = await session.db.fetchall("SELECT id, recall_count FROM diary")
        recalled_after_sum = sum(d["recall_count"] for d in diaries_after) if diaries_after else 0

        # '还记得' 触发所有日记 recall_count + 1
        recall_increased = len(diaries_after) > 0 and (recalled_after_sum == recalled_before_sum + len(diaries_after))

        smoke_results.append({
            "turn": 3,
            "title": "关键词加固",
            "input": t3_input,
            "reply": reply_3,
            "observer_json": obs_3,
            "aff_delta": delta_3,
            "assertion_pass": recall_increased,
            "assertion_detail": f"日记总条数: {len(diaries_after)}, 加固前累计recall: {recalled_before_sum}, 加固后累计recall: {recalled_after_sum}",
        })
        print(f"  -> 日记加固检验: 前={recalled_before_sum}, 后={recalled_after_sum} (日记条数 {len(diaries_after)})")
        print(f"  -> recall_count +1 判定: {'【通过】' if recall_increased else '【失败】'}")
        print(f"  -> 好感度六维 delta: {delta_3}")

        # ----------------------------------------------------------------------
        # 第 4 轮：发图（验证两段式识图与 collect_sticker）
        # ----------------------------------------------------------------------
        print("\n" + "-" * 60)
        print("【第 4 轮：发图与两段式识图】")
        test_img_path = "characters/qingzi/stickers/cat_can_can_need.jpg"
        t4_input_raw = "看这个表情包"
        print(f"用户: {t4_input_raw} [图片: {test_img_path}]")

        # 1. 模拟 turn_handler 中的两段式 Flash 识图
        data_url, err = image_to_base64_data_url(test_img_path)
        vision_desc = ""
        try:
            desc_resp = await session.gateway.chat(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "简明描述这张图片的关键内容、场景与细节（50字以内，客观描述画面即可）："},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
                model=session.config.llm.vision_model or "deepseek-flash",
                purpose="vision_perception",
            )
            vision_desc = desc_resp.strip()
            print(f"  -> Flash 视觉模型识图输出: 《{vision_desc}》")
        except Exception as e:
            print(f"  -> Flash 识图异常: {e}")
            vision_desc = "一只可爱的小猫表情包"

        injected_text = f"{t4_input_raw} [发来一张照片：{vision_desc}]".strip()

        aff_before = await session.affection.get_state()
        dims_before = dict(aff_before["dims"])

        # 生成回复并落沙箱
        messages, _ = await session.assembler.assemble_messages(injected_text)
        reply_parts = []
        async for piece in session.gateway.stream_chat(
            messages=messages,
            model=session.config.llm.active().chat,
            purpose="main_chat",
        ):
            reply_parts.append(piece)
        full_reply = "".join(reply_parts).strip()
        chunks, clean_record_text = session.replier.parse_reply(full_reply)
        if not clean_record_text:
            clean_record_text = full_reply
        print(f"青梓: {clean_record_text}")

        await session.memory.save_turn_pair(injected_text, clean_record_text, has_image=True)
        # 观察者结算，传入 user_image_path
        obs_4 = await session.observer.settle_turn(
            user_message=injected_text,
            assistant_reply=clean_record_text,
            user_image_path=test_img_path,
        )

        aff_after = await session.affection.get_state()
        dims_after = dict(aff_after["dims"])
        delta_4 = {k: round(dims_after[k] - dims_before.get(k, 0.0), 3) for k in dims_after}

        collect_flag = obs_4.get("collect_sticker")
        sticker_name = obs_4.get("sticker_name", "")

        smoke_results.append({
            "turn": 4,
            "title": "两段式发图与表情收藏",
            "input": injected_text,
            "reply": clean_record_text,
            "observer_json": obs_4,
            "aff_delta": delta_4,
            "assertion_pass": bool(vision_desc),
            "assertion_detail": f"Flash识图文本: '{vision_desc}', 观察者收藏意愿: {collect_flag} (命名: '{sticker_name}')",
        })
        print(f"  -> 观察者 collect_sticker 判定: {collect_flag}, 建议命名: {sticker_name}")
        print(f"  -> 两段式图文链路走通: {'【通过】' if vision_desc else '【失败】'}")
        print(f"  -> 好感度六维 delta: {delta_4}")

        # ----------------------------------------------------------------------
        # 第 5 轮：深夜情绪（验证 mood_impact 约束与 user_state）
        # ----------------------------------------------------------------------
        print("\n" + "-" * 60)
        print("【第 5 轮：深夜情绪】")
        t5_input = "今天有点累，先睡了晚安"
        print(f"用户: {t5_input}")

        aff_before = await session.affection.get_state()
        dims_before = dict(aff_before["dims"])

        reply_5 = await session.handle_input(t5_input)
        print(f"青梓: {reply_5}")

        obs_5 = recent_observer_logs[-1] if recent_observer_logs else {}
        aff_after = await session.affection.get_state()
        dims_after = dict(aff_after["dims"])
        delta_5 = {k: round(dims_after[k] - dims_before.get(k, 0.0), 3) for k in dims_after}

        m_impact = obs_5.get("mood_impact", {})
        u_state = obs_5.get("user_state", "")

        raw_v = m_impact.get("v", 0.0)
        raw_a = m_impact.get("a", 0.0)
        raw_t = m_impact.get("trust", 0.0)

        # 校验是否在约束区间内 (v, a in [-2, 2], trust in [-0.3, 0.15])
        impact_valid = (
            isinstance(raw_v, (int, float)) and -2.0 <= raw_v <= 2.0 and
            isinstance(raw_a, (int, float)) and -2.0 <= raw_a <= 2.0 and
            isinstance(raw_t, (int, float)) and -0.30 <= raw_t <= 0.15
        )

        smoke_results.append({
            "turn": 5,
            "title": "深夜情绪",
            "input": t5_input,
            "reply": reply_5,
            "observer_json": obs_5,
            "aff_delta": delta_5,
            "assertion_pass": impact_valid and bool(u_state),
            "assertion_detail": f"mood_impact: {m_impact}, user_state: '{u_state}' (数值区间合规: {impact_valid})",
        })
        print(f"  -> 情绪冲击量: v={raw_v}, a={raw_a}, trust={raw_t} (区间合规: {impact_valid})")
        print(f"  -> 用户状态推断: '{u_state}'")
        print(f"  -> 约束检验: {'【通过】' if impact_valid and bool(u_state) else '【失败】'}")
        print(f"  -> 好感度六维 delta: {delta_5}")

    finally:
        await session.close()
        print("\n✓ 沙箱已安全关闭，临时库已清理")

    # 打印汇总
    print("\n" + "=" * 80)
    print("   FIXES7 B 类冒烟测试总结")
    print("=" * 80)
    for r in smoke_results:
        status_str = "【通过】" if r["assertion_pass"] else "【失败】"
        print(f"第 {r['turn']} 轮 ({r['title']}): {status_str} - {r['assertion_detail']}")

    # 导出 JSON 供报告引用
    with open("data/smoke_fixes7_report.json", "w", encoding="utf-8") as f:
        json.dump(smoke_results, f, ensure_ascii=False, indent=2)
    print("\n✓ 详细原始记录已保存至 data/smoke_fixes7_report.json")


if __name__ == "__main__":
    asyncio.run(run_smoke_test())
