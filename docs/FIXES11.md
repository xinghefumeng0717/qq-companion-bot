# 迭代任务书（FIXES11）：观察期首轮修复——主动消息历史注入 / 节假日感知 / facts 更新通路 / 欲言又止门控 / 表情包稳定化

> 项目：QQ 伴侣机器人（D:/qq-companion）。前置阅读：`STATUS.md`、本文件。
> 背景：V2 卡封版 + 第三次 reset 后进入观察期。2026-10-03 首次观察检查（拉取服务器 2 天生产库：64 turns / 29 次观察者评分 / 414 次 LLM 调用）暴露一批**机制层实锤问题**，全部有生产证据。本迭代只修机制层；文字层（爹味、告别拖尾、话量、梗寿命、比喻质量）走沙箱 A/B 盲测后另出任务书，**不在本文件范围**。
>
> 所有者已拍板的决策（不许再翻案讨论，直接执行）：
> 1. 主聊模型留守 `deepseek-v4-pro`，本迭代不做任何模型盲测/切换；
> 2. 本迭代只修机制层（任务 1~6），**不动 `characters/` 任何文件**；
> 3. `replier.py` 的 `fit_chunks` "优先保表情包"是**有意设计**（docstring 已写明语义理由），本迭代**不改它**，表情包问题从源头（列表稳定性+硬上限）治理；
> 4. 部署由 Kimi/所有者另行执行（先传后挪+chown，config.toml 冻结），执行模型**不部署、不碰服务器**。
>
> 全局约束（高压线）：
> 1. 执行模型**禁止改动 `characters/` 任何文件、`config.toml`**（代码默认值兜底原则照旧）；
> 2. 完成后 `./venv/Scripts/python.exe -m unittest discover -s tests` 全绿（当前 210）；
> 3. 行为变更仅限本文件列出的点；开工前 `git status` 确认工作区干净，开工即新分支或直接 master 上先 commit 现状；
> 4. 新代码必须至少一次真实 API 冒烟（本地沙箱 `companion/chat.py` 走本地 config.toml 的真实 key，零服务器副作用）；
> 5. 交付：逐项 diff 摘要 + 测试输出 + 冒烟证据。

---

## 生产证据速查（执行模型必读，每条都是真实发生的翻车）

| 证据 | 时间 | 现象 | 根因任务 |
|---|---|---|---|
| E1 | 10-02 10:22 | 主动消息"你国庆动车票买好了没"——他 10-01 晚已坐动车到家 | 任务 1 |
| E2 | 10-03 09:02 | 主动消息"要不要一起吃点早午餐"——阶段 1 相识期邀约 + 他人在家里、她约学校食堂 | 任务 1 |
| E3 | 10-02 10:22 / 10-03 13:30 | 国庆假期中说"上午专业课""刚下课" | 任务 2 |
| E4 | facts 表 | "国庆期间打算坐动车回家"永久残留，事后不更新为"已回家" | 任务 3 |
| E5 | suppressed_desires | 阶段 1 出现"有点想他"；5 条里 2 条复读"咕咕嘎嘎" | 任务 4 |
| E6 | 表情包机制 | 提示词可用列表每次从 42 键随机抽 30，同一表情这轮可用下轮消失 | 任务 5 |
| E7 | diary #16 | 把**她自己说的**"食堂哪个窗口人少"记成**他问的**，10-03 主动消息拿错记忆反问"你上次不是还问哪个窗口人少嘛" | 任务 6 |

引擎面健康数据（不用动）：复合好感 23.0→24.94 节奏正常；观察者评分分布健康；气泡中位 12 字、疑问率 17% 命中真人基线；成本 ¥0.85/天。

---

## 任务 1：主动消息注入近期对话历史（P0，证据 E1/E2）

**病灶**：`companion/proactive.py:287-291` 生成主动消息时走 `assemble_system_prompt("")`（只产 system prompt），**0 条对话历史**；决策层 `PROACTIVE_DECISION_PROMPT`（`companion/prompts.py:212-235`）更贫瘠——连 facts 都没有。她对"最近聊了什么、他已经说过的事"完全无知，只能拿永不更新的 facts 硬编。

**规格**：

1. **生成层**（A 分支，`proactive.py` 生成路径）：
   - 通过 `self.memory.get_recent_turns(limit=8)` 取最近 8 条；
   - 格式化为文本块，注入 `PROACTIVE_GENERATE_PROMPT` 新增占位符 `{recent_chat}`：
     ```
     【最近的聊天记录】（你发这条消息前必须读过，严禁与已确立的事实矛盾）
     10-01 21:14 他：不不不，今天国庆，我打算坐动车回家……
     10-01 21:15 你：你呀，别总是一回头就消失……
     ```
     每条格式 `MM-DD HH:MM 他/你：内容`，内容截断 60 字；无历史时该块整体写"（今天是你们第一次聊天）"；
   - `PROACTIVE_GENERATE_PROMPT` 增加一条硬规则："发消息前核对【最近的聊天记录】：他已经说过的事不要重复问；他已经做过的事（如已回家、已吃过）不要当成未发生；不发起超出当前关系阶段的邀约（阶段 0~2 禁约线下见面）"。

2. **决策层**（`PROACTIVE_DECISION_PROMPT`）新增三个占位符：
   - `{recent_chat_brief}`：最近 5 条，每条截断 30 字（防决策层选重复话题）；
   - `{known_facts}`：`memory.get_all_facts()` 全量，`、`连接（让决策知道哪些话题已有定论）；
   - `{pending_desires}`：现存 suppressed_desires 的 content 列表（防 C 分支复读同一念头，配合任务 4）；
   - 无数据时分别写"无"。

3. **兼容性**：`proactive.py` 中两处 `PROACTIVE_DECISION_PROMPT.format(...)` 与 `PROACTIVE_GENERATE_PROMPT.format(...)` 同步补参；`prompts.py` 模板加占位符时保持其余文案不动。

**测试**：
- mock memory/db，断言生成层 messages 的 user prompt 含格式化后的近期记录、无历史时含"第一次聊天"兜底；
- 决策层 prompt 含三个新块；
- 现有 proactive 相关测试（tests/test_fixes*.py）同步补参保持全绿。

## 任务 2：节假日感知（P0，证据 E3）

**病灶**：`persona.py:142-162 get_current_activity(hour, weekday)` 只认周几；全代码无节假日概念（`config.py` 的 `holidays` 仅用于计费峰谷判定）。国庆落工作日她照走"上课"作息，落周末模型自由发挥出错（"刚下课"）。

**规格**：

1. **数据源单一化**：节假日列表只有一个来源——`PricingConfig.holidays`（`config.py:52`，所有者每年手动填，格式 YYYY-MM-DD）。在 `Config` 顶层加一个只读便捷访问（如 `get_holidays()`），persona/assembler/proactive 经由它取值；**计费逻辑零改动**，只读不写。
2. **作息覆盖**：`persona.get_current_activity` 增加可选参数 `is_holiday: bool = False`；为 True 时按周六（days 含 5）的作息匹配（假期≈周末节奏），查不到再回落现有逻辑。调用方（`assembler.py:184`、`proactive.py:189`、`proactive.py:233` 三处）传入 `now_dt.strftime("%Y-%m-%d") in holidays`。
3. **显式告知**：`assembler.assemble_system_prompt` 的时间行（`assembler.py:145-147` 附近）在节假日追加一句：`，今天是法定节假日（学校放假，不上课）`；`proactive.py` 决策/生成两处的 `{current_time}` 或 `{current_activity}` 同理带上该信息（可与任务 1 共用一次 holidays 查询，别重复读配置）。
4. **不做**：不区分具体节日名称（"国庆""春节"由所有者填日期即可，模型从日期+对话上下文自然得知）；不动 character.json 的 daily_routine。

**测试**：`get_current_activity(is_holiday=True)` 命中周六作息；assembler system prompt 在 mock 节假日时含"放假"提示；非节假日行为与现状完全一致（快照对比）。

## 任务 3：facts 更新/作废通路（P1，证据 E4）

**病灶**：`memory.py:448-488 add_fact()` 只有"新增 + Jaccard≥0.6 去重（命中仅刷时间戳、保留旧文案）"，无任何更新/作废通路。对比 followups 有 `done=1` 作废机制（`observer.py:214-230`）。

**规格**：

1. `memory.py` 新增 `async def supersede_fact(self, old_content: str, new_content: str) -> bool`：
   - 用与 `add_fact` 相同的字符 Jaccard 在 facts 全量里找 `old_content` 的最佳匹配，阈值放宽到 **≥0.4**（"打算坐动车回家" vs "国庆打算坐动车回家" 这类表述漂移要能命中）；
   - 命中：同事务内 `DELETE` 旧行 + 走 `add_fact(new_content)` 插入新事实，返回 True；
   - 未命中：只插入新事实（等价 add_fact），返回 False；全程记 INFO 日志。
2. `observer.py` 事实抽取协议扩展（向后兼容）：观察者输出 JSON 在现有 `facts` 字段之外，**可选**输出 `updated_facts: [{"old": "...", "new": "..."}]`；解析时逐项调用 `supersede_fact`；缺该字段/解析失败时行为与现状一致，**绝不允许因此炸掉观察结算**（参照 FIXES10 的 observer 容错原则）。
3. `OBSERVER_SYSTEM_PROMPT` 补一段说明："若本轮对话让某条既有事实过时（计划变已发生、打算变已做、喜好改变等），在 updated_facts 中输出旧事实原文要点与新事实；facts 字段只放全新事实。"（观察者输入里若当前不含 facts 列表，则把 `get_all_facts()` 一并注入观察者输入，让它知道"既有事实"是什么。）
4. **不做**：不加 TTL/自动过期；不改 facts 表结构（不加字段）。

**测试**：supersede 命中/未命中两路径（断言旧行消失、新行存在）；observer 带/不带 updated_facts 的解析兼容；updated_facts 解析异常时结算不炸。

## 任务 4：suppressed_desires 阶段门控 + 题材去重（P1，证据 E5）

**病灶**：desires 由 `proactive.py:256-270` 决策 C 分支写入，用的 `PROACTIVE_DECISION_PROMPT` 只传阶段名，**日记的"阶段 0~2 禁想他/喜欢/心疼"门控（`prompts.py:161-170`）没有同步过来**；且 topic_hint 无任何去重，同题材（咕咕嘎嘎）连刷。

**规格**：

1. `PROACTIVE_DECISION_PROMPT` 增加 `{stage_gating}` 占位符：阶段 0~2 时注入与日记同源的克制条款（"C 分支的念头同样受阶段约束：阶段 0~2 严禁'想他/喜欢他/心疼/心动'类依恋词，念头只能是好奇、吐槽、分享欲层面"），阶段 3+ 注入空串。阶段索引在 `proactive.py` 决策处取 `aff_state["stage"]` 判断。
2. 写入前去重（`proactive.py` C 分支 insert 之前）：新 topic_hint 与现存 desires（全部未删行）及**最近 3 条已发主动消息**（turns 表 proactive=1）做字符 Jaccard，≥0.4 视为同题材——**跳过 insert**（念头仍然"发生"过，只是不落库），记 INFO 日志。
3. 复用：Jaccard 计算已有多处拷贝（memory.py add_fact、facts 去重），本次**不重构合并**（负面清单），在 proactive.py 内写一个小的模块级辅助函数即可，注释注明与 memory.py 实现同源。

**测试**：阶段 1 时决策 prompt 含克制条款、阶段 4 不含；同题材 topic_hint 不落库；不同题材正常落库。

## 任务 5：表情包可用列表稳定化 + 硬上限（P2，证据 E6）

**病灶**：`stickers.py:125-130 get_prompt_sticker_list()` 键数 >30 时 `random.sample(all_keys, 30)`——同一个描述词这轮在列表、下轮消失，模型建立不了稳定手感；且"整轮最多一次"只是提示词软约束，代码不校验。

**规格**：

1. **列表稳定**：取消随机抽样，改为**确定性**返回：按键名排序后全量返回（当前 42 键，拼进提示词成本可忽略；若未来超过 60 个，按 `created_at` 升序取前 60，仍保持确定性）。docstring 同步。
2. **硬上限**：`replier.py` 解析出的段列表中，sticker 段只保留**第一个**，多余 sticker 段丢弃并记 INFO（防线：无论模型输出几个 [sticker:...]）。位置在 sticker 解析之后、fit_chunks 之前。
3. **明确不动**：`fit_chunks` 的"优先保图"语义保持原样（有意设计）；不加冷却计时器（生产实测 35 条消息仅 2 次表情包，频率无病，不动）。

**测试**：42 键时两次调用返回完全相同的列表；3 个 sticker 段压成 1 个且保留首个；fit_chunks 现有测试不受影响。

## 任务 6：日记事实归属防扭曲（P2，证据 E7）

**病灶**：`DIARY_SYSTEM_PROMPT` 无事实归属约束，日记把"她说的话"记成"他问的"，错记忆经【欲言又止】/日记摘要回流到主动消息，幻觉自产自销。

**规格**：`prompts.py` 的 `DIARY_SYSTEM_PROMPT` 反模板条款区追加一条：
> 【事实归属红线】日记只记录真实发生的对话，谁说的话、谁做的事必须与聊天记录严格对得上；严禁把"我"说过的话记成"他"说的，严禁虚构聊天里没有的情节。拿不准归属就不写进日记。

**测试**：无需新增（提示词文案变更）；现有日记测试全绿即可。

---

## 负面清单（不许做）

1. 不动 `characters/`、`config.toml`、`fit_chunks`、计费逻辑、observer 结算主流程语义；
2. 不合并/重构 Jaccard 多处拷贝、不重构 proactive.py 结构、不给 facts 表加字段、不加表情包冷却；
3. 不加新依赖；不改 OneBot 协议层；
4. 不部署、不碰服务器；不动 `main` 分支（GitHub 专用）；
5. 文字层问题（爹味/告别/话量/梗/比喻）**一律不碰**，另待任务书。

## 验收 DoD

1. `./venv/Scripts/python.exe -m unittest discover -s tests` 全绿（210 + 本迭代新增）；
2. 本地沙箱真实 API 冒烟至少一轮：构造"他昨晚说坐动车回家"的历史 → 触发一次主动消息生成，断言产出不含"买票/买好了没"类重复询问（贴出沙箱 transcript 作为证据）；
3. 逐项 diff 摘要（按任务 1~6 分组）；
4. git：施工前 commit 现状，施工完一个 commit（message 前缀 `FIXES11:`）。
