# 迭代任务书（FIXES14）：长假作息与卡锚点冲突 + 观察者畸形数据容忍

> 项目：QQ 伴侣机器人（D:/qq-companion）。前置阅读：`STATUS.md`、`docs/FIXES11.md`、`docs/BENCHMARK_V4.md`、本文件。
> 背景：BENCHMARK_V4 首轮大考（105 格真实 API）挖出的两个实锤生产 bug，全部有证据。本迭代只修这两处，属小修；修完后与 FIXES12/13 + V3 卡**一次性部署**（所有者执行）。
>
> 所有者已拍板的决策（不许翻案，直接执行）：
> 1. 本迭代只含任务 1、2，**不动 `characters/`、`config.toml`**（长假期判定从现有 holidays 列表推导，不新增配置字段）；
> 2. 部署由所有者另行执行，执行模型**不部署、不碰服务器**；
> 3. BENCHMARK_V4 的判定口径重校准由 Kimi 另行处理，**执行模型不要动 `scripts/benchmark_v4.py` 的指标口径**。
>
> 全局约束（高压线）：
> 1. 完成后 `./venv/Scripts/python.exe -m unittest discover -s tests` 全绿（当前 319）；
> 2. 行为变更仅限本文件列出的点；开工前 `git status` 确认工作区干净；
> 3. 新代码至少一次真实 API 冒烟（本地沙箱走本地 config.toml 真实 key，零服务器副作用）；
> 4. 交付：逐项 diff 摘要 + 测试输出 + 冒烟证据。

---

## 生产证据

**E10（长假作息注入校园地点）**：V3 卡 `core_description` 写明"国庆、寒暑假长假她回绍兴老家，长假期间银泉、临湖、琴房等校园场景一律不出现"。但 FIXES11 的假期回退逻辑（`persona.get_current_activity(is_holiday=True)` 按周六作息匹配）会把周六作息里的校园活动塞进提示词——大考 J 场景长假钟实测产出原话"坐校车去**玉泉**老校区看老建筑""在玉泉转悠呢"。机制与卡在打架。

**E11（观察者整批丢弃事实）**：生产日志持续刷 `[Observer] 丢弃畸形 fact (dict: {'内容': ...})` 与 `{'topic': ..., 'remind_after_hours': ...}`——观察者模型返回的 facts/followups 是字典而非字符串，解析层（`companion/observer.py`）只接受字符串，**她实际观察到的事实被整批扔掉**（facts 是她长期记忆的主输入，等于记忆在漏）。

---

## 任务 1：长假期判定与长假作息注入（P0，证据 E10）

**规格**：

1. `companion/persona.py` 新增模块级函数 `holiday_span(date_str: str, holidays: List[str]) -> int`：返回 `date_str` 所属的**连续节假日段长度**（ holidays 里与 date_str 前后相连的日期个数，含当天；date_str 不在 holidays 中返回 0）。纯字符串日期比较（YYYY-MM-DD 字典序即时间序），不做任何节假日推算。
2. 约定：**段长 ≥ 4 天视为长假**（国庆/春节/寒暑假级别；3 天以内小长假留校）。常量 `LONG_HOLIDAY_MIN_SPAN = 4` 放 persona.py，注释写明语义来源（与 V3 卡"短假留校、长假回家"锚点对应）。
3. `get_current_activity` 的 `is_holiday` 参数改为接收段长语义：签名调整为 `get_current_activity(self, hour, weekday=None, holiday_span: int = 0)`（**破坏性签名变更禁止**——保留 `is_holiday: bool = False` 关键字参数作兼容垫片，`is_holiday=True` 等价 `holiday_span=1`，已有调用方与测试不炸）：
   - `0 < span < 4`（短假）：维持现状，按周六作息匹配（留校，校园场景合理）；
   - `span >= 4`（长假）：**不匹配任何 daily_routine**，直接返回 `"放长假中，回绍兴老家陪父母，不在学校"`（这句文案与 V3 卡锚点一致，放模块级常量 `LONG_HOLIDAY_ACTIVITY`）。
4. 时间行附注分级（`companion/prompts.py`）：`HOLIDAY_PROMPT_NOTE` 拆两个常量：
   - `SHORT_HOLIDAY_PROMPT_NOTE = "，今天是法定节假日（学校放假，不上课）"`（文案与现状一致）；
   - `LONG_HOLIDAY_PROMPT_NOTE = "，今天是法定节假日（放长假，她不在学校）"`。
   调用方（`assembler.py`、`proactive.py` 共 3~4 处）按 span 选注。`HOLIDAY_PROMPT_NOTE` 保留为 SHORT 的别名（兼容现有引用与测试）。
5. **不做**：不区分寒暑假/国庆的具体节日名；不动 holidays 配置结构；不动周六作息本身；不处理"长假中段返校"等细颗粒状态。

**测试**：`holiday_span` 边界（当天不在列表/3 天段/8 天段/跨月相连如 09-30+10-01 算不算相连——约定**算**，按月日进位处理）；长假作息返回 LONG_HOLIDAY_ACTIVITY 且不遍历 routine；短假维持周六匹配；`is_holiday=True` 旧调用方式行为不变；assembler/proactive 长假钟注入 LONG 注。

## 任务 2：观察者 facts/followups 字典形态容忍（P1，证据 E11）

**规格**（`companion/observer.py`，沿用 FIXES10 容错原则——单项畸形丢单项，绝不炸整轮）：

1. **facts 项**：当前只接受 `str`。扩展为：若是 `dict`，依次尝试键 `"内容"`、`"content"`、`"fact"`、`"text"`，取第一个非空字符串值；取不到才按现状丢弃并告警。转换成功记 INFO（"已从字典形态提取 fact"）。
2. **followups 项**：扩展接受 `{"topic": str, "remind_after_hours": int|float}` 形态——`remind_after = 当前时间 + hours` 换算成现有时间格式入库（参照现有 followups 入库代码的时间处理方式）；`topic` 非字符串或缺失、`hours` 非正数时按现状丢弃。现有字符串形态与既有字段名形态的行为完全不变。
3. `OBSERVER_SYSTEM_PROMPT` 的 facts/followups 字段说明保持原文（规范形态仍是字符串/既有格式；本任务是容错不是改协议）。
4. **不做**：不扩展其他字段；不改 `done_followups`；不改观察者模型与调用方式。

**测试**：facts 字典四键各提取成功/无有效键丢弃；followups hours 形态换算正确（断言入库的 remind_after ≈ now+hours）；混合形态（字符串+字典同批）各自正确处理；畸形项不炸整轮结算。

---

## 负面清单（不许做）

1. 不动 `characters/`、`config.toml`、`scripts/benchmark_v4.py` 的指标口径；
2. 不新增配置字段；不改 holidays 既有语义（计费侧零影响）；
3. 不重构 observer/persona 其他部分；不加新依赖；
4. 不部署、不碰服务器、不动 main 分支；
5. 文字层/卡的一切内容不碰（B 场景叮嘱口径已由所有者另行拍板，与执行模型无关）。

## 验收 DoD

1. `./venv/Scripts/python.exe -m unittest discover -s tests` 全绿（319 + 本迭代新增）；
2. 本地沙箱真实 API 冒烟：
   - A 段（E10 回归）：holidays 注入连续 8 天段（含"今天"），触发一次主动消息或组装 system prompt，产出/提示词中**不得出现**校园作息词（银泉/临湖/琴房/玉泉/校车），且含长假提示；贴 transcript；
   - B 段（E11 回归）：mock 观察者模型返回 `{"facts": [{"内容": "他不吃香菜"}], "followups": [{"topic": "问他科创比赛", "remind_after_hours": 24}], ...}` 走一遍真实结算（结算本身可打真实 API 后用 mock 数据注入解析层），断言 fact 与 followup 正确入库；
3. 逐项 diff 摘要（按任务 1、2 分组）；
4. git：施工前 commit 现状，施工完一个 commit（message 前缀 `FIXES14:`）。
