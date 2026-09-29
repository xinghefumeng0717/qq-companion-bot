# 迭代任务书（FIXES3）：模型预设化 + 仿真对话 + 数据重置 + 仪表盘增强

> 项目：QQ 伴侣机器人（根目录 D:/qq-companion，蓝图 PLAN.md，前序任务 FIXES.md / FIXES2.md 已完成）。
> 本次 4 个任务均为增量迭代，不破坏既有功能。
>
> 全局约束：
> 1. 不引入新第三方依赖（维持 aiohttp / aiosqlite / Pillow / sherpa-onnx）；
> 2. 所有提示词仍集中在 `companion/prompts.py`；所有 LLM 调用仍收敛在 `companion/gateway.py`；
> 3. **config.toml 里已有的 api_key、QQ 号等真实配置一个字符都不许丢**（重构配置结构时原样迁移）；
> 4. 完成后运行 `./venv/Scripts/python.exe -m unittest discover -s tests -v`（项目根目录），全部通过才算完成；
> 5. 与本文件或 PLAN.md 矛盾时停下报告，不自行裁决。

---

## 任务 1：模型预设化配置

**目标**：换模型厂商从"改 5 处"变成"改 1 行"。

### 1.1 配置结构（改写 config.example.toml 与 config.toml 的 [llm] 段）

```toml
[llm]
current = "deepseek"          # 当前激活的模型预设，切换只改这一行
thinking_chat = true
thinking_effort_chat = "low"
thinking_tasks = false
thinking_effort_tasks = "low"

# 每个 [models.<名>] 是一家厂商的完整档案
[models.deepseek]
provider = "deepseek"         # deepseek / minimax / openai（generic，不传任何思考参数）
base_url = "https://api.deepseek.com"
api_key = "sk-（保留原有 key）"
chat = "deepseek-v4-pro"
vision = "deepseek-flash"     # 留空字符串则禁用识图
tasks = "deepseek-flash"

[models.minimax]
provider = "minimax"
base_url = "https://api.minimaxi.com/v1"
api_key = "（从 data/test_keys.toml 的 minimax 字段取）"
chat = "MiniMax-M3"
vision = "MiniMax-M3"
tasks = "MiniMax-M2.7"

[models.seed]
provider = "openai"
base_url = "https://ark.cn-beijing.volces.com/api/v3"
api_key = "（从 data/test_keys.toml 的 seed 字段取）"
chat = "doubao-seed-2-1-pro-260915"
vision = "doubao-seed-2-1-pro-260915"
tasks = "doubao-seed-2-1-turbo-260628"
```

`[llm.pricing]` 段保持现状不动（价目本来就按模型名匹配）。

### 1.2 config.py 改造

- 新增 `ModelPreset` dataclass（provider/base_url/api_key/chat/vision/tasks）；
- `LLMConfig` 改为持有 `current: str`、`presets: Dict[str, ModelPreset]`、思考配置与 pricing（不动）；
- 提供 `LLMConfig.active() -> ModelPreset` 返回当前预设；
- `api_key / base_url / text_model / vision_model / observer_model` 这些旧访问点改为转发到 `active()` 的兼容属性（标注 deprecated），避免全项目大改；
- `current` 指向不存在的预设名时启动报错并列出可用预设。

### 1.3 gateway.py 按 provider 分流

- `apply_thinking()` 仅在 `provider == "deepseek"` 时调用（维持现有行为：chat 开 low、tasks 关）；
- `provider == "minimax"` 时：不传 thinking 参数，改为在 payload 加 `"reasoning_split": true`（把思考过程隔离到独立字段，防止 `<think>` 标签混入正文发到 QQ）；
- `provider == "openai"`（generic）：不传任何思考/推理参数；
- 视觉路由改用 `active().vision`，空字符串时走原有的禁用降级路径。

### 1.4 验收

- 现有测试全绿；新增测试：预设解析、current 切换、minimax payload 含 reasoning_split 且不含 thinking、generic provider 不附带任何推理参数、current 指向错误时报错信息包含可用预设名。

---

## 任务 2：仿真对话 CLI（companion/chat.py）

**目标**：在终端和青梓正常聊天验证文笔，对生产数据**零副作用**。

### 实现（沙箱副本方案，禁止逐模块改造）

1. `python -m companion.chat` 启动时：把 `data/companion.db` **复制**为 `data/chat-sandbox.db`，全部引擎（db/affection/mood/memory/assembler/gateway/observer）指向沙箱副本——这样观察者、日记、情绪全部照常运转，但一滴都落不到生产库；
2. 终端循环：输入文本 → `assembler.assemble_messages(text)` → `gateway.stream_chat` 流式打印 → 走 observer 正常结算（落沙箱）→ 循环；
3. 内置命令：`/quit` 退出、`/prompt` 打印最近一次完整 system prompt、`/status` 打印当前好感度六维与 PAD 值；
4. 退出时删除沙箱文件；
5. 生产库不存在时（新部署）友好报错提示先正常聊过再仿真。

### 验收

- 仿真聊 3 轮后退出，检查 `data/companion.db` 的 turns 表行数与启动前一致（新增测试覆盖这一点）。

---

## 任务 3：数据重置工具（companion/reset.py）

**目标**：一键清空关系数据，从头开始相处。

### 实现

1. `python -m companion.reset`：交互式确认（输入 `YES` 才继续）；
2. **先备份**：复制 `data/companion.db` → `data/backup/companion-YYYYMMDD-HHMMSS.db`（目录不存在则创建）；
3. 清空：`turns / diary / diary_archive / facts / followups / suppressed_desires / observer_scores / milestones` 全表 DELETE；
4. `counters` 的 `total_turns`、`archived_turns` 归零；
5. `state` 表全表 DELETE（好感度与情绪状态由引擎下次访问时按角色卡 initial_dims / 默认值自动重建，不要手工塞值）；
6. **保留**：`stickers` 表（表情包收藏是资产）、`llm_calls` 表（计费历史）。可选 `--purge-all` 连这两张也清；
7. 打印摘要：备份路径 + 各表清除行数。

### 验收

- 新增测试：往测试库塞数据 → reset → 断言目标表为空、stickers/llm_calls 保留、备份文件存在且内容等于重置前。

---

## 任务 4：仪表盘增强（admin.py，A+C+D 三项）

**风格约束**：纯服务端渲染，深色内联 CSS，**禁止任何 CDN/JS 框架**（雷达图用 Python 算坐标生成内联 SVG，不用 ECharts）。页面 30 秒自动刷新维持现状。

### 4A. 总览页加六维雷达图 + 阶段进度条

- 六维（warmth/trust/intimacy/intrigue/patience/tension）以 0~100 映射为六边形雷达图：Python 计算多边形顶点坐标，输出 `<svg>` 内联（底色网格 + 数据多边形 + 顶点数值标注）；
- 阶段进度条：当前复合分、当前阶段名、距下一阶段门槛还差多少分（门槛表见 affection.STAGE_THRESHOLDS），用 div 宽度百分比渲染。

### 4C. 新增 /logs 路由

- 读取 `data/logs/bot.log` 最后 200 行，HTML 转义后 `<pre>` 展示；文件不存在时显示友好提示；导航栏加入口。

### 4D. 总览页加"关系档案"卡片

- 认识天数（turns 表最早 `created_at` 到今天）；
- 累计对话轮数（counters.total_turns）；
- 里程碑时间线（milestones 表：阶段号 + reached_at，倒序展示，阶段名从角色卡取）。

### 验收

- 现有 test_m6 全绿并扩展：雷达图 SVG 片段存在、/logs 路由 200、关系档案字段渲染正确。

---

## 交付要求

1. 逐项结论（文件 + 怎么改的）；
2. 全量测试（含新增）通过；
3. 更新 README：chat/reset 用法、模型预设切换方法；
4. 报告未能完成或需人工决定的事项；
5. **不要**动 characters/qingzi/character.json（角色卡另有专人维护）。
