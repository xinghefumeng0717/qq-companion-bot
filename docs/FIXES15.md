# 迭代任务书（FIXES15）：回复时机人格化（首条延迟 + "正在输入"视觉签名）

> 项目：QQ 伴侣机器人（D:/qq-companion）。前置阅读：`STATUS.md`、`docs/FIXES11.md`、本文件。
> 背景：第二轮（V2 大版本）开工。观察期确诊的最大"非人感"来源是**她永远在线、永远秒回**——真人在上课/练琴/睡觉时手机是扣着的。本迭代做"回复时机"的轻量版：按她的作息延迟首条回复，并在延迟末端用 NapCat 的"正在输入"状态制造真人打字错觉。
>
> 所有者已拍板的决策（不许翻案，直接执行）：
> 1. **只延迟"第一条"**：对话激活后（她 5 分钟内回过话）的后续回复不延迟，保持现在的节奏；延迟只作用于"她拿起手机的第一条"；
> 2. **"正在输入"的出现节奏**：延迟的大部分时间保持安静（她"还没看到"），发送前才出现 typing 状态，时长随消息长度走；
> 3. **深夜不做特殊处理**（凌晨也照回，走同一套延迟）；"已读慢回/隔夜回"（中量版）**明确不做**，下一轮再说；
> 4. 配置默认值兜底原则照旧：**服务器 config.toml 零改动也能跑**，新参数全部走代码默认值；
> 5. 执行模型**不动 `characters/`、`config.toml`**；部署由所有者另行执行。
>
> 全局约束（高压线）：
> 1. 完成后 `./venv/Scripts/python.exe -m unittest discover -s tests` 全绿（当前 376）；
> 2. 行为变更仅限本文件列出的点；开工前 `git status` 确认工作区干净；
> 3. 交付：逐项 diff 摘要 + 测试输出 + 沙箱验证证据（typing action 被正确调用）。

---

## 一、总体设计（执行模型先读懂再动手）

现状链路（`companion/turn_handler.py`）：聚合器提交一轮 → 视觉处理 → 组装 → LLM 生成 → replier 切段 → `send_chunk_fn` 逐段发送（段间 `chunk_delay_min~max` 秒）。整条链是"消息一到立刻办"。

改造后链路：

```
聚合器提交一轮
  → 判定"是否首条"（她最近 5 分钟内有没有回过话）
  → 首条：按她此刻作息选一个延迟 D（见规格 2）
      → 静默等待 D - T_typing（她"还没看到"）
      → 调 set_input_status(event_type=1)（她"开始打字"）
      → 等待 T_typing（与将发送的文字量正相关）
  → 非首条：跳过等待，直接走 typing 短展示（T_typing 同样计算，上限收紧）
  → 生成（生成期间 typing 保持）
  → 发送前调 set_input_status(event_type=0)，逐段发送
```

**设计要点（不许走样）**：
- 延迟与 typing 是"她的行为表演"，不是技术等待——所有等待必须 `asyncio.sleep`，绝不阻塞事件循环；
- typing 状态失败/超时必须静默降级（NapCat 不支持、网络抖动都不能影响主流程）；
- 生成失败走"刚刚走神了"兜底前，必须先 event_type=0 收掉 typing。

## 二、NapCat 能力封装（companion/onebot.py）

`OneBotClient` 新增方法（复用现有 `_call_action` 模式，参照 `get_msg` 的实现）：

```python
async def set_input_status(self, user_id: int, typing: bool) -> bool:
    """设置/取消"正在输入"状态。event_type: 1=正在输入, 0=停止。
    失败/超时（2s）/未连接 一律静默降级返回 False，绝不抛异常。"""
```

- action 名 `set_input_status`，参数 `{"user_id": user_id, "event_type": 1 if typing else 0}`；
- 调用处（turn_handler/main 的注入链）把该方法以回调形式注入 TurnHandler（参照现有 `send_msg_fn`/`assembler` 的注入方式），**TurnHandler 不直接 import onebot**；
- 沙箱（chat.py）注入一个只记日志的假实现（打印"typing 开/关"），保证沙箱零外呼。

## 三、延迟引擎（turn_handler.py + config.py）

1. **配置**（`companion/config.py` 的 `ReplyConfig` 或新建 `TimingConfig`，按现有结构习惯放置，全部带默认值）：
   - `timing_enabled: bool = True`
   - `typing_indicator_enabled: bool = True`
   - `first_reply_busy_delay_min/max: float = 60.0 / 600.0`（秒，忙时作息 1~10 分钟）
   - `first_reply_free_delay_min/max: float = 5.0 / 30.0`（秒，空闲作息 5~30 秒）
   - `active_conversation_window: float = 300.0`（秒，她 5 分钟内回过话即"对话激活"，不延迟）
   - `typing_seconds_per_10chars: float = 2.0`、`typing_min/max: float = 3.0 / 25.0`（typing 展示时长：每 10 字 2 秒，钳在 3~25 秒）
   - `busy_keywords` 不用配——忙/闲判定走下面的规则，不做关键词匹配。
2. **忙/闲判定**：复用 `persona.get_current_activity(now.hour, now.weekday(), holiday_span=span)` 的产出。**忙** = 活动命中结构化作息条目（daily_routine 里有明确条目的时段，如上课/练琴/合练/睡觉）；**闲** = 回退文案（"在度过属于自己的时间"）。判定函数放 `persona.py`（返回 (activity, is_structured) 或新增 `is_busy_now()`，执行模型选最小侵入的形态，注释写明语义）。
3. **首条判定**：查 turns 表最近一条 assistant 消息的 created_at，距今 < `active_conversation_window` 即非首条。查询走 memory/db 现有接口。
4. **延迟流程**（turn_handler 主流程开头，视觉处理之前）：
   - 首条且忙：D = uniform(busy_min, busy_max)；首条且闲：D = uniform(free_min, free_max)；非首条：D = 0；
   - T_typing 在**生成完成后**按实际将发送的纯文本长度计算（`len(text)/10 * per_10chars`，钳 min~max）——因此等待拆两段：先睡 `D`（静默期），生成，再开 typing 睡 `T_typing`，发送；
   - 非首条（D=0）：直接生成，然后 typing 展示 `min(T_typing, 8.0)` 秒（收紧上限，对话中她的打字是快的），发送；
   - typing 开启：生成完成、计算 T_typing 后 `set_input_status(uid, True)` → sleep → `set_input_status(uid, False)` → 逐段发送；typing 期间如果又有新消息进来**不打断**（聚合器只作用于接收侧，发送侧表演继续演完）。
5. **主动消息**（proactive.py）：同样走 typing 展示（D=0，T_typing 正常算），不另外搞一套。
6. **沉默权（FIXES13）优先**：模型输出 [沉默] 时不发送、也不该有 typing——沉默分支在生成后判定，若命中则 `set_input_status(uid, False)` 收尾后直接 return（顺序：生成 → 判定沉默 → 不沉默才开 typing）。
7. **熔断**：任何一步异常都不许让消息丢死——延迟/typing 环节整体 try/except，异常降级为"立即生成立即发送"并记 WARNING。

## 四、测试与验证

**单元测试**（新增 tests/test_fixes15.py）：
- 忙/闲判定：结构化条目时段=忙、回退文案=闲、长假=闲（LONG_HOLIDAY_ACTIVITY 是回退类）；
- 首条判定：5 分钟内有无 assistant 记录两分支；
- 延迟选择：忙首条落在 busy 区间、闲首条落在 free 区间、非首条 D=0；
- typing 时长：按字数计算 + 上下限钳制 + 非首条上限收紧；
- 静默降级：set_input_status 抛异常/返回 False 时主流程照常发送；
- 沉默权优先：[沉默] 时 typing 被收掉、无发送；
- 熔断：延迟环节抛异常降级为立即发送。
- 用 fake clock/mock asyncio.sleep 验证，不许真睡。

**沙箱验证**（scripts/smoke/smoke_fixes15.py，真实 API）：
- 忙时首条：日志可见"延迟 D 秒（作息：xxx）"→ typing 开 → typing 关 → 发送的完整序列；
- 连续第二条：无长延迟；
- 贴日志序列作为证据（沙箱注入的 typing 假实现打印开/关）。

**负面清单**：
1. 不做隔夜慢回/已读控制（`mark_private_msg_as_read` 本轮不碰）；
2. 不改聚合器参数、不改 chunk_delay 段间延迟（那是气泡节奏，与本轮的"拿起手机"延迟是两回事）；
3. 不动 `characters/`、`config.toml`、proactive 的调度闸门；
4. typing 状态不做"打了又删"的反复表演（本轮只开/关一次）；
5. 不部署、不碰服务器、不动 main 分支。

## 验收 DoD

1. `./venv/Scripts/python.exe -m unittest discover -s tests` 全绿（376 + 新增）；
2. smoke_fixes15 日志序列三段证据（忙首条/连续条/typing 开关）；
3. 逐项 diff 摘要；
4. git：施工前 commit 现状，施工完一个 commit（前缀 `FIXES15:`）。
