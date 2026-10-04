# 迭代任务书（FIXES16）：生活剧本——她的生活有连续性（life_arcs + 事件驱动主动消息）

> 项目：QQ 伴侣机器人（D:/qq-companion）。前置阅读：`STATUS.md`、`docs/FIXES15.md`、本文件、**`docs/ZJU_LIFE_SEEDS.md`（素材池审核版，41 条短句，所有者审核后的版本才能用；`docs/ZJU_LIFE_MATERIALS.md` 是其调研底稿，仅备查）**。
> 背景：第二轮核心项。确诊的病：她的"生活"是每条消息即兴编的，说完就蒸发——提过的审查没有结果，说过的考试没有下文。本迭代给她一张"生活主线表"：她的生活按时间自动向前滚动，节点事件直接触发"脱口而出"的主动消息。
>
> 所有者已拍板的决策（不许翻案，直接执行）：
> 1. 主线密度：**同时活跃 2~3 条**，少了补、多了不生成（多了像连续剧，少了没感觉）；
> 2. 结果由模型现编（不设固定结果库），但带语气约束（见任务 3）；
> 3. 主线**只进提示词**，与 mood/affection 引擎零耦合（防"输出刻度 vs 输入预期"错位，本项目祖传病种）；
> 4. **素材池（docs/ZJU_LIFE_SEEDS.md）必须先经所有者审核删改**，执行模型开工前先确认该文件已被所有者编辑过（问所有者要确认），未经审核版本不得蒸馏进提示词；
> 5. 执行模型**不动 `characters/`、`config.toml`**；部署由所有者另行执行。
>
> 全局约束（高压线）：
> 1. 完成后 `./venv/Scripts/python.exe -m unittest discover -s tests` 全绿（当前 376+）；
> 2. 新表必须进 reset 清理清单与备份流程（reset 即清档，生活主线随记忆一起清零）；
> 3. 新代码至少一次真实 API 冒烟；
> 4. 交付：逐项 diff 摘要 + 测试输出 + 冒烟证据。

---

## 任务 1：数据层 life_arcs（db.py + reset.py）

1. `db.py` 建表（CREATE TABLE IF NOT EXISTS，老库平滑升级）：
```sql
CREATE TABLE IF NOT EXISTS life_arcs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,            -- 一句话主线名，如"乐团节目审查"
    detail TEXT NOT NULL,           -- 两三句背景与她的牵挂点
    key_date TEXT NOT NULL,         -- 节点日期 YYYY-MM-DD
    status TEXT NOT NULL DEFAULT 'upcoming',  -- upcoming / near / today / resolved / faded
    emotional_stake TEXT DEFAULT '', -- 她的情绪赌注，如"紧张，低音部那段还没合齐"
    resolution TEXT DEFAULT '',      -- 结果（任务 3 生成）
    event_announced INTEGER DEFAULT 0,  -- 事件消息是否已发
    created_at TEXT NOT NULL,
    resolved_at TEXT DEFAULT ''
)
```
2. `reset.py` 清档清单加入 life_arcs（参照 turns/diary/facts 的现有清理方式）；备份流程无需改动（整库备份已覆盖）。

## 任务 2：主线生成器（新模块 companion/arcs.py）

1. **触发**：两个时机——每天凌晨的备份/维护时段检查后补一次；或活跃主线（status 非 faded/resolved 超过 2 天）< 2 条时即时补。用 flash 模型（purpose 新增 `life_arc`，计费落库照现有模式；thinking 开 low——产物的措辞会间接进入她的话）。
2. **生成提示词**（prompts.py 新增 `LIFE_ARC_GENERATE_PROMPT`）输入：
   - 角色卡 core_description（只读注入）；
   - 当前日期 + **学期节奏锚点**（见下）；
   - **素材池蒸馏常量**：直接以所有者审核后的 `docs/ZJU_LIFE_SEEDS.md` 全文（41 条）为素材池（课程形态/校园地点/日常烦恼/乐团文化四类），做成 prompts.py 的模块级常量 `LIFE_ARC_SEED_POOL`（注释注明来源文件与"所有者已审核"，文件后续修订时同步更新常量）；
   - 近 30 天已有主线标题列表（防重复）；
   - 输出 JSON：1~2 条主线，每条 {title, detail, key_date, emotional_stake}；key_date 必须在未来 3~14 天内。
3. **学期节奏锚点**：persona.py 内置常量 `ZJU_CALENDAR_ANCHORS`（列表：(日期范围, 一句话锚点)，按 2025-2026 校历硬事实，来源 docs/ZJU_LIFE_MATERIALS.md 第一节，如 `("10-24","10-26","校运动会，10-24 停课")`、`("11-08","11-09","秋学期考试")`、`("11-10","11-16","冬学期开学+体测预约")`、`("01-07","01-16","冬学期期末考周")` 等，覆盖秋/冬/春/夏全学年）。生成时取"当前日期所在的锚点 + 下一个锚点"注入。
4. **入库前去重**：新主线 title+detail 与近 30 天所有主线做字符 Jaccard（复用现有同源实现，阈值 ≥0.5 判重），重的丢弃并记 INFO。
5. **上限**：活跃主线 ≥3 条时本轮生成结果全部丢弃（宁可少不可滥）。

## 任务 3：状态推进与结果生成（arcs.py）

1. **状态机**（每次主动消息调度醒来的周期里顺手检查，或独立的小时级 tick，执行模型选侵入最小的挂点）：
   - `upcoming`：key_date 距今 > 2 天；
   - `near`：距今 1~2 天；
   - `today`：key_date 当天；
   - `today` 且当天 18:00 后：调 flash 生成 `resolution`（1~2 句结果，见下）→ 状态 `resolved`，写 resolved_at；
   - `resolved` 超过 2 天：状态 `faded`（不再注入提示词，行保留供去重）。
2. **结果生成约束**（写进提示词）：大多数结果平淡顺利（"过了""交上了"），少数带小波折（"过了但被要求加排""论文被退回改一版"），**禁止戏剧性大事件**（获奖、事故、表白等）；结果必须呼应 detail 里的牵挂点；1~2 句大白话，不带任何文学修辞。

## 任务 4：提示词注入【她最近的生活】（assembler.py）

1. system prompt 新增区块（位置在【事实】区之后，参照现有 `_build_*_block` 模式）：
```
【她最近的生活】（这是她当下真实的生活主线，她的话会自然带上这些事的进展）
- （临近）乐团节目审查：紧张，低音部那段还没合齐（后天审查）
- （有结果）期中读书报告：交上了，老师说选题有意思（昨天出的结果）
```
   - 注入 status ∈ {near, today} 的全部 + resolved ≤2 天的；upcoming 不注入（还没到她心里）；
   - 主聊与主动消息共用 `assemble_system_prompt`，一次接入两边生效；
   - 无任何活跃主线时整块省略（不留空标题）。

## 任务 5：事件驱动主动消息（proactive.py）

1. `trigger_cycle` 在常规 LLM 决策**之前**插入事件检查：存在 `status='resolved'`、`event_announced=0`、`resolved_at` 在 24 小时内的主线 → 走事件通道：
   - 直接用该主线+结果构造 topic_material，附加生成指令："这是刚刚发生在她身上的事，她脱口而出分享结果——语气是事情刚发生时的第一反应（报喜/吐槽/松口气），不是汇报"；
   - 标记 `event_announced=1` 后走正常生成→发送管道（含 replier、typing 表演若 FIXES15 已就位）；
   - **闸门**：免打扰时段（0~8 点）事件**等待不丢**（跳过本次周期，下轮再检查）；每天事件消息上限 1 条；事件消息计入 `unanswered_proactive` 的现有连续未回闸门；
2. 事件通道当天无事件时，回落到现有常规决策层，行为不变。

## 任务 6：测试与冒烟

**单元测试**（tests/test_fixes16.py）：建表平滑升级（老库无此表不炸）；状态机各迁移（upcoming→near→today→resolved→faded）；生成去重；活跃上限；注入区块的三种形态（无主线整块省略/只有 resolved/混合）；事件通道（触发、24h 窗口、免打扰等待、日上限、event_announced 置位）；reset 清表。
**真实 API 冒烟**（scripts/smoke_fixes16.py，全新临时库）：
- A 段：手工插入一条 key_date=今天、18:00 后的主线 → 跑推进+事件通道 → 贴出她的事件消息原文（应带"刚发生"的第一反应语气）；
- B 段：插入一条 near 主线 → 组装 system prompt → 贴【她最近的生活】区块；
- C 段：跑一次真实生成器 → 贴生成的主线 JSON（检查 key_date 在未来 3~14 天、与素材池气质一致、无重复）。

**负面清单**：
1. 不与 mood/affection/observer 耦合；不改主动消息常规决策层语义；
2. 不动 `characters/`、`config.toml`、看板（dashboard 展示主线留待以后）；
3. 素材常量只能来自所有者审核后的 ZJU_LIFE_SEEDS.md，禁止执行模型自行编造校园事实；
4. 不部署、不碰服务器、不动 main 分支。

## 验收 DoD

1. 测试全绿（376+ 新增）；2. 冒烟 A/B/C 三段证据；3. 逐项 diff 摘要；4. git 单 commit（前缀 `FIXES16:`）。
