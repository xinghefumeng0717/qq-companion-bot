# 修复任务书（FIXES）

> 本文件是针对当前代码库的一次性修复任务清单。项目施工蓝图为同目录 `PLAN.md`，其中所有公式/契约不变，本文件只列**缺陷修复**。另需按 `PLAN.md §7.5` 与 `§14 M4.5` 新增语音输入模块（规格以 PLAN.md 为准，本文不重复）。
>
> 全局约束：
> 1. 不引入新依赖（语音模块允许 `sherpa-onnx`，见 PLAN §0.2/§17）；
> 2. 所有提示词仍集中在 `companion/prompts.py`；所有 LLM 调用仍收敛在 `companion/gateway.py`；
> 3. 完成后运行全部测试：`./venv/Scripts/python.exe -m unittest discover -s tests -v`（Windows 下在项目根目录执行），全部通过才算完成；
> 4. 修复与现有测试冲突时，分析根因后同步修正实现与测试，并在交付说明中注明；
> 5. 发现本文件与 PLAN.md 矛盾时，停下来报告，不自行裁决。

## A. 缺陷修复清单

### 高优先级

**A1. LLM 故障兜底回复会被自己的旁白正则剥光，用户端静默**
- 位置：`companion/main.py` 约 204 行（LLM 调用失败的兜底回复 `"（发呆中，刚刚走神了……）"`），配合 `companion/replier.py` 19-21 行的旁白剥离正则。
- 问题：兜底文案本身是全角括号旁白，发送前被整块剥除，剥空后无任何消息发出，LLM 故障时表现为机器人完全没反应。
- 修复：兜底文案改为不含任何括号/星号的纯文字（例如"刚刚走神了……你再说一次？"），并确认剥离管道处理后仍有内容可发。

### 中优先级

**A2. 日记归档窗口与游标错位（数据正确性）**
- 位置：`companion/memory.py` 约 112-118 行。
- 问题：每 8 轮归档时取素材用"turns 表最近 16 条消息"，但主动消息（proactive）也写入 turns 表，导致归档窗口与 `archived_turns` 游标不严格对应，长期运行产生重复/遗漏日记。
- 修复：turns 表有自增 `id`，归档游标改用 id 区间——记录上次归档到的 `max(id)`，本次取 `id > 上次游标` 的记录按序取够 8 轮（user/assistant 成对计数口径与现状保持一致），归档后推进游标。`counters` 表 `archived_turns` 的语义相应改为"已归档到的 turn id"。注意兼容 `tests/test_m3_engines.py`，必要时同步更新。

**A3. 日记归档阻塞消息队列**
- 位置：`companion/memory.py` `save_turn_pair` 内 `await check_and_trigger_diary_archive()`。
- 问题：归档的 LLM 调用同步阻塞在聚合器消费队列里，每 8 轮对话主流程会卡顿数秒，违背"后台结算不阻塞下一轮"的设计（PLAN §1.1）。
- 修复：改为 `asyncio.create_task` 后台执行归档，并加一把 `asyncio.Lock` 防止并发归档；主流程不等待归档完成；归档异常只记日志不影响主流程。

**A4. 表情包上限与去重数据源不一致（会重复收藏）**
- 位置：`companion/stickers.py`（约 154 行上限判断用内存 index.json，161-165 行 md5 去重查 SQLite stickers 表）。
- 问题：角色卡自带初始表情只在 index.json、不在 SQLite，导致机主发一张库内已有的图时 md5 查不到，被重复收藏。
- 修复：启动加载时把 index.json 中的初始表情（文件存在则计算 md5）同步进 SQLite `stickers` 表；此后 200 张上限判断与 md5 去重统一查 SQLite。index.json 仅作为角色卡初始素材的声明文件。

### 低优先级（一并修复）

**A5.** `companion/observer.py` 约 88/99 行：LLM 返回 JSON 中 `moments` 或 `mood_impact` 为 `null`（JSON 合法）时抛 TypeError/AttributeError，本轮结算丢失。对两字段加 isinstance/None 防护，异常时取中性默认（空列表 / 零冲击 dict），符合 PLAN §8.4"失败全部取中性默认"契约。

**A6.** `companion/observer.py` 约 154-157 行：`done_followups` 用 `topic LIKE '%...%'` 子串匹配，topic 较短时（如"考试"）会误标记其他未完成事项。改为精确匹配；匹配不到时不标记并记日志。

**A7.** `companion/onebot.py` 约 177 行与 `companion/stickers.py` 约 70-71 行：下载图片固定存为 `.jpg`，PNG/GIF 原图与实际字节不符，转 base64 时 MIME 声明错误。修复：优先从消息段 file/url 推断扩展名；无法推断时按字节魔数判断（PNG `89 50 4E 47` / GIF `47 49 46` / JPEG `FF D8 FF`），兜底 `.jpg`；MIME 与实际格式一致。

**A8.** `companion/gateway.py` + `companion/main.py`：流式调用中途失败重试时从头重发，已累积的部分文本未丢弃，重试成功后回复前半段重复。修复：每次完整重试重新收集文本，只有最终成功的那次结果返回上层。

**A9.** `companion/safety.py` 16-25 行：`CRISIS_PROMPT`/`WATCH_PROMPT` 两段提示词移至 `prompts.py`（PLAN §6.1：所有提示词集中在 prompts.py），safety.py 改为从 prompts 导入。

**A10.** `companion/memory.py` 工作记忆截取：取最近 N 条后不保证 user/assistant 严格交替（主动消息插入后可能以 assistant 开头或连续两条 assistant）。修复：截取后若首条为 assistant 则裁掉到以 user 开头为止。

**A11.** 清理 `data/` 下残留的 6 个 `test_m6_*.db` 测试产物；检查各测试的 tearDown，临时测试文件应在测试结束后自清理（用临时目录或唯一文件名，避免 Windows 下文件锁导致 WinError 32）。

## B. 语音输入模块

按 `PLAN.md §7.5`（功能规格）与 `§14 M4.5`（验收标准）实现，要点复述：

- 新建 `companion/voice.py`：record 消息段下载 → ffmpeg 转码（`ffmpeg -y -i in.silk -ar 16000 -ac 1 out.wav`；ffmpeg 缺失/转码失败降级为占位符 `[对方发来一条语音，但没能听清]`）→ sherpa-onnx SenseVoice int8 本地识别（`asyncio.to_thread` 包裹，不阻塞事件循环；模型文件缺失时优雅降级并在日志提示下载方式）→ 识别文本加"（语音消息）"前缀进聚合器 → 临时音频文件处理完即删除；
- `config.example.toml` 与 `config.py` 增加 `[voice] enabled / model_dir`；`requirements.txt` 加 `sherpa-onnx`（注释注明仅语音输入用）；
- `onebot.py`/`main.py` 接入 record 消息段路由；
- 新增 `tests/test_m45_voice.py`：unittest + mock（mock 掉 ffmpeg 子进程与 sherpa 识别器，**不要真实下载约 200MB 的模型文件**），覆盖：正常链路、ffmpeg 缺失降级、识别为空降级、配置关闭时占位符。测试风格参照现有 `tests/`。

## C. 交付要求

- 修复完成后输出逐项结论（文件 + 怎么改的）；
- 全部测试（含既有 7 个测试文件 + 新增 test_m45_voice.py）运行通过；
- 报告任何未能完成或需要人工决定的事项。
