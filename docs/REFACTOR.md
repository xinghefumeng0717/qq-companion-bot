# 重构任务书（REFACTOR）：优雅重构（行为零变化）

> 项目：QQ 伴侣机器人（D:/qq-companion）。前置阅读：`AGENTS.md`、`STATUS.md`、本文件。
> 性质：**纯重构，不加功能、不改行为**。这是 FIXES8 上线后、最终部署+reset 前的代码质量收拾。
>
> 所有者已拍板的决策（不许再翻案讨论，直接执行）：
> 1. 范围 = companion/ 生产代码 + tests/ 测试样板 + scripts/sim/score_simulation.py 的公式引用；**launcher/ 不动**；
> 2. 死代码（status.py、data/dashboard_dump.html、未用 import、写了从不读的属性）**直接删除**，git 历史即备份；
> 3. 两个真 bug（admin 缺 `import asyncio`、看板遗忘公式分叉）已由 Kimi 在重构前单独修复并 commit（`bb5ab65`），**不属于本任务书范围，不要重复修、不要回滚**；
> 4. 遗忘曲线公式以 `memory.py` 生产公式（`tau_base = max(10.0, importance * 6.8)`）为唯一基准，admin/仿真/测试三处拷贝统一向它引用。
>
> 全局约束（高压线，违反即返工）：
> 1. **行为零变化**：完成后 `./venv/Scripts/python.exe -m unittest discover -s tests` 92 个用例全绿；看板 7 个 HTML 页面 + `/api/status` 与重构前快照 diff 为空（归一化规则见任务 0）；对外 API 字段集合逐字不变；
> 2. **禁止改动 `characters/` 任何文件、`config.toml`**；
> 3. **禁止引入任何新依赖**（不装模板引擎，看板维持字符串拼 HTML 架构，只是搬家）；
> 4. 只允许本文件列出的改动点；每完成一个任务**单独 commit**一次（信息格式：`REFACTOR任务N：……`）；
> 5. 重构期间**不做任何部署**；全部完成后由 Kimi 终审、所有者确认，才进入最终部署流程；
> 6. 交付：逐项 diff 摘要 + 测试输出 + 看板快照 diff 报告（必须为空 diff）。

---

## 任务 0：建立零变化基线（先做这个，别的都靠它兜底）

1. 写 `scripts/util/snapshot_dashboard.py`（新工具脚本，不算业务代码）：
   - 参考 `tests/test_m6_admin.py:22-68` 的引擎栈组装方式，用 `data/companion.db` 的**临时副本**（复制到 `data/snapshot_tmp.db`，用完删）启动 AdminServer（端口用一个空闲端口如 18080）；
   - 用 aiohttp 抓取 7 个页面（`/`、`/memory`、`/debug`、`/costs`、`/stickers`、`/logs`、`/admin`）+ `/api/status`，存入指定目录；
   - `--save <dir>` 抓快照；`--diff <dir_a> <dir_b>` 对比：先做归一化再逐字节 diff。归一化规则（正则替换为占位符）：
     - 所有 `\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?` 时间戳 → `<TS>`；
     - `"uptime_minutes": \d+` → 固定值；
     - `他已有 \d+ 小时没说话了` → `<HOURS>`；
     - 日志页 `/logs` 整体豁免（内容随运行变化），只验证 HTTP 200 与页面骨架（含 `log-window`）。
2. 运行一次抓取存为 `snapshots/baseline/`（此目录加进 .gitignore 或用完即删，不入库），作为本任务书全程的对照基准；
3. **两次快照必须在同一天内运行**（今日费用等日期敏感字段才一致）；
4. 确认 92 测试全绿后 commit（`REFACTOR任务0：看板快照工具与零变化基线`）。

## 任务 1：admin.py 三段拆分（巨石解体，纯搬运）

`companion/admin.py`（1378 行）拆为两个模块，**handler 逻辑逐行不动，只搬家**：

- 新建 `companion/admin_render.py`：`HTML_STYLE`（33-356 行，**保持 Python 字符串常量，不抽 .css 文件**——避免启动时文件 IO 的路径依赖）、`format_chinese_date`、`get_sentiment_color`、`render_section_header`、`render_radar_svg`、`render_nav`、`html_shell`（359-503 行）原样移入；
- `admin.py` 保留 `AdminServer` 类与全部 12 个 handler，顶部 `from companion.admin_render import ...`；为保持外部 import 面不变（tests 里直接 import 了这些名字），admin.py 顶部对被移走的名字做**显式 re-export**；
- 顺手去重：今日费用 SQL 在 admin.py 出现 3 次（589-594、1038-1043、1206-1211），提取为 `AdminServer._today_cost()` 私有方法，三处替换；
- 验收：92 测试全绿 + `snapshot_dashboard.py --diff snapshots/baseline` 空 diff。

## 任务 2：state 表 KV-JSON 读写收口（db.py）

- 现状：affection.py:85-109、mood.py:25-52、proactive.py:83-111 三处近乎逐行相同的 `SELECT value FROM state WHERE key=?` + `INSERT OR REPLACE` JSON 读写；
- 在 `db.py` 增加一对方法（如 `get_state_json(key, default)` / `set_state_json(key, value)`），三处引擎改为调用它们。**默认值、序列化格式、异常兜底语义逐字节对齐现有实现**（三方若有细微差异，以当前各自行为为准逐处保留——不确定就问，不许"顺便统一"）；
- 状态 key 魔法字符串（`'affection'`/`'mood'`/`'unanswered_proactive'`）与 counter key（`'total_turns'`/`'archived_turns'`）散落 db.py:195-203、reset.py:66-72、memory.py:102/131、admin.py:682、status.py（将被任务 5 删除）等处：在 db.py 顶部集中定义为常量，各引用处替换；
- 验收：92 测试全绿。

## 任务 3：遗忘曲线公式单点化（memory.py）

- 现状：同一公式 4 份拷贝——memory.py:335-343（生产基准）、admin.py:874-882（bugfix 后已对齐）、scripts/sim/score_simulation.py:281-283、tests/test_fixes8.py:94-103；
- 在 memory.py 提取公共函数（建议签名：`calc_diary_strength(importance, recall_count, sentiment, days) -> tuple[float, float]`，返回 strength 与 tau_effective，具体以现有代码为准），四处全部改为 import 调用；
- tests/test_fixes8.py 里的拷贝改成 import 后，**原断言不许删**——它就地从"抄公式"升级为"回归检测器"；
- 验收：92 测试全绿 + `scripts/sim/score_simulation.py` 跑一遍，输出与重构前一致（同一天运行对比）。

## 任务 4：两个小提取（JSON 容错解析 + 时间解析）

- **JSON 容错解析**：observer.py:224-228 与 memory.py:217-221 逐字相同的 `json.loads` → 失败剥 ```` ```json ```` → 再 parse 逻辑，提取为 `gateway.py` 的模块级函数（如 `parse_llm_json`，LLM 输出的善后属网关职责），两处调用替换；
- **TIME_FORMAT 收敛**：db.py:13 定义了 `TIME_FORMAT = "%Y-%m-%d %H:%M"` 但 12 处硬编码字面量（mood.py:60/84、affection.py:128、memory.py:330、proactive.py:137/162/180、assembler.py:128、admin.py:621/623/869 等），全部改引用常量；8 处相同的 `try: datetime.strptime(...) except Exception:` 样板提取为 db.py 的 `parse_dt(s) -> Optional[datetime]`（**保留每处原有的失败兜底语义**——有的回 None、有的回 0.0 天，调用处各自处理）；
- 注意 admin.py:621-623 有两段式 strptime（先 19 字符再 16 字符），是变体，提取时保持行为等价；
- 验收：92 测试全绿 + 看板快照空 diff。

## 任务 5：死代码清除

逐项执行，每项删完跑一遍测试：

1. **删除 `companion/status.py` 整个文件**（被看板取代的遗留 CLI，内含旧版 4 阶段名表）。删前 `grep -rn "status" companion/ launcher/ scripts/ tests/ --include=*.py | grep -i "import\|from"` 确认无引用；特别验证 `main.py --status`（main.py:47-97 是自己的 `print_status` 实现，不依赖 status.py——若实际有依赖，先改 main.py 再删）；
2. **删除 `data/dashboard_dump.html`**（FIXES5B 删夜主题前的旧页面 dump，垃圾文件）；
3. **未使用 import**：admin.py:17 `calc_composite_score`、main.py:17 `calc_composite_score`、main.py:32 `image_to_base64_data_url`、chat.py:12-13 多余名字、gateway.py:11 `Tuple`（以实际 lint 为准，可用 `python -m pyflakes` 或人工核对，只删确认无引用的）；
4. **onebot.py:87/169 `_last_heartbeat`**：写了从不读，删除赋值与声明；
5. **aggregator.py:58 与 onebot.py:169 的 `asyncio.get_event_loop()`** → `get_running_loop()`（协程内二者等价，消 DeprecationWarning）；
6. **LLMConfig 兼容层（config.py:113-202）不删**——构造签名是对外面，只在其上方加一行 deprecated 注释。

**明确不许动的两处**：
- `turn_handler.py:84` 的 `image_data_url = None` **不是死代码，是承重墙**——两段式视觉设计（flash 看图→描述注入 pro）靠它丢弃 data URL，删了会激活 assembler.py:232-238 的多模态分支，改变主对话行为。在其上方加一行注释说明设计意图即可；
- assembler.py:232-238 多模态分支在生产路径不可达，但属能力储备，**保留**（若发现 chat.py 沙箱路径可达，更必须保留）。

## 任务 6：chat.py 流水线去重 + 状态渲染收敛

- chat.py 的 `handle_input`（89-121）与 `run_chat` 主循环内联段（187-214）是同一段"流式调用→切段→落库→加固→结算"的两份拷贝，提取为一个方法，两处调用；
- 状态渲染三处收敛：status.py 删除后剩 `main.py print_status` 与 `chat.py get_status_str` 两处，合并为一个共享函数（放哪由执行方定，建议随主要使用方），两处调用；
- **chat.py 无直接 unittest 覆盖**，额外验收：改完跑 `python -m companion.chat --help`（或其实际 CLI 入口）确认能启动；沙箱对话冒烟由所有者后续人工跑一次；
- 验收：92 测试全绿。

## 任务 7：测试样板提取（tests/helpers.py）

- 新建 `tests/helpers.py`，提取三件公共物：
  1. `make_db()`：临时库 setup/teardown（现状 6 份：test_m3_engines.py:23-33、test_fixes8.py:30-40、test_fixes7.py:78-102/233-245、test_m7_integration.py:21-37、test_m6_admin.py:22-25）；
  2. `make_engine_stack()`：Persona(example)+六引擎组装（现状 3 份：test_m6_admin.py:27-47、test_m7_integration.py:28-32、test_fixes4.py:231-240）；
  3. `make_mock_gateway()`：MagicMock gateway + AsyncMock chat（现状 5 份）；
- **注意**：tests/ 无 `__init__.py`，`unittest discover -s tests` 会把 tests 目录加进 sys.path，用 `from helpers import ...` 导入；先写一个最小 helper 跑通发现机制，再批量替换；各测试文件的细微差异（`:memory:` vs 文件库、路径命名）逐处保留其现有语义，helper 加参数容纳，**不许为了统一而改变任何测试的断言语义**；
- 验收：92 测试全绿（数量不减、断言不变）。

---

## 负面清单（明确不做）

- 不引入模板引擎 / 静态资源目录 / 任何新依赖；
- 不动 `launcher/`（含 qingzi_home.py 的硬编码路径，记入 STATUS.md 小尾巴即可）；
- 不做 asyncio 任务引用统一管理改造（fire-and-forget 加引用会延长任务生命周期，属行为变更；已知的 memory.py:106 / turn_handler.py:126 / admin.py:1308 三处维持现状）；
- 不动 prompts.py 的任何提示词文本（import 调整除外）；
- 不动 config.py 的配置语义与 config.toml 的字段结构；
- 不"顺手"统一各引擎的异常兜底策略（三档吞咽策略维持现状，那是行为）；
- 不部署、不碰服务器。

## 完成定义（DoD）

1. 任务 0-7 全部完成，每任务一个 commit；
2. `./venv/Scripts/python.exe -m unittest discover -s tests` 92 全绿；
3. `snapshot_dashboard.py --diff snapshots/baseline snapshots/after` 输出为空 diff（7 页 + API）；
4. `/api/status` 的 JSON key 集合与基线逐字一致；
5. `git diff --stat 4453bfc..HEAD` 摘要附在交付报告中；
6. 交付报告交给 Kimi 终审，通过后由所有者确认，才进入 STATUS.md 的最终收尾流程（部署→拉回备份→reset）。
