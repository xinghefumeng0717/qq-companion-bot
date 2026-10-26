# QQ 伴侣机器人 (QQ Companion Bot)

部署于 Linux (Ubuntu 22.04) 或本地 Windows 上的 24 小时运行 **QQ 私聊角色扮演伴侣机器人**。

- **机主私聊专属**：机主使用自己的 QQ 大号与机器人登录的 QQ 小号 1v1 私聊；
- **人设与代码彻底解耦**：系统代码不包含任何特定角色设定，角色完全通过外置角色卡 JSON 配置；
- **真实聊天感（最高优先级）**：绝对禁止小说式括号旁白描写，像真人在手机 QQ 聊天一样分段发送、模拟打字节奏、支持收发与收藏表情包；
- **丰富内部引擎**：六维好感度系统（数学动力学模型）、三维 PAD 连续情绪引擎（带 O-U 均值回归与冷落惩罚）、三层记忆系统（工作记忆、情景日记遗忘曲线、语义事实）、主动消息三层调度（规则闸门 + 潜意识决策 + 素材优选）。

> **想把部署交给 AI 助手？** 本仓库自带写给 AI 的零基础部署说明书 [`docs/DEPLOY_FOR_AI.md`](docs/DEPLOY_FOR_AI.md)：包含服务器/本机双路线与分档带教指南。

---

## 目录

1. [第一设计原则：手机聊天感与主模型减负](#1-第一设计原则手机聊天感与主模型减负)
2. [新内核架构 (DialogueKernel)](#2-新内核架构-dialoguekernel)
3. [输入分流与旧链路回退 (Fallback)](#3-输入分流与旧链路回退-fallback)
4. [语音与多动作系统：职责分离与原稿冻结](#4-语音与多动作系统职责分离与原稿冻结)
5. [配置项与开关说明 (use_kernel)](#5-配置项与开关说明-use_kernel)
6. [环境准备与依赖](#6-环境准备与依赖)
7. [NapCat 协议端部署 (Docker)](#7-napcat-协议端部署-docker)
8. [机器人配置与启动](#8-机器人配置与启动)
9. [角色卡编写教程](#9-角色卡编写教程)
10. [状态仪表盘访问 (SSH 隧道)](#10-状态仪表盘访问-ssh-隧道)
11. [Systemd 服务守护与开机自启](#11-systemd-服务守护与开机自启)
12. [工具、仿真沙箱与 full_sandbox](#12-工具仿真沙箱与-full_sandbox)
13. [开发者：单元测试与冒烟](#13-开发者单元测试与冒烟)
14. [部署边界与安全规范](#14-部署边界与安全规范)

---

## 1. 第一设计原则：手机聊天感与主模型减负

机器人必须像一个真人在手机 QQ 上聊天，**绝对禁止**小说式角色扮演风格（如括号心理描写、第三人称陈述）。

在经历多次线上迭代演变后，系统确立了**「主模型减负与防漂移」**核心原则：
1. **主模型只负责根据干净上下文生成自然语言**：不再要求模型理解、解析或在正文内嵌内部协议标签（禁止 `[label:1]`、`[voice:xxx]`、`[quote:xxx]`、`[吃糖]` 等内嵌指令污染生成正文）；
2. **先做减法，再做加法**：优先精简重复模板与矛盾提示词。主模型只接收清晰、精简的心理学结论与上下文，内部复杂的数学动力学（好感度、PAD 动量）由系统内部计算与冻结；
3. **原稿冻结与职责分离**：主模型生成的自然语言草稿一旦定稿（`ReplyDraft`），文字语义不可变；系统投递动作只引用冻结气泡，禁止通过正则二次润色、抽空正文或前后删改补救；
4. **系统管理非文本行为**：语音规划、表情选择、引用消息投递、沉默权判定均由系统架构在主模型生成前后完成。

---

## 2. 新内核架构 (DialogueKernel)

为了实现主模型减负与契约化解耦，系统引入了离线对话内核（`companion/kernel/`），由四大核心数据契约贯穿单轮生命周期：

```
[标准化输入 TurnInput]
       │
       ▼
[快照与只读组装 PreparedTurn] ─── (冻结心理快照 PsychologicalSnapshot + 只读记忆视图 + 精简示例)
       │
       ▼
[主模型生成单轮纯文本]
       │
       ▼
[不可变原稿 ReplyDraft] ──────── (切分为固定 ReplyBubble 气泡，严禁后续正则篡改)
       │
       ▼
[动作规划 DeliveryPlan] ──────── (绑定气泡 ID、引用消息 ID、外部投递通道)
       │
       ▼
[投递执行 DeliveryResult] ────── (支持文字、系统语音、真实收据验证)
```

- **`StandardizedTurnInput`**：保留真实 QQ `message_id`，同时归一化提取用户输入的自然语义文本，将历史引用语义提前解析；
- **`PreparedTurn`**：一轮内只读准备结果。包含当轮唯一冻结的 `PsychologicalSnapshot`（精简 `prompt_hint` 与内部 `action_context`）、记忆只读视图（`MemoryPromptBlocks`）与冻结动态示例，生成准备过程不污染状态；
- **`ReplyDraft`**：主模型单次调用产出的原始文本响应及其切分出的 `ReplyBubble` 气泡列表。原稿在此冻结，作为后续所有投递动作的真实事实源；
- **`DeliveryPlan`**：投递动作计划。每个动作（`DeliveryAction`）仅引用 `bubble_id` 并指定通道（如 `text`、`voice`），支持明确绑定的 `quote_message_id` 与生成前沉默判定。

---

## 3. 输入分流与旧链路回退 (Fallback)

在当前阶段，新内核与生产环境通过桥接器 `KernelTurnHandler`（位于 `companion/kernel/production.py`）连接，实行严格且安全的分流策略：

1. **普通纯文字输入进入新内核**：用户发送的普通纯文字私聊消息，由 `DialogueKernel` 完成端到端准备、单次生成与安全校验；
2. **特殊输入生成前回退旧链路**：
   - 包含图片消息；
   - 历史消息复合引用、非纯文本引用；
   - 包含旧版表情内嵌协议（如 `[face:xxx]`、`[sticker:xxx]`）或特殊富文本；
   - 包含非语音请求的括号标签。
   以上输入在**调用模型前**由 `can_use_kernel_text` 判定并安全回退至原有 `Replier` 链路处理，确保现有功能完备性不受影响；
3. **不可恢复异常时的失败回退**：若新内核生成过程中出现不可用异常，系统保障安全拦截，绝不调用第二个模型二次润色，更不篡改原稿。

---

## 4. 语音与多动作系统：职责分离与原稿冻结

系统支持 MiniMax TTS 语音回复与 QQ 动作投递，并在新架构下实现严格的职责划分：

- **系统统一规划**：当用户提出明确语音诉求（或符合语音触发规则）时，语音调度由系统独立规划（`plan_explicit_voice`），主模型无需输出任何特殊的语音控制字符；
- **原稿真实投递**：被选为语音播报的气泡必须是主模型原稿中真实存在的气泡，正文原样送入合成器；
- **合成/发送失败降级保底**：当 TTS 服务不可用、额度耗尽或 OneBot 发送失败时，系统将该气泡真实降级为普通文字发出，**绝不静默吞字丢话**，也不会出现删句后只留空白的事故。

---

## 5. 配置项与开关说明 (use_kernel)

新内核目前采用渐进式开关控制：

```toml
[reply]
use_kernel = false   # 对话内核总开关：默认关闭 (False)
```

- **`use_kernel = false`（当前默认值）**：全量走稳定的传统回复链路（`TurnHandler` + `Replier`）；
- **`use_kernel = true`**：启用新内核桥接（`KernelTurnHandler`）。普通纯文字经由 `DialogueKernel` 处理，特殊消息与多模态在生成前平滑回退旧链路。

> **注意**：线上生产环境建议在离线沙箱与测试集充分验证后，再按需开启。

---

## 6. 环境准备与依赖

### 基础环境
- Linux (Ubuntu 22.04 LTS 推荐) 或 Windows 10/11
- Python 3.10+
- Docker（用于运行 NapCat 容器）

### 安装步骤

```bash
# 克隆代码仓库
git clone <你的仓库地址>
cd <仓库目录>

# 创建并激活 Python 虚拟环境
python3 -m venv venv
source venv/bin/activate  # Windows: .\venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
```

---

## 7. NapCat 协议端部署 (Docker)

NapCat 是底层 QQ 协议端实现（基于 NTQQ），通过 OneBot v11 WebSocket 与本项目通信。

### 7.1 运行 NapCat 容器

```bash
docker run -d \
  --name napcat \
  --restart always \
  -p 3001:3001 \
  -p 6099:6099 \
  -e ACCOUNT=你的机器人QQ号 \
  -v /opt/napcat/data:/app/.config/QQ \
  -v /opt/napcat/config:/app/napcat/config \
  mlikiowa/napcat-docker:latest
```

### 7.2 扫码登录与 WebSocket 配置

1. 浏览器访问 `http://<服务器IP或本地地址>:6099/webui`；
2. 在 WebUI 中使用机器人的 QQ 小号扫码登录；
3. 进入 NapCat 网络配置，启用 **WebSocket 服务端 (WS)**：
   - 监听端口：`3001`
   - 访问 Token：设置一个安全的随机字符串。

---

## 8. 机器人配置与启动

### 8.1 配置文件

复制配置模板：
```bash
cp config.example.toml config.toml
```

编辑 `config.toml`（示例）：
```toml
[account]
allowed_user_id = 123456789        # 机主大号 QQ 号（只响应此人）
bot_qq = 987654321                 # 小号 QQ 号

[onebot]
ws_url = "ws://127.0.0.1:3001"
access_token = "与 NapCat WebUI 中一致的 token"

[llm]
current = "deepseek"
thinking_chat = true
thinking_effort_chat = "low"
thinking_tasks = false
thinking_effort_tasks = "low"

[models.deepseek]
provider = "deepseek"
base_url = "https://api.deepseek.com"
api_key = "sk-xxxxxxxx"
chat = "deepseek-v4-pro"
vision = "deepseek-flash"
tasks = "deepseek-flash"

[character]
path = "characters/example"        # 激活的角色卡目录

[reply]
use_kernel = false                 # 内核开关：默认 false
```

### 8.2 验证运行

```bash
python -m companion.main
```

启动后连接 OneBot 成功，使用机主大号给机器人发送一条测试消息，确认能收到角色口吻回复。

---

## 9. 角色卡编写教程

角色设定与系统完全解耦。仓库自带公开模板卡 `characters/example/`。

- [docs/CUSTOMIZE.md](docs/CUSTOMIZE.md)：定制角色的完整指引；
- [docs/CARD_CRAFT.md](docs/CARD_CRAFT.md)：《怎么写出不像 AI 的角色卡》方法论；
- **目录结构**：
  ```
  characters/your_character/
  ├── character.json       # 人设核心配置（世界观、语气、关系阶段、作息、生活主线）
  └── stickers/
      ├── index.json       # 表情包语用索引（按语用功能匹配，而非画面）
      └── *.png / *.jpg    # 表情包图片文件
  ```

---

## 10. 状态仪表盘访问 (SSH 隧道)

机器人内置轻量 Web 状态仪表盘（默认端口 `8080`）。

### 访问方式 (推荐 SSH 隧道)

在本地电脑终端执行端口转发：
```bash
ssh -N -L 8080:127.0.0.1:8080 ubuntu@<你的服务器IP>
```
在本地浏览器打开 `http://127.0.0.1:8080` 即可查看好感度趋势、当前 PAD 情绪坐标、记忆摘要与系统日志。

---

## 11. Systemd 服务守护与开机自启

用于在 Linux 服务器后台持久化运行：

```bash
# 复制服务模板到系统目录
sudo cp deploy/qq-companion.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qq-companion
```

- 状态查看：`sudo systemctl status qq-companion`
- 日志跟踪：`journalctl -u qq-companion -f` 或 `tail -f data/logs/bot.log`
- 重启服务：`sudo systemctl restart qq-companion`

---

## 12. 工具、仿真沙箱与 full_sandbox

仓库提供了多层次的本地仿真测试工具，无需依赖真实 QQ 客户端即可验证系统逻辑：

### 12.1 交互式对话沙箱 (`companion.chat`)
在命令行与当前角色卡直接文字交互，便于快速微调人设：
```bash
python -m companion.chat
```

### 12.2 全流程端到端沙箱 (`scripts/sim/full_sandbox.py`)
完全启动与生产环境相同的 `CompanionBot` 接线（包括聚合器、观察者、日记事实、主动消息、语音判定），仅将外部依赖进行桩实现替换：
- **假 OneBot 客户端**：记录所有发出的文字/语音/表情，支持主动注入“发送失败”异常；
- **假 TTS 语音合成**：无需真实费用，支持注入“合成失败”以校验真实降级文字逻辑；
- **拨动假时钟**：模拟跨天、作息切换与多轮剧情演进，中途执行模拟重启校验记忆连续性。

运行命令：
```bash
python scripts/sim/full_sandbox.py --max-cost 3
```
运行结果会保存在 `data/full_sandbox/<run_id>/`（包含可读的 `transcript.md` 与详细检查点 `checks.json`）。

---

## 13. 开发者：单元测试与冒烟

仓库包含完善的单元测试集，覆盖底层动力学、记忆视图、新内核各契约组件与投递验证：

```bash
# 安装测试依赖（开发环境）
pip install pytest anyio

# 运行完整测试
python -m pytest -q tests
```

- **公开克隆可跑通用测试**：使用仓库自带的 `characters/example/` 模板卡；依赖私有配置、角色卡、历史数据库或真实语料的用例会明确标记跳过，不把这些资产打包进公开仓库；
- **显式指定角色卡测试**：
  ```bash
  QQC_TEST_CARD=characters/example python -m pytest -q tests
  ```
- **真实 API 冒烟提示**：`scripts/smoke/` 下的脚本会直接调用外部在线 LLM / TTS 接口并产生真实费用，请勿批量执行。

---

## 14. 部署边界与安全规范

1. **绝对保护核心机密与配置**：
   - 严禁提交包含真实 QQ 账号、OneBot Token、模型 API 密钥的 `config.toml`；
   - 严禁将私人服务器真实 IP、私有路径与敏感环境变量写入公开代码或文档；
2. **私有角色卡与数据隔离**：
   - `characters/` 下自定义私有角色卡及 `data/` 运行时数据库已被 `.gitignore` 拦截；
   - 仓库代码保持完全通用，不硬编码任何专属角色设定或作品分支；
3. **架构演进边界**：
   - 当前新内核处于平稳推进阶段，生产默认关闭开关（`use_kernel = false`），多模态与特殊消息按既定策略安全回退；
   - 更多部署指引参见官方公开部署手册 [`docs/DEPLOY_FOR_AI.md`](docs/DEPLOY_FOR_AI.md) 与 [`docs/DEPLOY.md`](docs/DEPLOY.md)。

---

## 15. 代码目录地图

```text
companion/
├── main.py                 # 生产组合入口：装配、生命周期与 OneBot 连接
├── turn_handler.py         # 旧流水线：特殊消息与既有能力回退
├── aggregator.py           # 私聊消息聚合、真实 message_id 保留
├── kernel/
│   ├── contracts.py        # 不可变输入、快照、原稿与投递契约
│   ├── kernel.py           # 公共准备 + 单次生成 + 结果校验
│   ├── production.py       # 生产薄桥接与事前分流
│   ├── context.py          # system/history/user messages 构建
│   ├── psychology.py       # 每轮唯一只读心理快照
│   ├── memory_view.py      # 只读记忆与来源视图
│   ├── semantic.py         # 输入归一、历史语义、原稿与计划
│   ├── delivery.py         # 动作校验、语音通道分配与执行
│   ├── style_view.py       # 可选示例的确定性只读视图（默认不推广）
│   └── tracing.py          # JSONL 脱敏追踪
├── replier.py              # 既有发送分段、表情和兼容逻辑
├── tts.py                  # TTS 合成、日额度与合成闸门
└── memory.py               # 工作记忆、日记、事实与自我记忆

scripts/
├── sim/full_sandbox.py     # 全流程端到端沙箱
└── ab/                     # 离线 A/B 实验工具（默认只组装估价）

tests/                      # 单元、契约、集成与沙箱测试
docs/                       # 部署、定制与历史设计文档
characters/example/         # 可公开的示例角色卡
deploy/                     # systemd 服务模板（部署 tar 包不入库）
```

---

## 16. 当前验证口径与已知边界

### 验证分三层

1. **契约与单元测试**：心理数学不变、输入输出契约、引用目标、原稿顺序、语音/文字/引用组合；
2. **离线沙箱**：同一输入下 `prepare_turn` 与 `handle_turn` 上下文一致，准备阶段不调用模型、不推进心理；
3. **完整剧情沙箱**：跨天剧情、真实 LLM、假 QQ/TTS、故障注入和重启记忆。每次真实运行还会输出费用、逐拍对话、记忆表与检查表，便于判断是结构错误还是单纯的文风问题。

### 当前边界

- 新内核并不替代所有旧能力。多模态、特殊富文本和主动消息仍有旧路径参与；
- 动态示例虽支持确定性回放，但实测没有稳定收益，不进入默认推广范围；
- 沙箱通过不代表所有客户端和所有网络条件都相同。上线仍应检查真实 QQ、NapCat 与 TTS；
- 模型原稿冻结不等于模型永远不会写出不自然的话，它只保证系统不再二次修剪或补救正文。

### 费用提醒

`python scripts/sim/full_sandbox.py` 的假 QQ 和假 TTS 不产生 QQ/TTS 费用，但**主模型、观察者和模拟用户仍会调用真实 LLM**。 `--max-cost` 是费用保险丝，不是免费测试开关。默认先查看测试脚本与估价方式，再决定运行范围。

---

## 17. 开发与发布约定

- 新功能先证明它不会增加主模型不必要的阅读、协议和格式负担；
- 修改生产路径必须带对应离线测试，不能只凭一段手工对话宣称通过；
- 真实配置、私人角色卡、生产数据库、私有对话、备份包和本地路径不进入发布物；
- Release 只包含可公开复现的代码、示例卡、公开文档与验证结果。私有聊天原文、内部交接和部署备份信息不进入公开 Release。
