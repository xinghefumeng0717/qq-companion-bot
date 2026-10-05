# QQ 伴侣机器人 · 可执行施工蓝图（PLAN）

> 本文档是一份**自包含**的可执行规划。执行者无需任何外部背景知识，严格按本文档实施即可。
> 目标读者：执行能力强的 AI 编码代理。所有公式、参数、提示词、表结构均已给出确定值，**不要自行发挥修改**。

---

## 0. 项目概述

在腾讯云 Ubuntu 22.04 服务器上部署一个 24 小时运行的"QQ 角色扮演伴侣机器人"。

- 机主用自己的 QQ 大号与机器人登录的 QQ 小号私聊；
- 机器人扮演一个由"角色卡"定义的角色，拥有好感度、情绪、记忆、主动发消息、收发图片/表情包的能力；
- 项目将发布到 GitHub，**人设与代码彻底分离**：代码仓库不包含任何具体人设，角色通过外置角色卡 JSON 配置。

### 0.1 第一设计原则：手机聊天感（最高优先级）

机器人必须像一个真人在手机 QQ 上聊天，**绝对禁止**"小说式角色扮演"风格：

- 禁止输出任何动作/心理/场景描写，禁止 `（低头）`、`*笑了笑*`、`【内心】` 等一切旁白；
- 回复必须是纯聊天文字，允许语气词、emoji、短句；
- 长回复拆成多条短消息发送，模拟真人打字节奏；
- 情绪的表达渠道只有四个：措辞、标点、emoji、表情包图片。

### 0.2 技术约束

- 业务代码 **100% Python**（Python ≥ 3.11），除下方"允许依赖"外不引入任何第三方库；
- 允许依赖：`aiohttp`（HTTP/WebSocket 客户端 + 状态网页服务端）、`aiosqlite`（异步 SQLite）、`Pillow`（仅用于图像缩放，见 §7.1）、`sherpa-onnx`（仅用于语音输入识别，见 §7.5）。配置解析用标准库 `tomllib`；系统工具 ffmpeg（apt 安装）仅用于语音转码，不算项目组件；
- 不使用：MySQL、Redis、任何 ORM、任何 Web 框架、任何 Agent 框架、Node.js 业务代码；
- 唯一的非 Python 组件是 QQ 协议端 NapCat（黑盒，Docker 部署，见 §11）；
- 代码注释使用中文，仅在必要时书写；全部使用类型注解；异步统一使用 asyncio。

---

## 1. 总体架构

```
机主 QQ 大号
    ⇅  （腾讯 QQ 服务器）
NapCat（协议端，Docker，挂小号，输出 OneBot v11 WebSocket）
    ⇅  ws://127.0.0.1:3001 （JSON over WebSocket）
Python 机器人进程（本项目唯一开发的程序）
    ├─ OneBot 客户端：收发消息、心跳、断线重连
    ├─ 消息聚合器：连发短消息合并成一轮
    ├─ 提示词组装器：角色卡 + 记忆 + 情绪 + 好感度 + 事实
    ├─ LLM 网关：DeepSeek 文本/视觉模型调用、流式、重试、计费
    ├─ 观察者：每轮对话后的 JSON 结算（好感度/情绪/记忆/待跟进）
    ├─ 好感度引擎（六维向量，纯数学）
    ├─ 情绪引擎（PAD 三维连续值，纯数学）
    ├─ 记忆系统（工作/情景/语义三层，遗忘曲线）
    ├─ 主动消息调度器（asyncio 定时任务，三层决策）
    ├─ 表情包系统（图库、收藏、发送）
    └─ 状态仪表盘（aiohttp HTTP，绑定 127.0.0.1:8080）
    ⇅  HTTPS
DeepSeek API（文本模型 + 视觉模型）
    
存储：SQLite 单文件（data/companion.db）+ 角色卡目录 + 表情包目录
守护：systemd（机器人进程）+ Docker restart 策略（NapCat）
```

### 1.1 核心数据流（一轮对话）

1. OneBot 客户端收到私聊消息事件 → 进入聚合器；
2. 聚合器等待静默窗口（见 §4），把连续多条合并为一轮（图片消息单独处理，见 §7）；
3. 提示词组装器按 §6.2 的顺序拼装 system prompt + 工作记忆；
4. LLM 网关流式调用文本模型 → 回复器按 §5 切段、剥离旁白标记、解析 `[sticker:xxx]` 标记 → 逐条发送，段间随机延迟；
5. 本轮（用户消息 + 机器人回复）写入工作记忆表；
6. **观察者**异步结算（不阻塞下一轮）：一次 JSON 调用产出好感度评分、情绪冲击、记忆事实、待跟进事项、表情包收藏意愿 → 各引擎按 §8 公式更新 SQLite；
7. 每 8 轮对话触发一次日记归档（LLM 写第一人称日记）。

主动消息调度器是独立协程，与上述流程并行，见 §9。

---

## 2. 服务器环境准备

1. Ubuntu 22.04，安全组仅放行 22（SSH）。**不放行任何其他端口**（状态网页走 SSH 隧道，见 §12）；
2. 安装 Python 3.11+、pip、venv、Docker；
3. 创建项目目录 `/opt/qq-companion`，python -m venv venv，安装 `aiohttp aiosqlite`；
4. 部署 NapCat（§11），扫码登录 QQ 小号；
5. 机器人以 systemd 服务运行（§11.3）。

---

## 3. 项目目录结构

```
/opt/qq-companion/
├── PLAN.md                     # 本文档
├── README.md                   # 项目说明（GitHub 用，含角色卡填写教程）
├── requirements.txt            # aiohttp, aiosqlite
├── config.example.toml         # 配置模板（入库）
├── config.toml                 # 真实配置（.gitignore，含 API key）
├── characters/                 # 角色卡目录（入库的是 example，私有角色卡 gitignore）
│   └── example/
│       ├── character.json      # 角色卡（§3.2）
│       └── stickers/           # 该角色的表情包库（开局精选 + 运行时收藏）
├── companion/
│   ├── __init__.py
│   ├── main.py                 # 入口：装配所有模块、启动协程
│   ├── config.py               # tomllib 加载配置
│   ├── db.py                   # aiosqlite 连接与建表（§10）
│   ├── onebot.py               # OneBot v11 WebSocket 客户端
│   ├── aggregator.py           # 消息聚合
│   ├── persona.py              # 角色卡加载与渲染
│   ├── prompts.py              # 全部提示词模板集中在此文件
│   ├── gateway.py              # DeepSeek API 网关（文本/视觉/流式/重试/计费）
│   ├── assembler.py            # 提示词组装器
│   ├── replier.py              # 回复管道：切段、旁白剥离、sticker 解析、分段发送
│   ├── observer.py             # 观察者 JSON 结算
│   ├── affection.py            # 好感度引擎
│   ├── mood.py                 # 情绪引擎
│   ├── memory.py               # 三层记忆 + 遗忘曲线
│   ├── proactive.py            # 主动消息调度器
│   ├── stickers.py             # 表情包系统
│   ├── voice.py                # 语音输入（SILK→WAV→SenseVoice 识别，§7.5）
│   ├── safety.py               # 安全兜底（关键词三档）
│   └── admin.py                # 状态仪表盘 HTTP 服务
├── data/                       # 运行数据（.gitignore）
│   ├── companion.db
│   └── logs/
└── deploy/
    └── qq-companion.service    # systemd 单元
```

### 3.1 配置文件（config.example.toml）

```toml
[account]
allowed_user_id = 123456789        # 机主大号 QQ 号，只响应此人，其余一律忽略
bot_qq = 987654321                 # 小号 QQ 号（日志用）

[onebot]
ws_url = "ws://127.0.0.1:3001"
access_token = "在这里填 NapCat 设置的 token"

[llm]
api_key = "sk-在这里填"
base_url = "https://api.deepseek.com"
text_model = "deepseek-chat"            # 文本模型名，可换
vision_model = "deepseek-v4-flash-vision-exp"  # 视觉模型名，留空则禁用识图
observer_model = "deepseek-chat"        # 观察者用便宜模型

[character]
path = "characters/example"        # 当前激活的角色卡目录

[reply]
max_chunks = 5                     # 单轮最多拆几条发送
chunk_delay_min = 0.8              # 段间延迟秒数下限
chunk_delay_max = 2.2

[proactive]
enabled = true
wake_interval_min = 20             # 唤醒间隔（分钟）下限，每次随机取 [min,max]
wake_interval_max = 40
quiet_hours = [0, 8]               # 免打扰时段
max_unanswered = 2                 # 连续 N 条主动消息未获回复，当天停止

[admin]
host = "127.0.0.1"
port = 8080
```

### 3.2 角色卡 schema（characters/example/character.json）

角色卡是**唯一**承载人设的地方。代码中不允许出现任何具体角色名、具体语气文案。

```json
{
  "_schema": 1,
  "name": "示例角色",
  "user_address": "你",                    // 她如何称呼机主
  "core_description": "……一段话描述她是谁、她的世界、她与机主的关系……",
  "chat_style": {
    "rules": [
      "你在用手机QQ聊天，只输出聊天文字本身",
      "说话简短口语化，常用语气词",
      "开心时会连发两三条短消息",
      "……角色具体的口癖、标点习惯、emoji 使用习惯……"
    ],
    "good_examples": ["哈哈哈哈哈真的假的", "在呢，刚开完会", "今天有点累……"],
    "plain_examples": ["机主：「在干嘛」→ 她：「吃饭」"],
    "bad_examples": ["（笑了笑）今天很开心", "*揉眼睛* 我刚睡醒"]
  },
  "initial_dims": { "warmth": 40.0, "trust": 50.0, "intimacy": 35.0, "intrigue": 30.0, "patience": 50.0, "tension": 3.0 },
  "stages": [
    { "name": "阶段0名", "tone": "礼貌而疏远",
      "instructions": ["对你还有些生疏，语气礼貌客气", "对话简短，不会主动延伸话题"] },
    { "name": "阶段1名", "tone": "……", "instructions": ["……"] }
  ],
  "daily_routine": [
    { "start": 6, "end": 8, "activity": "刚起床，在窗边喝第一杯茶" },
    { "start": 23, "end": 6, "activity": "已经睡下了" }
  ],  "personal_memories": [
    { "title": "……", "content": "……", "emotion": "温暖" }
  ],
  "habits": ["思考时会咬笔帽"],
  "stickers_dir": "stickers"
}
```

- `stages` 必须恰好 10 项，对应好感度阶段 0~9（§8.1），仓库内 example 用**通用版**文案；每项可选 `examples` 字段（该阶段的对话示范，格式 `"机主：「…」→ 她：「…」"`，注入阶段块末尾作为语气示范，近因效应对阶段分寸的校准强于纯指令）；
- `daily_routine` 覆盖全天 24 小时（允许跨零点段），只作为"她此刻在做什么"的事实注入，**不参与数值计算**。每条可选 `days` 字段（如 `"days": [0, 2]` 表示周一/周三，0=周一）：全部条目都带 `days` 时按星期几区分作息；不带 `days` 的条目视为每日通用兜底；
- `personal_memories` / `habits` 注入角色背景段，供模型参考，不入记忆系统。

---

## 4. OneBot 客户端与消息聚合（onebot.py / aggregator.py）

### 4.1 OneBot v11 WebSocket 客户端

- 使用 `aiohttp.ClientSession().ws_connect()` 连接 `ws_url`，请求头带 `Authorization: Bearer <access_token>`（token 为空则不带）；
- 收到 JSON：若 `post_type == "message"` 且 `message_type == "private"` 且 `user_id == allowed_user_id` → 交给聚合器；其余事件忽略（但 `meta_event` 心跳要更新存活时间戳）；
- 发送：构造 `{"action": "send_msg", "params": {"message_type": "private", "user_id": ..., "message": [...消息段...]}, "echo": <uuid>}`，等待带相同 echo 的响应帧；
- 消息段格式：文本 `{"type":"text","data":{"text":...}}`；图片 `{"type":"image","data":{"file":"file:///绝对路径"}}`；
- 断线重连：指数退避（1s→2s→…→上限 60s），重连成功后记日志。所有发送失败重试 2 次；
- 收到图片消息段时，`data.url` 是图片下载地址，用 aiohttp 下载到 `data/images/`，记录本地路径。

### 4.2 消息聚合器

真人习惯连发多条短消息。规则：

- 收到一条文本消息后启动/重置一个 **3 秒静默计时器**；期间又收到新消息则重置；
- 静默满 3 秒，或累计等待达到 8 秒（硬上限），把缓冲的所有消息按序合并（用换行连接）作为一轮用户输入；
- 若缓冲中包含图片消息：图片不等待聚合，单独触发一轮（见 §7）；
- 机器人正在生成回复期间收到新消息：照常缓冲，本轮回复发完后立即处理下一轮（不丢弃、不插队）。

---

## 5. 回复管道（replier.py）

1. 从 LLM 网关拿到**流式**回复全文；
2. **旁白剥离兜底**：用正则 `（[^（）\n]{1,40}）|\([^()\n]{1,40}\)|\*[^*\n]{1,40}\*` 删除疑似动作描写段，删除发生时记 WARNING 日志（用于判断提示词是否需要加强）；被剥空的段落丢弃；
3. **sticker 标记解析**：全文查找 `[sticker:描述词]`，命中则在 stickers 库中按描述词匹配（§7.3），把该段替换为图片消息段；未命中则丢弃该标记；
4. **切段**：按 `。！？!?\n~～` 切句，相邻短句（合计 ≤ 15 字）合并，每段 1~2 句，总段数 ≤ `max_chunks`，超出部分并入最后一段；
5. **分段发送**：每段一条私聊消息；段间延迟 = `uniform(chunk_delay_min, chunk_delay_max)` + 每字 0.03 秒（模拟打字），首段延迟减半；
6. 本轮的（用户输入, 机器人回复纯文本）交给 memory.save_turn 落库，然后触发观察者。

---

## 6. 提示词体系（prompts.py / assembler.py）

### 6.1 总则

- 所有提示词模板集中在 `prompts.py`，用 `str.format` 渲染，禁止散落各模块；
- 每个状态段落生成函数用 try/except 包裹，单块失败返回空串并记日志，**绝不影响主对话**。

### 6.2 system prompt 组装顺序（每轮对话实时拼装）

```
【角色】{core_description}（含 personal_memories、habits 的自然语言化）
【聊天规则】你在用手机QQ和{user_address}聊天。只输出聊天文字本身。
  绝对禁止输出任何动作、心理、场景描写，禁止使用括号或星号旁白。
  你的情绪只能通过措辞、语气词、标点和emoji表达。
  正确示范：{good_examples 逐条列出}
  错误示范（禁止）：{bad_examples 逐条列出}
  她的具体说话习惯：{chat_style.rules 逐条列出}
  可发送表情包：在需要时于消息中单独一行输出 [sticker:描述词]（可用描述：{贴纸库描述词列表}），不要滥用，整轮最多一次。
【当前关系阶段】{§8.1 阶段名+tone+instructions}
【她此刻】{作息表当前活动}；{情绪翻译（§8.2）}；{信任分层描述}；{冷落/挫败描述（如有）}
【事实】现在是 {YYYY-MM-DD HH:mm 星期X}；距上次聊天已 {N} 小时（如适用）
【关于他】{语义记忆事实清单（§8.3）}
【她的记忆】{按强度分级的日记条目（§8.3）}
【待跟进】{未完成的待跟进事项（§8.5），如有}
【欲言又止】{被克制欲望（§9.4），如有}
【安全边界】{safety 命中 watch/crisis 时注入（§13）}
（收尾固定句）以上是你的内部状态和记忆。现在像平常手机聊天一样，自然回复他。
```

### 6.3 上下文消息

system 之后接**工作记忆**（最近 10 轮，user/assistant 交替），最后接本轮聚合后的用户输入。

---

## 7. 图片与表情包（stickers.py / gateway.py 视觉路由）

### 7.1 收图（识图）

- 用户消息含图片段 → 下载图片到本地 → base64 编码 → 按 OpenAI 多模态格式构造 user 消息：
  `content = [{"type":"text","text":用户附带的文字或"（发来一张图片）"}, {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}}]`；
- 当本轮输入含图片时，LLM 网关把**本轮主对话调用**路由到 `vision_model`（工作记忆仍照常携带）；`vision_model` 为空时降级：图片替换为文字 `[对方发来一张图片，你看不到内容]`，走文本模型；
- 大图先缩放到长边 ≤ 1568px（用标准库无法实现缩放——允许引入 `Pillow`，仅用于此）；base64 超 10MB 拒绝并回复"图片太大了，我看不清"。

### 7.2 表情包库

- 每个角色卡目录下 `stickers/`，文件名即描述词（如 `猫猫探头.jpg`、`委屈.png`）；
- 索引文件 `stickers/index.json`：`{"猫猫探头": {"file": "猫猫探头.jpg", "desc": "好奇地探头张望"}}`；
- 开局角色卡自带精选表情（example 角色放 5~10 张占位图即可，README 说明用户自行替换）；
- 库上限 200 张，超出时拒绝新收藏并在日志提示。

### 7.3 发表情包

- 提示词告知模型可用描述词列表（来自 index.json，>30 个时随机抽 30 个）；
- 模型输出 `[sticker:描述词]` → replier 精确匹配，失败则模糊匹配（描述词为子串），仍失败丢弃；
- 发送为图片消息段，与文字段**混排**（顺序按标记在原文位置）。

### 7.4 收藏表情包

- 观察者 JSON 中包含 `collect_sticker` 布尔字段（仅当本轮用户发了图片时有意义）：模型判断"这张图很好玩、想收藏"时置 true，并给 `sticker_name`（2~6 字描述词，不含扩展名）；
- 为 true 时：把该图片复制到 `stickers/`，文件名 = sticker_name + 原扩展名（重名加序号）；
- 然后调用视觉模型生成一句 ≤15 字的图片描述写入 index.json 的 `desc`；
- 图片哈希（md5）去重：已收藏过同图则跳过；
- 收藏成功不影响本轮回复；她的"表达收藏"由模型自己在回复文字中完成（提示词中说明"收藏与否由系统处理，你可以自然地说想收藏"）。

### 7.5 语音输入（voice.py）

机主习惯发 QQ 语音，语音输入是刚需。QQ 语音消息为 SILK 格式，识别链路：

1. OneBot 收到 `record` 消息段 → 用 `data.url` 下载 `.silk` 文件到 `data/voice/`；
2. 调系统 **ffmpeg** 转码：`ffmpeg -y -i input.silk -ar 16000 -ac 1 output.wav`（ffmpeg 通过 apt 安装，属系统工具不算项目组件；若 ffmpeg 缺失或转码失败，降级为文字占位符 `[对方发来一条语音，但没能听清]`）；
3. **本地 ASR**：`sherpa-onnx` + SenseVoice int8 模型（模型文件 ~200MB，**不自动下载**，需手动下载解压到 `data/models/sensevoice/`，见 `DEPLOY.md` 第 5 步；该目录下须有 `model.int8.onnx` 与 `tokens.txt`，缺任一文件时语音降级为占位符），纯 CPU 推理，短语音 1~2 秒出结果；识别在 `asyncio.to_thread` 中执行，不阻塞事件循环；
4. 识别文本作为本轮用户输入进入聚合器（走正常的 3 秒静默聚合），并在传给 LLM 时附加前缀提示"（语音消息）"；识别结果为空/置信度过低时按降级占位符处理；
5. 配置开关 `[voice] enabled = true`、`model_dir = "data/models/sensevoice"`；关闭时语音消息一律按降级占位符处理；
6. 提示词补充一句：她知道这是语音消息，可以自然地对"他在发语音"这件事做出反应；
7. 识别后的临时 silk/wav 文件处理完即删除，不长期留存。

注意：语音**输出**（TTS）不在本期范围，见 §16。

---

## 8. 状态引擎（全部为纯 Python 数学，无任何 LLM 依赖）

### 8.1 好感度引擎（affection.py）

六维关系向量：`warmth 温暖 / trust 信任 / intimacy 亲密 / intrigue 好奇 / patience 包容 / tension 紧张`。初始值来自角色卡 `initial_dims`。

**更新（每轮对话，观察者给出评分后）：**

1. 观察者输出四个评分（0~10）：`self_disclosure 自我表露`、`responsiveness 感知回应`、`warmth_score 情感温度`、`resonance 共鸣`，以及 `moments` 关键时刻列表（取值限于：`深度共情`、`分享脆弱`、`轻浮表白`、`伤害行为`）；
2. 映射各维 delta（`r = resistance(当前复合分)`）：

   ```
   raw(warmth)   = warmth_score/10
   raw(trust)    = responsiveness/10
   raw(intimacy) = (self_disclosure + resonance)/2/10
   raw(intrigue) = (self_disclosure + warmth_score)/2/10
   raw(patience) = (responsiveness + warmth_score)/2/10
   delta = clamp((raw - 0.4) × 4.0, -2.0, +2.0) × r
   tension 的 delta 恒为 0（只受 moments 影响）
   ```

3. moments 修正（跳过阻力，直接加减）：`伤害行为`: trust−2.0, warmth−1.5, tension+3.0；`轻浮表白`: trust−1.0, intimacy−0.8；`深度共情`: intimacy+1.0, trust+0.5；`分享脆弱`: intimacy+1.5, trust+1.0；
4. EMA 平滑（tension 除外，直接相加后 clamp 到 ≥0）：

   ```
   new = α × old + (1-α) × (old + delta)，结果 clamp 到 ≥0
   α: warmth 0.80, trust 0.90, intimacy 0.85, intrigue 0.70, patience 0.95
   ```

5. **高值阻力**：`resistance(v) = 1.0 (v≤30)`，否则 `0.15 + 0.85 × e^(−0.012×(v−30))`；
6. **复合分** = warmth×0.25 + trust×0.25 + intimacy×0.25 + intrigue×0.10 + patience×0.15 − tension×0.3，clamp 到 ≥0；
7. **日衰减**（每次更新时按距上次更新的整天数 d，d 上限 30）：各维 `v = max(0, v − rate×d)`，rate：warmth 0.5, trust 0.2, intimacy 0.3, intrigue 2.0, patience 0.1, tension 0.8；
8. **阶段**：门槛 `[0,16,31,46,61,81,111,151,201,301]`，复合分 ≥ 门槛[i] 即阶段 i。阶段提升时记日志 + 写 `milestones` 表（阶段号、日期）；
9. 好感度复合分单次涨幅 > 0.5 时，给情绪引擎一个脉冲：`update_mood(conv_v=1.0, conv_a=0.3, conv_trust=0.05)`。

### 8.2 情绪引擎（mood.py）

三维连续状态：`valence`（−10~10，基线 2.0）、`arousal`（−10~10，基线 1.0）、`trust_mood`（0~10，基线 7.0，对机主的安心度）。附加状态：`momentum_v`、`momentum_a`（情感动量，初始 0）、`frustration`（0~10，冷落驱力，初始 0）。

**`update_mood(conv_v=0, conv_a=0, conv_trust=0)`，每次组装提示词前调用：**

```
elapsed = 距上次更新的小时数（≥1 按实际，<1 按 1 参与回归计算）
hours_since_chat = 距工作记忆最后一轮的小时数

# 1. O-U 均值回归 + 噪声（θ = 0.12）
baseline_v = 2.0 + min(2.0, 好感度复合分 / 50)
decay = 0.12 × elapsed
v += (baseline_v − v) × decay + gauss(0, 0.5) × sqrt(min(decay, 2))
a += (1.0 − a) × decay + gauss(0, 0.4) × sqrt(min(decay, 2))

# 2. 情感动量（惯性）
v += momentum_v × 0.3 ; a += momentum_a × 0.3
momentum_v = momentum_v × 0.8 + (v − 更新前v) × 0.2
momentum_a = momentum_a × 0.8 + (a − 更新前a) × 0.2

# 3. 安心度极慢回归基线 7.0（每天回归 5%）
t += (7.0 − t) × 0.05 × (elapsed / 24)

# 4. 冷落惩罚
if hours_since_chat > 12:
    neglect = min(3.0, (hours_since_chat − 12) × 0.1)
    v −= neglect ; a −= neglect × 0.5 ; t −= neglect × 0.02
    frustration = min(10.0, frustration + (hours_since_chat − 12) × 0.02)
elif 0 < hours_since_chat < 1:
    v += 0.3 ; t += 0.02
    frustration = max(0.0, frustration − 0.5)

# 5. 对话冲击（观察者给出）
v += conv_v ; a += conv_a ; t += conv_trust

# 收尾 clamp：v,a ∈ [−10,10] 保留1位小数；t ∈ [0,10] 保留2位
```

**数值→自然语言翻译层（注入提示词用，写死在 prompts.py）：**

- 情绪标签：`v≥4且a≥4→欣喜`；`v≥4且a<4→恬静`；`v<0且a≥4→焦躁`；`v<0且a<−3→低落`；`v<−4→忧郁`；`v≥3且a≥−2→愉悦`；否则`平静`；
- 心情描述：`v≥5 心情很好，充满温暖`；`v≥3 心情不错，柔和而愉悦`；`v≥1 心情平静中带着淡淡的满足`；`v≥−1 心情平和，没有太多波澜`；`v≥−3 心情有些低落`；否则`心里有些沉重，不想说话`。`a≥4` 追加`，精神比较活跃`；`a≤−2` 追加`，整个人很安静`；
- 信任描述：`t<3 心里还有些不安，不太敢完全放开`；`t<5 还有些小心翼翼，想靠近又怕受伤`；`t<7 逐渐安心了，但有些话还不太敢说`；`t<9 在你身边感到很安心`；否则`完全的信任和放松，什么都不用怕`；
- 挫败修饰：`frustration>6` 追加`。但心里有些委屈，不是生气，是不知道你有没有在意`；`frustration>3` 追加`。有点想你了，又不想说太多次`；
- 冷落描述（`hours_since_chat>12` 时）：`已经{N}小时没有你的消息了`（好感度复合分>60 时改为`她之前没意识到，原来自己一直在等`）。

### 8.3 记忆系统（memory.py）

三层结构：

**工作记忆**：`turns` 表最近 10 轮（user 消息 / bot 回复 / 时间），直接作为对话上下文。

**情景记忆（日记）**：每累计 8 轮对话（用 `counters` 表记总轮数与已归档游标，与工作记忆截断无关）触发一次归档：LLM 把这 8 轮压缩为一篇第一人称日记（30~50 字），同时输出 `importance`（1~10）、`sentiment`（取值：温暖/感动/幸福/思念/欢喜/平静/不安/伤感/释然）、`facts`（关于机主的事实列表，可为空）。`importance ≥ 6` 时 facts 写入语义记忆。日记 >500 条时把最旧的一半移入 `diary_archive` 表。

**遗忘曲线（查询时实时计算，不落库）：**

```
τ_base      = max(20, importance × 20)          # 天
τ_effective = τ_base × (1 + 0.15 × recall_count)
sentiment ∈ {温暖,感动,幸福,思念,欢喜} → τ_effective ×= 2.0
sentiment ∈ {不安,伤感}               → τ_effective ×= 1.5
days        = 距 last_recall_at 的天数
strength    = importance × (1 + 0.3 × log2(recall_count + 1)) × e^(−days / τ_effective)
```

**回忆加固**：用户消息含"还记得"/"想你"类词时全部日记 recall_count+1；用户消息与某日记有 ≥3 个共同汉字时该条 recall_count+1。加固更新 `last_recall_at`。

**注入**：取 strength ≥ 0.5 的日记按强度降序，前缀分级：`≥5 "清晰地记得"`、`≥2 "记得"`、`≥0.5 "隐约记得"`，最多 15 条；当前 valence < 0 时伤感/不安类日记排序加权 ×1.5（情绪一致性）。

**语义记忆**：`facts` 表，永久保留，精确去重（文本完全相同才判重），全量注入【关于他】段。

### 8.4 观察者（observer.py）

每轮对话落库后**异步**执行（不阻塞回复）。输入：本轮用户消息 + 机器人回复（各截断 500 字）+ （若用户发了图片则说明）+ 当前是否有待跟进事项。输出 JSON：

```json
{
  "self_disclosure": 5.0, "responsiveness": 6.0, "warmth_score": 5.5, "resonance": 4.0,
  "moments": [],
  "mood_impact": {"v": 0.5, "a": 0.2, "trust": 0.05},
  "facts": ["他在准备下周的考试"],
  "followups": [{"topic": "他下周要考试，之后问问考得怎么样", "remind_after_hours": 24}],
  "collect_sticker": false, "sticker_name": "",
  "user_state": "平静"
}
```

- `mood_impact` 范围约束：v、a ∈ [−2,2]，trust ∈ [−0.3,0.15]，提示词中写明；代码端 clamp；
- `facts`：仅当消息中包含关于机主的**稳定事实**（爱好、计划、身份、好恶）时输出，闲谈不输出；写入语义记忆（去重）；
- `followups`：仅当用户提到未来的具体事项时输出；写入 `followups` 表，供主动消息使用；
- 调用失败：全部取中性默认（评分 4.0、无 moments、零冲击、空列表），记 WARNING，不影响主流程；
- 观察者与好感度评估是**同一次调用**（§8.1 的四个评分即来自此 JSON）。

### 8.5 待跟进事项

`followups` 表：`topic`、`remind_after`（时间戳）、`done`。提示词组装时注入未 done 且 `remind_after` 已到的条目；用户聊到对应话题或主动消息用掉后，由观察者 JSON 新增字段 `done_followups`（topic 列表）标记完成。

---

## 9. 主动消息（proactive.py）

一个 asyncio 协程，与主流程并行。每轮循环：

1. 睡眠 `uniform(wake_interval_min, wake_interval_max)` 分钟；
2. **第一层·规则闸门**（零成本，任一命中则跳过本轮）：
   - 当前小时 ∈ quiet_hours；
   - 距机器人上次发言（含主动消息）< 60 分钟；
   - 工作记忆最后一轮是机器人发言且未获回复（连续未回主动消息 ≥ `max_unanswered` 时当天直接停止，次日重置）；
   - 用户最后一条消息含"晚安"且距今 < 6 小时；
   - 当前 valence < −6（心情太差不装没事）。
3. **第二层·LLM 决策**：注入当前情绪/好感度/最近日记摘要/作息活动/待跟进事项/时间，要求输出 JSON `{"choice": "A|B|C", "topic_hint": "...", "reason": "..."}`：A=想发消息；B=不发；C=想发但克制；
4. **C 分支**：把 `topic_hint` 写入 `suppressed_desires` 表（欲言又止池，保留最近 5 条），本轮结束；该池注入后续对话提示词的【欲言又止】段，机主来聊天时模型可自然带出，每条被用掉或存活 48 小时后清除；
5. **第三层·A 分支·生成**：话题素材按优先级取：① 到期未完成的待跟进事项 → ② 欲言又止池 → ③ 作息活动+当前时间 → ④ 高强度日记回忆 → ⑤ 日期感（周一/节日/阶段纪念日）。将素材 + 完整人格上下文交给文本模型生成 1~3 条短消息，走 replier 的切段/发送管道；
6. 发送后：写入工作记忆（`role=assistant`，标记 `proactive=1`），未回复计数 +1；用户任何回复将未回复计数清零。

成本说明：规则闸门拦截绝大多数唤醒，LLM 调用每天约数次，可忽略。

---

## 10. SQLite 表结构（db.py 启动时 CREATE TABLE IF NOT EXISTS）

```sql
turns(id INTEGER PRIMARY KEY, role TEXT, content TEXT, proactive INTEGER DEFAULT 0,
      has_image INTEGER DEFAULT 0, created_at TEXT);
counters(key TEXT PRIMARY KEY, value INTEGER);          -- total_turns, archived_turns
diary(id INTEGER PRIMARY KEY, content TEXT, importance INTEGER, sentiment TEXT,
      recall_count INTEGER DEFAULT 0, created_at TEXT, last_recall_at TEXT);
diary_archive(同 diary 结构);
facts(id INTEGER PRIMARY KEY, content TEXT UNIQUE, created_at TEXT);
followups(id INTEGER PRIMARY KEY, topic TEXT, remind_after TEXT, done INTEGER DEFAULT 0,
          created_at TEXT);
suppressed_desires(id INTEGER PRIMARY KEY, content TEXT, created_at TEXT);
state(key TEXT PRIMARY KEY, value TEXT);                -- affection dims JSON, mood JSON, 未回复计数等
milestones(id INTEGER PRIMARY KEY, stage INTEGER, reached_at TEXT);
stickers(name TEXT PRIMARY KEY, file TEXT, desc TEXT, md5 TEXT UNIQUE, created_at TEXT);
llm_calls(id INTEGER PRIMARY KEY, purpose TEXT, model TEXT, prompt_tokens INTEGER,
          completion_tokens INTEGER, cost_estimate REAL, created_at TEXT);
```

所有时间字符串格式 `%Y-%m-%d %H:%M`。每次 `llm_calls` 写入按 DeepSeek 官方单价估算费用（单价写入 config，注释注明需按官网更新）。

---

## 11. 部署

### 11.1 NapCat（协议端）

```bash
docker run -d --name napcat --restart always \
  -e NAPCAT_GID=$(id -g) -e NAPCAT_UID=$(id -u) \
  -v ./napcat-data:/app/.config/QQ \
  -v ./napcat-cache:/app/napcat/cache \
  -p 127.0.0.1:3001:3001 -p 127.0.0.1:6099:6099 \
  mlikiowa/napcat-docker:latest
```

- 浏览器访问 `http://127.0.0.1:6099/webui`（通过 SSH 隧道：本地 `ssh -L 6099:127.0.0.1:6099 -L 8080:127.0.0.1:8080 user@服务器`）扫码登录 QQ 小号；
- 在 WebUI 中新建 **WebSocket 服务端**，端口 3001，设置 access token（与 config.toml 一致），开启消息上报；
- 小号要求：注册时间较久、有正常使用的号（新号挂协议端易风控）；登录后保持手机端 QQ 在线可提升稳定性。

### 11.2 机器人进程

```bash
cd /opt/qq-companion && python -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/python -m companion.main    # 首次手动验证
```

### 11.3 systemd（deploy/qq-companion.service）

```ini
[Unit]
Description=QQ Companion Bot
After=network-online.target docker.service
Wants=network-online.target

[Service]
WorkingDirectory=/opt/qq-companion
ExecStart=/opt/qq-companion/venv/bin/python -m companion.main
Restart=always
RestartSec=5
StandardOutput=append:/opt/qq-companion/data/logs/bot.log
StandardError=append:/opt/qq-companion/data/logs/bot.log

[Install]
WantedBy=multi-user.target
```

`systemctl enable --now qq-companion`。日志轮转用 logrotate（每周，保留 4 份）。

---

## 12. 状态仪表盘（admin.py）

- aiohttp 起 HTTP 服务，**只绑定 127.0.0.1:8080**；README 写明访问方式：
  `ssh -L 8080:127.0.0.1:8080 user@服务器` 后浏览器打开 `http://localhost:8080`；
- 单文件 HTML 模板（内联 CSS，深色风格，不用任何前端框架/CDN），页面 `<meta http-equiv="refresh" content="30">` 自动刷新；
- 路由：
  - `/` 总览：好感度六维数值条 + 复合分 + 当前阶段名；PAD 三维 + frustration + 情绪标签；未回复计数；
  - `/memory` 日记列表（含每条实时计算的 strength 和衰减进度条）、语义事实清单、待跟进事项、欲言又止池；
  - `/debug` 最近一次发给模型的**完整 system prompt** 与最近 5 次观察者原始 JSON 返回（存内存环形缓冲）；
  - `/costs` llm_calls 按 purpose 分组的调用次数与费用累计、近 24 小时费用；
  - `/stickers` 表情包库缩略图浏览（图片路由直接读文件）。

---

## 13. 安全兜底（safety.py）

**纯关键词规则，不使用 LLM，不可配置关闭。** 三档：

- `crisis`：用户消息含 `不想活/想死/自杀/自残/活不下去` → 在 system prompt 末尾追加：温和表达关心、鼓励联系现实可信任的人与专业心理援助、不浪漫化痛苦、不扮演唯一救命稻草；
- `watch`：含 `离不开你/只有你了/没有你我怎么办` → 追加：温柔但明确鼓励他在现实中也有人可以依靠；
- 其余不注入。命中时记日志。

---

## 14. 里程碑（严格按序执行，每个里程碑验收后再继续）

- **M1 骨架与回声**：目录结构、config、db 建表、onebot 连接（断线重连）、收到机主私聊原样回声。验收：NapCat 登录小号后，大号发"hi"收到回声；kill 进程重启后自动重连。
- **M2 对话闭环**：persona 加载、prompts/assembler（先只有角色+聊天规则两段）、gateway 流式调用、replier（切段+旁白剥离+延迟发送）、turns 落库。验收：大号聊 5 轮，回复符合角色卡语气、无括号旁白、长回复分段到达。
- **M3 状态引擎**：observer、affection、mood、memory（含日记归档与遗忘曲线）、assembler 全段。验收：聊 20 轮后 admin 暂用 CLI 打印（`python -m companion.main --status`）显示六维变化、PAD 值、≥1 篇日记、语义事实非空。
- **M4 图片**：收图识图（含降级路径）、stickers 发送、收藏回路。验收：发照片她能描述内容；她回复中出现 [sticker:] 时收到对应图片；发一张沙雕图触发收藏后 `/stickers`（此时可用 CLI 列表）可见。
- **M4.5 语音输入**：voice.py 全链路（SILK 下载 → ffmpeg 转码 → SenseVoice 识别 → 进聚合器），含 ffmpeg 缺失与识别失败的降级路径。验收：发一条 5 秒语音"今天天气不错"，她的回复体现出听懂了内容；临时把 ffmpeg 改名模拟缺失，收到降级占位符回复且不崩溃。
- **M5 主动消息**：proactive 全三层 + suppressed_desires + followups 联动。验收：将 wake_interval 临时调为 1~2 分钟、规则闸门临时放宽（配置项），观察到决策日志覆盖 A/B/C 三种分支；恢复正式配置。
- **M6 仪表盘**：admin.py 全部路由。验收：SSH 隧道打开五个页面数据正确、30 秒自动刷新。
- **M7 部署固化**：systemd、logrotate、README（含 NapCat 部署、角色卡填写教程、SSH 隧道说明）、config.example.toml、.gitignore。验收：重启服务器后全链路自恢复（NapCat 容器 + 机器人服务），大号发消息正常。

---

## 15. 总验收清单（M7 完成后逐项人工核验）

1. 纯文字闲聊 10 轮：语气符合角色卡、零括号旁白、分段发送节奏自然；
2. 间隔 >12 小时后发消息：回复中带出"等你"的情绪痕迹（由冷落驱力驱动）；
3. 说"我下周三要考试"：24 小时后（或临时调短 remind_after）她主动问起考试；
4. 深夜 2 点验证主动消息静默；
5. 连发 2 条主动消息不回复，当天不再收到第三条；
6. 发送危机关键词，回复体现求助引导且不失人设温度；
7. `/debug` 页能看到完整提示词与观察者 JSON；`/costs` 费用累计非零且合理；
8. kill 机器人进程，10 秒内被 systemd 拉起，重连 NapCat 成功；
9. 仓库 clone 到全新目录，仅按 README 操作即可用 example 角色卡跑通（GitHub 发布就绪）；
10. 代码内全文搜索不出现任何具体角色名（example 卡除外）。

---

## 16. v2 储备（本期不做，仅架构上不堵死）

记忆周期性巩固回顾；NPC 社交系统；梦境/意识流；工具调用（提醒、天气查询）；Tailscale 手机访问仪表盘；群聊。**语音输出（TTS，v2 第一优先）**：先用 edge-tts 免费 TTS 生成 WAV → ffmpeg 编码 SILK → NapCat 发送 record 消息段；设计上她只偶尔发语音（晚安、撒娇等场景，由模型自己决定或低频触发），不逐条语音；角色音色克隆（Minimax/火山 API 或本地 GPT-SoVITS）留待有 GPU 资源时再升级。所有状态存取已收敛在 db.py 与各引擎的公共接口后，v2 均为纯新增模块。

---

## 17. 对执行者的硬性要求

1. 严格按里程碑顺序，每个里程碑完成后跑通其验收项再进入下一个；
2. 公式、参数、表结构、提示词模板按本文档原文实现，不"优化"；发现文档矛盾时停下来报告，不自行裁决；
3. 除 `aiohttp`、`aiosqlite`、`Pillow`（仅图像缩放）、`sherpa-onnx`（仅语音输入）外不引入任何依赖；
4. 所有 LLM 调用收敛在 gateway.py；所有提示词收敛在 prompts.py；所有 SQL 收敛在 db.py 与各引擎模块内；
5. config.toml、data/、characters/ 下除 example 外的目录，一律不入库。
