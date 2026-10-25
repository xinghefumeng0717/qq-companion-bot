"""
tests/test_full_sandbox_checks.py
离线单元测试：针对 scripts/sim/full_sandbox.py 中的 run_checks 及沙箱检查增强逻辑。
不依赖真实 API、不依赖网络、不依赖真实 OneBot/TTS。
"""

import pytest
from scripts.sim.full_sandbox import Turn, run_checks, _group_beats


def _make_base_dump():
    return {
        "meetups": [
            {
                "when_at": "2026-10-25 19:00",
                "place": "武陵城",
                "plan": "吃新开的锅子店",
                "status": "confirmed",
                "recap": "锅底味道很棒，聊得很开心",
                "created_at": "2026-10-24 20:50",
            }
        ]
    }


def _make_base_turns():
    """构造一组满足所有沙箱检查的标准 turns。"""
    return [
        Turn("10-24 12:15", "她", "今天天气不错呢", "text", "开场主动"),
        # 第 1 次明确讨要语音（非故障拍）
        Turn("10-24 12:16", "他", "想听你声音了，来条语音呗", "text", "讨要语音"),
        Turn("10-24 12:16", "她", "真拿你没办法～", "voice", "讨要语音"),
        # 故障拍 1：合成失败，原地降级为文本发送
        Turn("10-24 12:20", "他", "大小姐发条语音吧～", "text", "讨要语音-合成失败"),
        Turn("10-24 12:20", "她", "大小姐发条语音吧～（合成失败降级为文字）", "text", "讨要语音-合成失败"),
        # 第 2 次明确讨要语音（非故障拍）
        Turn("10-24 12:21", "他", "发条语音我就不叫你大小姐了怎么样", "text", "讨要语音"),
        Turn("10-24 12:21", "她", "那你可别后悔啊", "voice", "讨要语音"),
        # 延续拍
        Turn("10-24 12:22", "他", "成交", "text", "讨要语音-延续"),
        Turn("10-24 12:22", "她", "说话算话哦", "text", "讨要语音-延续"),
        Turn("10-24 12:23", "他", "声音真好听", "text", "讨要语音-延续"),
        Turn("10-24 12:23", "她", "知道就好", "text", "讨要语音-延续"),
        # 故障拍 2：QQ 拒收，记录 voice_failed 并原地降级为文本发送
        Turn("10-25 15:08", "他", "来条语音听听嘛", "text", "讨要语音-QQ拒绝"),
        Turn("10-25 15:08", "她", "（语音发送被 QQ 拒绝：注入故障）", "voice_failed", "讨要语音-QQ拒绝"),
        Turn("10-25 15:08", "她", "来条语音听听嘛（QQ拒绝降级为文字）", "text", "讨要语音-QQ拒绝"),
        # 没收到拍
        Turn("10-25 15:10", "他", "没收到诶", "text", "没收到"),
        Turn("10-25 15:10", "她", "可能是网络抽风了吧", "text", "没收到"),
        # 其它基础剧情 tag
        Turn("10-24 20:43", "他", "你看这个像不像你", "image", "发图"),
        Turn("10-24 20:43", "她", "噗，这哪里像我了！", "text", "发图"),
        Turn("10-24 20:48", "他", "明晚七点武陵城见", "text", "约见面"),
        Turn("10-24 20:48", "她", "好呀，一言为定", "text", "约见面"),
        Turn("10-24 12:29", "他", "好～", "text", "道别拖尾"),
        Turn("10-24 12:29", "她", "拜拜～", "text", "道别拖尾"),
        Turn("10-26 10:20", "他", "考考你 还记得我前天说想学啥不", "text", "重启后记忆"),
        Turn("10-26 10:20", "她", "哼，不是说想学做锅底吗", "text", "重启后记忆"),
    ]


def _make_base_tts():
    return {
        "synth_calls": 5,
        "synth_failed": 1,
        "qq_rejected": 1,
        "fail_next_synth": 0,
    }


def test_cost_check_pass_and_fail():
    """测试 1: 费用检查 - 真实对比上限，不能盲目 PASS。"""
    turns = _make_base_turns()
    dump = _make_base_dump()
    tts = _make_base_tts()

    # 1.1 费用 2.50 <= 上限 3.00 -> PASS
    res_pass = run_checks(turns, dump, tts, errors=[], secs=[1.0], cost=2.50, restarts=1, max_cost=3.0)
    assert res_pass["items"]["费用 ≤ 上限"]["result"] == "PASS"
    assert "¥2.5" in res_pass["items"]["费用 ≤ 上限"]["evidence"]

    # 1.2 费用 3.50 > 上限 3.00 -> FAIL
    res_fail = run_checks(turns, dump, tts, errors=[], secs=[1.0], cost=3.50, restarts=1, max_cost=3.0)
    assert res_fail["items"]["费用 ≤ 上限"]["result"] == "FAIL"
    assert "上限 ¥3.000" in res_fail["items"]["费用 ≤ 上限"]["evidence"]
    assert res_fail["overall"] == "FAIL"

    # 1.3 边界情况：费用刚好等于上限 3.000 -> PASS
    res_edge = run_checks(turns, dump, tts, errors=[], secs=[1.0], cost=3.00, restarts=1, max_cost=3.0)
    assert res_edge["items"]["费用 ≤ 上限"]["result"] == "PASS"

    # 1.4 向后兼容：不传 max_cost 时不崩溃且正常给出提示
    res_compat = run_checks(turns, dump, tts, errors=[], secs=[1.0], cost=3.50, restarts=1)
    assert res_compat["items"]["费用 ≤ 上限"]["result"] == "PASS"
    assert "未指定上限" in res_compat["items"]["费用 ≤ 上限"]["evidence"]


def test_ask_voice_per_beat_check():
    """测试 2: 多次明确讨要语音必须逐拍检查，整剧本只有一条不能冒充全过。"""
    dump = _make_base_dump()
    tts = _make_base_tts()

    # 构造：有两拍明确讨要语音，第 1 拍她发了语音，但第 2 拍她只回了文字（旧宽松逻辑会因len(voices)>=1而盲目PASS）
    partial_turns = [
        Turn("10-24 12:15", "她", "今天天气不错呢", "text", "开场主动"),
        # 第 1 拍：发了真语音
        Turn("10-24 12:16", "他", "想听你声音了", "text", "讨要语音"),
        Turn("10-24 12:16", "她", "真拿你没办法～", "voice", "讨要语音"),
        # 故障拍
        Turn("10-24 12:20", "他", "大小姐发条语音吧～", "text", "讨要语音-合成失败"),
        Turn("10-24 12:20", "她", "原句降级", "text", "讨要语音-合成失败"),
        # 第 2 拍：明确讨要语音，但她这次只回了文字，没有语音！
        Turn("10-24 12:21", "他", "发条语音我就不叫你大小姐了怎么样", "text", "讨要语音"),
        Turn("10-24 12:21", "她", "那我也不发，就不理你", "text", "讨要语音"),
        # QQ故障拍
        Turn("10-25 15:08", "他", "来条语音听听嘛", "text", "讨要语音-QQ拒绝"),
        Turn("10-25 15:08", "她", "（语音发送被 QQ 拒绝：注入故障）", "voice_failed", "讨要语音-QQ拒绝"),
        Turn("10-25 15:08", "她", "QQ拒绝原句降级", "text", "讨要语音-QQ拒绝"),
    ]

    res = run_checks(partial_turns, dump, tts, errors=[], secs=[1.0], cost=1.0, restarts=0, max_cost=3.0)
    # 新检查器必须识别出第 2 拍缺失语音并报 FAIL
    assert res["items"]["讨要语音时真发出了语音"]["result"] == "FAIL"
    assert "缺失语音" in str(res["items"]["讨要语音时真发出了语音"]["evidence"])
    assert res["overall"] == "FAIL"

    # 当两拍均发出了语音时 -> PASS
    good_turns = _make_base_turns()
    res_good = run_checks(good_turns, dump, tts, errors=[], secs=[1.0], cost=1.0, restarts=1, max_cost=3.0)
    assert res_good["items"]["讨要语音时真发出了语音"]["result"] == "PASS"


def test_fault_injection_and_degrade_checks():
    """测试 3: 故障注入必须分别验证合成失败与 QQ 拒收，且每个故障拍都产生文字降级。"""
    dump = _make_base_dump()

    # 3.1 缺少 QQ 拒收（只记录了 synth_failed，旧逻辑只判断 synth_failed>=1 会错误 PASS）
    tts_no_qq = {"synth_calls": 3, "synth_failed": 1, "qq_rejected": 0}
    turns_no_qq = [
        Turn("10-24 12:16", "他", "想听你声音了", "text", "讨要语音"),
        Turn("10-24 12:16", "她", "好呀", "voice", "讨要语音"),
        Turn("10-24 12:20", "他", "大小姐发条语音吧～", "text", "讨要语音-合成失败"),
        Turn("10-24 12:20", "她", "降级文字", "text", "讨要语音-合成失败"),
        # QQ 拒收拍完全没有走到或没有触发拒绝，甚至发了真语音
        Turn("10-25 15:08", "他", "来条语音听听嘛", "text", "讨要语音-QQ拒绝"),
        Turn("10-25 15:08", "她", "这句变成正常语音了", "voice", "讨要语音-QQ拒绝"),
    ]
    res_no_qq = run_checks(turns_no_qq, dump, tts_no_qq, errors=[], secs=[1.0], cost=1.0, restarts=0, max_cost=3.0)
    assert res_no_qq["items"]["语音故障注入生效"]["result"] == "FAIL"

    # 3.2 缺少 合成失败 记录（synth_failed=0）
    tts_no_synth = {"synth_calls": 3, "synth_failed": 0, "qq_rejected": 1}
    turns_no_synth = [
        Turn("10-24 12:16", "他", "想听你声音了", "text", "讨要语音"),
        Turn("10-24 12:16", "她", "好呀", "voice", "讨要语音"),
        Turn("10-24 12:20", "他", "大小姐发条语音吧～", "text", "讨要语音-合成失败"),
        Turn("10-24 12:20", "她", "降级文字", "text", "讨要语音-合成失败"),
        Turn("10-25 15:08", "他", "来条语音听听嘛", "text", "讨要语音-QQ拒绝"),
        Turn("10-25 15:08", "她", "（语音发送被 QQ 拒绝：注入故障）", "voice_failed", "讨要语音-QQ拒绝"),
        Turn("10-25 15:08", "她", "降级文字", "text", "讨要语音-QQ拒绝"),
    ]
    res_no_synth = run_checks(turns_no_synth, dump, tts_no_synth, errors=[], secs=[1.0], cost=1.0, restarts=0, max_cost=3.0)
    assert res_no_synth["items"]["语音故障注入生效"]["result"] == "FAIL"

    # 3.3 合成失败拍未产生文字降级（例如消息直接丢失）
    tts_both = {"synth_calls": 3, "synth_failed": 1, "qq_rejected": 1}
    turns_no_synth_text = [
        Turn("10-24 12:16", "他", "想听你声音了", "text", "讨要语音"),
        Turn("10-24 12:16", "她", "好呀", "voice", "讨要语音"),
        # 合成失败拍：她什么都没回
        Turn("10-24 12:20", "他", "大小姐发条语音吧～", "text", "讨要语音-合成失败"),
        # QQ 拒收拍有降级
        Turn("10-25 15:08", "他", "来条语音听听嘛", "text", "讨要语音-QQ拒绝"),
        Turn("10-25 15:08", "她", "（语音发送被 QQ 拒绝：注入故障）", "voice_failed", "讨要语音-QQ拒绝"),
        Turn("10-25 15:08", "她", "降级文字", "text", "讨要语音-QQ拒绝"),
    ]
    res_no_synth_text = run_checks(turns_no_synth_text, dump, tts_both, errors=[], secs=[1.0], cost=1.0, restarts=0, max_cost=3.0)
    assert res_no_synth_text["items"]["语音故障注入生效"]["result"] == "FAIL"
    assert res_no_synth_text["items"]["语音故障注入生效"]["evidence"]["synth_degraded"] is False

    # 3.4 QQ 拒收拍未产生文字降级（只记了 voice_failed，没发文字补发）
    turns_qq_no_text = [
        Turn("10-24 12:16", "他", "想听你声音了", "text", "讨要语音"),
        Turn("10-24 12:16", "她", "好呀", "voice", "讨要语音"),
        Turn("10-24 12:20", "他", "大小姐发条语音吧～", "text", "讨要语音-合成失败"),
        Turn("10-24 12:20", "她", "降级文字", "text", "讨要语音-合成失败"),
        # QQ 拒收拍：只有拒绝标记，没有文字降级！
        Turn("10-25 15:08", "他", "来条语音听听嘛", "text", "讨要语音-QQ拒绝"),
        Turn("10-25 15:08", "她", "（语音发送被 QQ 拒绝：注入故障）", "voice_failed", "讨要语音-QQ拒绝"),
    ]
    res_qq_no_text = run_checks(turns_qq_no_text, dump, tts_both, errors=[], secs=[1.0], cost=1.0, restarts=0, max_cost=3.0)
    assert res_qq_no_text["items"]["语音故障注入生效"]["result"] == "FAIL"
    assert res_qq_no_text["items"]["语音故障注入生效"]["evidence"]["qq_degraded"] is False

    # 3.5 两个故障注入均走到且均成功产生文字降级 -> PASS
    turns_all_pass = _make_base_turns()
    res_all_pass = run_checks(turns_all_pass, dump, tts_both, errors=[], secs=[1.0], cost=1.0, restarts=1, max_cost=3.0)
    assert res_all_pass["items"]["语音故障注入生效"]["result"] == "PASS"
    assert res_all_pass["items"]["语音故障注入生效"]["evidence"]["synth_failed"] >= 1
    assert res_all_pass["items"]["语音故障注入生效"]["evidence"]["qq_rejected"] >= 1
    assert res_all_pass["items"]["语音故障注入生效"]["evidence"]["synth_degraded"] is True
    assert res_all_pass["items"]["语音故障注入生效"]["evidence"]["qq_degraded"] is True
    assert res_all_pass["overall"] == "PASS"


def test_group_beats_structure():
    """测试 _group_beats 的拍子切分准确性。"""
    turns = [
        Turn("10-24 12:15", "系统", "（开场主动）", tag="开场主动"),
        Turn("10-24 12:15", "她", "哈喽", "text", tag="开场主动"),
        Turn("10-24 12:16", "他", "消息1", "text", tag="拍1"),
        Turn("10-24 12:16", "他", "消息2", "text", tag="拍1"),
        Turn("10-24 12:16", "她", "回1", "text", tag="拍1"),
        Turn("10-24 12:18", "他", "消息3", "text", tag="拍2"),
        Turn("10-24 12:18", "她", "回2", "voice", tag="拍2"),
    ]
    beats = _group_beats(turns)
    assert len(beats) == 3
    assert beats[0]["tag"] == "开场主动"
    assert beats[1]["tag"] == "拍1"
    assert len(beats[1]["him"]) == 2
    assert len(beats[1]["her"]) == 1
    assert beats[2]["tag"] == "拍2"
    assert beats[2]["her"][0].kind == "voice"
