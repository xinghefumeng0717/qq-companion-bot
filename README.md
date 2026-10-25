# QQ 伴侣机器人 (QQ Companion Bot) v2.1.0

一个基于 Python 3.11+ 与 OneBot v11 协议的 QQ 角色扮演伴侣机器人，支持 Linux (Ubuntu) 守护运行与 Windows 本地运行。

> **给新用户的快速通道**：
> - 🤖 **想把部署交给 AI 助手？** 请打开 [`docs/DEPLOY_FOR_AI.md`](docs/DEPLOY_FOR_AI.md)：包含服务器与本地电脑双路线指引、小白到老手三档分级带教与一页速通清单。直接把该文档发给你的 AI 助手（Claude Code / Kimi Code / Cursor 等），即可由 AI 引导完成全流程。
> - 🎨 **想定制属于自己的伴侣角色？** 请阅读 [`docs/CUSTOMIZE.md`](docs/CUSTOMIZE.md)：五步替换指南与配置修改说明；角色卡编写方法论可深入参考 [`docs/CARD_CRAFT.md`](docs/CARD_CRAFT.md)。
> - 📖 **详细环境搭建参考**：请参阅 [`docs/DEPLOY.md`](docs/DEPLOY.md)。

---

## 项目设计原则

- **机主专属私聊**：基于 QQ 协议端一对一私聊，严格校验通信来源，只响应在配置中授权的机主 QQ 号。
- **人设与代码彻底解耦**：核心代码库不包含任何特定角色设定、机主个人画像或历史私有语料。仓库仅附带标准示例角色卡 `characters/example/`，可通过更换角色卡目录无缝切换人格。
- **真实手机聊天节奏**：
  - 纯聊天文字，不输出任何如 `（低头）`、`*笑了笑*` 等小说式心理、环境或动作旁白；
  - 长文本按句意自动切分，以多条短消息分段发出，并带有拟真打字延迟与随机停顿；
  - 支持接收与发送表情包（本地图库索引匹配替换）及图片识别感知。
- **结构化数值与状态演进**：
  - 六维好感度系统（温暖、信任、亲密、好奇、耐心、张力）与阶段进阶；
  - 三维 PAD 情绪模型（愉悦度、激活度、优势度），支持时间衰减与 O-U 均值回归；
  - 三层记忆体系（近期工作记忆、基于遗忘曲线的情景日记、长期语义事实）；
  - 主动消息三层调度（时间规则闸门、作息决策、候选素材匹配）。
- **开源安全规范**：本仓库经过严格脱敏，不含真实 IP、QQ 账号、个人姓名、私有卡名、私有工作路径或真实 API 密钥。

---

## v2.1.0 语音链路架构

v2.1.0 重点重构了语音合成与投递管线，遵循“**模型负责生成自然正文，程序负责选句与传输，结果以真实协议回执为准**”的设计原则：

1. **主聊只写正文**：主模型无需学习或输出任何语音控制标记，提示词无需承载语音格式包袱；彻底移除二次规整模型与失败道歉生成，杜绝套娃调用。
2. **VoicePlanner 逐字连续选句**：程序根据文本标点与语义完整性，从模型输出的正文中提取一段连续原句交由语音合成，语音部分与剩余文本片段严格保持原输出顺序发出，不做二次改写。
3. **失败原地转文字**：当 TTS 服务未开启、合成报错或 OneBot 协议端未确认送达时，选定的原句无缝原地回退为普通文本消息发送，不打断聊天流，不生成虚假的补偿解释。
4. **主聊与主动消息同源**：主动关怀消息与日常交互对聊共用同一套语音规划算法、日配额计数与真实状态追踪；普通文字消息不会冲掉最近一次有效的语音交互记录。
5. **TTS 发送锁与并发防超发**：采用互斥锁完整覆盖“配额复查 -> 语音合成 -> 协议发送 -> 成功记账”全周期，彻底避免两路消息高频并发时出现配额超发与状态竞态。
6. **真实投递状态判定**：仅在 OneBot 协议端明确接受语音消息后才确认记录为语音事件并扣减配额，确保系统记忆与聊天事实一致。

---

## 目录

1. [环境要求与依赖准备](#1-环境要求与依赖准备)
2. [NapCat 协议端部署 (Docker)](#2-napcat-协议端部署-docker)
3. [机器人配置与启动](#3-机器人配置与启动)
4. [角色卡结构与配置](#4-角色卡结构与配置)
5. [状态仪表盘与 Web 管理端](#5-状态仪表盘与-web-管理端)
6. [Systemd 服务常驻与守护](#6-systemd-服务常驻与守护)
7. [命令行工具与常用运维](#7-命令行工具与常用运维)
8. [自动化测试与二次开发](#8-自动化测试与二次开发)

---

## 1. 环境要求与依赖准备

- **操作系统**：Ubuntu 22.04 LTS（推荐）或任意安装有 Python 3.11+ 的 Linux / Windows 环境。
- **Python 运行时**：Python ≥ 3.11。
- **基础依赖**：`aiohttp`、`aiosqlite`、`Pillow`，配置解析基于 Python 3.11 标准库 `tomllib`。
  `requirements.txt` 中对依赖版本进行了完整固定，保证部署的可复现性。
- **语音扩展依赖（按需配置）**：
  - **语音输入 (ASR)**：
    - 系统工具 **ffmpeg**（`sudo apt install ffmpeg`），用于将 QQ 语音 (SILK) 解码为 WAV；
    - **SenseVoice 离线模型**（约 200MB），放置于 `data/models/sensevoice/`（包含 `model.int8.onnx` 与 `tokens.txt`）。若未配置，语音消息将优雅降级为文字提示，程序不会异常崩溃。
  - **语音输出 (TTS)**：
    - 内置支持微软 **Edge-TTS**（免配置零成本）与 **MiniMax (海螺语音)**（高质量拟真音色）。

### 基础环境初始化

```bash
# 1. 克隆代码到部署目录（以 Linux 常见路径为例）
git clone <本仓库地址> /opt/qq-companion
cd /opt/qq-companion

# 2. 创建并激活 Python 虚拟环境
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 3. 校验并校准系统时区为北京时间（避免作息与定时任务时间偏移）
sudo timedatectl set-timezone Asia/Shanghai && timedatectl
```

---

## 2. NapCat 协议端部署 (Docker)

NapCat 是 OneBot v11 协议端，运行在独立的 Docker 容器中，挂载 QQ 小号并对外提供 WebSocket 服务。

### 2.1 启动 NapCat 容器

```bash
docker run -d --name napcat --restart always \
  -e NAPCAT_GID=$(id -g) -e NAPCAT_UID=$(id -u) \
  -v ./napcat-data:/app/.config/QQ \
  -v ./napcat-cache:/app/napcat/cache \
  -p 127.0.0.1:3001:3001 -p 127.0.0.1:6099:6099 \
  mlikiowa/napcat-docker:latest
```

> **安全提示**：容器端口（`3001` 与 `6099`）仅监听服务器回环地址 `127.0.0.1`，请勿向公网开放。云服务器安全组仅需放行 SSH (`22`) 端口。

### 2.2 登录 QQ 小号与配置 WebSocket

1. **本地建立 SSH 隧道**（在开发机终端执行）：
   ```bash
   ssh -L 6099:127.0.0.1:6099 -L 8080:127.0.0.1:8080 user@your-server-ip
   ```
2. 打开本地浏览器访问 `http://127.0.0.1:6099/webui`，根据页面指引使用手机 QQ 扫描二维码登录小号。
3. 登录成功后进入 NapCat WebUI 设置，新建一个 **WebSocket 服务端**：
   - 监听端口：`3001`
   - 访问密钥 (Access Token)：自定义一个安全字符串（需与 `config.toml` 中保持一致）
   - 勾选：启用消息上报

---

## 3. 机器人配置与启动

### 3.1 配置文件生成

从模板复制配置文件：
```bash
cp config.example.toml config.toml
```

根据实际环境编辑 `config.toml`：
```toml
[account]
allowed_user_id = 123456789        # 机主大号 QQ（只响应此人，其余消息一律忽略）
bot_qq = 987654321                 # 伴侣登录的小号 QQ

[onebot]
ws_url = "ws://127.0.0.1:3001"
access_token = "与 NapCat WebUI 中配置的一致"

[llm]
current = "deepseek"               # 当前激活的模型预设名称
thinking_chat = true
thinking_effort_chat = "low"
thinking_tasks = false
thinking_tasks_purposes = ["diary_archive", "vision_perception", "proactive_message", "life_arc", "reply_check"]

[models.deepseek]
provider = "deepseek"              # 可选: deepseek / minimax / openai
base_url = "https://api.deepseek.com"
api_key = "sk-xxxxxxxx"
chat = "deepseek-chat"
vision = "deepseek-chat"           # 若模型不支持多模态可置空
tasks = "deepseek-chat"

[character]
path = "characters/example"        # 激活的角色卡目录路径

[tts]
enabled = false                    # 语音回复开关（建议文字稳定后再开启）
provider = "edge"                  # "edge" (免费) 或 "minimax"
daily_limit = 60                   # 每日语音发送条数上限
max_chars = 90                     # 单条语音最大文本长度

[admin]
host = "127.0.0.1"
port = 8080
token = ""                         # 仪表盘鉴权 Token（留空则为本地回环免密模式）
```

### 3.2 运行与冒烟验证

检查伴侣健康状态：
```bash
./venv/bin/python -m companion.main --status
```

前台运行伴侣机器人：
```bash
./venv/bin/python -m companion.main
```
启动后，使用配置的机主大号给小号发送一条测试消息，确认伴侣能正常响应分段文本。

---

## 4. 角色卡结构与配置

角色卡是伴侣人格设定的载体，位于 `characters/<角色目录>/`：

```
characters/example/
├── character.json       # 核心人格配置文件
└── stickers/            # 专属表情包图库
    ├── index.json       # 表情包索引映射
    ├── 发呆.png
    ├── 开心.png
    └── 猫猫探头.png
```

### 4.1 character.json 核心规范

```json
{
  "_schema": 1,
  "name": "苏晓棠",
  "user_address": "你",
  "core_description": "22岁，大学艺术系学生，性格温和随和，习惯用自然随意的口吻聊天。",
  "chat_style": {
    "rules": [
      "你在用手机QQ聊天，只输出聊天文字本身",
      "常用语气词：呀、嗷、呢",
      "绝对禁止输出动作心理描写"
    ],
    "good_examples": ["哈哈真的假的", "快看我刚刚画的！"],
    "plain_examples": ["机主：「在干嘛」→ 她：「吃饭」"],
    "bad_examples": ["（笑了笑）今天很开心"]
  },
  "initial_dims": {
    "warmth": 40.0, "trust": 50.0, "intimacy": 35.0,
    "intrigue": 30.0, "patience": 50.0, "tension": 3.0
  },
  "stages": [
    {
      "name": "初识",
      "tone": "礼貌而疏远",
      "instructions": ["语气客气，不会主动延伸话题"]
    }
    // 包含由浅入深的阶段进阶定义 (0~9 共 10 阶段)
  ],
  "daily_routine": [
    { "start": 0, "end": 7, "activity": "在睡梦中" },
    { "start": 7, "end": 9, "activity": "起床洗漱吃早餐" }
    // 覆盖全天 24 小时的作息锚点
  ],
  "life_arc_seed_pool": "学校、画室、图书馆、专业课、期末作业、食堂晚餐",
  "calendar_anchors": [
    ["09-01", "09-05", "新学期开学周"],
    ["12-25", "12-31", "期末复习周"]
  ],
  "stickers_dir": "stickers"
}
```

### 4.2 表情包配置 (stickers/index.json)

```json
{
  "猫猫探头": {
    "file": "猫猫探头.png",
    "desc": "好奇地探头张望"
  }
}
```
模型在回复中输出 `[sticker:猫猫探头]` 占位符时，程序将自动转换为对应的 CQ 码图片发送。

---

## 5. 状态仪表盘与 Web 管理端

伴侣内置了一个基于 aiohttp 的轻量级 Web 状态看板与管理端，默认绑定于 `127.0.0.1:8080`。

### 5.1 访问方式

- **方式 A：SSH 端口转发（推荐）**
  在本地开发机执行：
  ```bash
  ssh -L 8080:127.0.0.1:8080 user@your-server-ip
  ```
  在本地浏览器打开：`http://127.0.0.1:8080`
- **方式 B：Tailscale 私网访问**
  在服务器端执行 `sudo tailscale serve --bg http://127.0.0.1:8080`，即可在授权设备上通过私网域名访问，无需开放公网端口。

### 5.2 页面功能矩阵

- `/` **状态总览**：好感度六维雷达图（SVG 矢量绘制）、阶段关系进展、PAD 情绪心境、累计交互天数与消息统计。
- `/memory` **记忆看板**：情景日记衰减曲线展示、语义事实记忆清单、未决跟进事项。
- `/debug` **调试诊断**：最近一次交互装配的完整 System Prompt 及观察者决策原始输出。
- `/costs` **调用计费**：Token 消耗与费用实时统计（区分峰谷与业务分类）。
- `/stickers` **表情包图库**：浏览当前角色卡表情包缩略图与触发描述。
- `/logs` **系统日志**：实时滚动查看伴侣运行日志。

### 5.3 安全与管理控制台 (`/admin`)

- **访问与鉴权**：
  默认情况下仪表盘仅监听 `127.0.0.1`，适合回环或 SSH 隧道直连。当在 `config.toml` 中将 `[admin].host` 暴露或配置了 `[admin].token` 时，**所有读页面及写接口均强制校验令牌**（通过 URL 参数 `?token=xxx` 或 `X-Admin-Token` 请求头），防止敏感聊天记忆外泄。
- **运维操作**：
  在 `/admin` 页面提供运维快捷操作：
  - **立即备份**：触发在线 SQLite 热备份，生成快照并更新 `latest.db`；
  - **重启服务**：向进程发送安全退出信号，由 Systemd 守护进程重新拉起以载入最新代码或配置；
  - **重置数据**：清空所有相处记忆与好感度，需输入大写 `YES` 二次确认，并在清空前强制生成完整备份快照。

---

## 6. Systemd 服务常驻与守护

将伴侣注册为 Linux 系统服务，实现开机自启与异常自动重启：

```bash
# 复制服务配置文件
sudo cp deploy/qq-companion.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qq-companion
```

### 常用运维管理命令

- 查看运行状态：`sudo systemctl status qq-companion`
- 实时追踪日志：`tail -f /opt/qq-companion/data/logs/bot.log`
- 重启服务：`sudo systemctl restart qq-companion`
- 停止服务：`sudo systemctl stop qq-companion`

---

## 7. 命令行工具与常用运维

### 7.1 本地仿真对话沙箱 (`companion.chat`)

无需通过 QQ 协议端，可直接在终端中以机主身份与伴侣对话，验证人设表现与回复逻辑：
```bash
./venv/bin/python -m companion.chat
```
- **生产数据零污染**：启动时自动将当前生产库完整复制为临时沙箱副本 `data/chat-sandbox.db`，测试过程中的记忆沉淀与好感度更新均在沙箱内运行；
- **退出自动清理**：会话结束自动销毁临时沙箱，保障真实数据安全；
- **控制指令**：支持 `/prompt` 查看提示词、`/status` 查看即时数值状态、`/quit` 退出。

### 7.2 关系数据重置与清理 (`companion.reset`)

```bash
./venv/bin/python -m companion.reset
```
- 抹除前的全量数据自动归档至 `data/backup/daily/companion-YYYYMMDD-HHMMSS.db`；
- 默认清空聊天、日记、好感度与记忆状态，保留已下载表情包和历史计费账单；
- 使用 `--purge-all` 参数可彻底清理全部数据。

### 7.3 定时备份机制与数据回滚

- **内置自动热备**：进程内调度器每日凌晨 **04:17** 自动在线热备数据库，保留最近 14 份历史副本，并同步刷新镜像 `data/backup/daily/latest.db`；
- **启动自动补备**：进程启动时检测当日若无备份将立即执行一次补备；
- **数据回滚流程**：
  ```bash
  # 1. 停止服务
  sudo systemctl stop qq-companion

  # 2. 从备份快照恢复数据库
  cp data/backup/daily/companion-YYYYMMDD-HHMMSS.db data/companion.db

  # 3. 重新启动服务
  sudo systemctl start qq-companion
  ```

### 7.4 桌面辅助控制台 (`launcher/`)

在 `launcher/` 目录下提供了一个基于 Python 标准库 tkinter 构建的轻量级桌面控制台，适用于本地 Windows 运维：
- 具备网络隧道、Web 看板与伴侣在线状态检测；
- 支持一键拉起 SSH 隧道与打开监控页面；
- 支持通过 SCP 一键将服务器最新数据库备份拉取到本地归档。

---

## 8. 自动化测试与二次开发

本仓库具备 1000+ 项单元测试，涵盖提示词装配、模型决策解析、数值动力学衰减、记忆遗忘曲线、主动消息调度及 v2.1.0 重构后的语音规划链路。

### 8.1 执行单元测试

测试用例全部采用本地 Mock 构造，**无外部网络请求，不产生任何 API 费用**：

```bash
# Linux
./venv/bin/python -m unittest discover -s tests

# Windows (PowerShell)
.\venv\Scripts\python.exe -m unittest discover -s tests
```

### 8.2 角色卡与测试环境解耦

- 测试套件默认加载公开示例卡 `characters/example/`；
- 可通过环境变量 `QQC_TEST_CARD` 显式指定测试所用的角色卡目录：
  ```bash
  QQC_TEST_CARD=characters/example ./venv/bin/python -m unittest discover -s tests
  ```
- 涉及特定作息或阶段校验的用例会在临时目录动态构建独立的内存夹具卡，不依赖本地特定配置。

### 8.3 仿真测试跳过说明

在未生成本地私有测试简报时，运行测试套件可能会看到部分用例处于 `skipped` 状态。这是对聊仿真测试（依赖 `data/duo_sim/user_persona_brief.md`）的正常机制——该简报文件属于私有资产且被 `.gitignore` 忽略，公开仓库在缺少该简报时会自动安全跳过对应测试，不影响整体测试绿灯。

> **注意事项**：`scripts/sim/full_sandbox.py` 支持使用示例角色卡进行端到端仿真；真实 API 冒烟可能产生费用，二次开发请优先使用单元测试或本地沙箱 `companion.chat`。
