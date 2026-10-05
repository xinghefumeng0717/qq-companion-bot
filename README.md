# QQ 伴侣机器人 (QQ Companion Bot)

一个部署于 Linux (Ubuntu 22.04) 上的 24 小时运行的 **QQ 角色扮演伴侣机器人**。

> **v2.0.0 更新亮点**：回复时机人格化（首条按作息延迟 + 正在输入状态）、生活剧本系统（她的生活有连续性：提到的事会有下文）、表情包语义重标注（按"语用功能"而非画面选图）、QQ 系统表情双向收发、引用回复、语音回复（MiniMax TTS）、沉默权（她可以合法地不接话）、对聊仿真器（AI 扮演机主 × 全管道多轮冒烟）。全部功能由 1000+ 条单元测试与多轮真实 API 冒烟护航。

- **机主私聊专属**：机主使用自己的 QQ 大号与机器人登录的 QQ 小号私聊；
- **人设与代码彻底分离**：代码仓库不包含任何具体角色设定，角色完全通过外置角色卡 JSON 配置；
- **真实聊天感（最高优先级）**：绝对禁止小说式括号旁白描写，像真人在手机 QQ 聊天一样分段发送、模拟打字节奏、支持收发与收藏表情包；
- **丰富内部引擎**：六维好感度系统（数学动力学模型）、三维 PAD 连续情绪引擎（带 O-U 均值回归与冷落惩罚）、三层记忆系统（工作记忆、情景日记遗忘曲线、语义事实）、主动消息三层调度（规则闸门 + 潜意识决策 + 素材优选）。

> **想把部署交给 AI 助手？** 本仓库自带一份写给 AI 的零基础部署说明书 [`docs/DEPLOY_FOR_AI.md`](docs/DEPLOY_FOR_AI.md)：把这份文档丢给你的 AI 助手（Claude Code / Kimi Code / Cursor 等），它会带着你从装系统一路走到机器人在 QQ 上回话——你不需要懂终端，也不需要读下面这些章节。

---

## 目录

1. [第一设计原则：手机聊天感](#1-第一设计原则手机聊天感)
2. [环境准备与依赖](#2-环境准备与依赖)
3. [NapCat 协议端部署 (Docker)](#3-napcat-协议端部署-docker)
4. [机器人配置与启动](#4-机器人配置与启动)
5. [角色卡编写教程](#5-角色卡编写教程)
6. [状态仪表盘访问 (SSH 隧道)](#6-状态仪表盘访问-ssh-隧道)
7. [Systemd 服务守护与开机自启](#7-systemd-服务守护与开机自启)

---

## 1. 第一设计原则：手机聊天感

机器人必须像一个真人在手机 QQ 上聊天，**绝对禁止**小说式角色扮演风格：
- **禁止旁白**：严禁输出 `（低头）`、`*笑了笑*`、`【内心】` 等一切动作、心理或场景描写；
- **纯聊天文字**：允许口语、语气词（呀、呢、哈、呜）、emoji、短句；
- **多段打字节奏**：长回复自动按句意切段并拆成多条短消息发送，每段之间附带打字与随机延迟；
- **情绪传达**：情绪的表达渠道只有措辞、标点、emoji 和表情包图片。

---

## 2. 环境准备与依赖

- 操作系统：Ubuntu 22.04（或任意支持 Python 3.11+ 的 Linux/Windows 环境）
- Python 版本：Python ≥ 3.11
- 核心依赖：`aiohttp`、`aiosqlite`、`Pillow`（仅用于图片缩放），配置使用标准库 `tomllib`。`requirements.txt` 里全部钉了 `==` 版本（长跑服务，可复现优先）。
- 语音**输入**额外需要两样手工资产（都不会自动下载，缺了只是降级、不会崩）：
  - 系统工具 **ffmpeg**（`apt install ffmpeg`）：QQ 语音是 SILK 格式，靠它转成 wav；
  - **sherpa-onnx SenseVoice int8 模型**（约 200MB）：解压到 `data/models/sensevoice/`，该目录下须有 `model.int8.onnx` 与 `tokens.txt`（下载命令见 `docs/DEPLOY.md` 第 5 步）。

### 安装步骤

```bash
git clone <本仓库地址> /opt/qq-companion
cd /opt/qq-companion
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

> **服务器时区必须先设对**（否则作息、节假日、免打扰时段整体偏 8 小时）：
> ```bash
> sudo timedatectl set-timezone Asia/Shanghai && timedatectl
> ```
> 完整部署流程（含 Python 3.11、ffmpeg、Docker、语音模型下载）见 `docs/DEPLOY.md`。

---

## 3. NapCat 协议端部署 (Docker)

NapCat 是 QQ 协议端，运行在 Docker 容器中，挂载 QQ 小号并对外输出 OneBot v11 WebSocket 服务。

### 3.1 运行 NapCat 容器

```bash
docker run -d --name napcat --restart always \
  -e NAPCAT_GID=$(id -g) -e NAPCAT_UID=$(id -u) \
  -v ./napcat-data:/app/.config/QQ \
  -v ./napcat-cache:/app/napcat/cache \
  -p 127.0.0.1:3001:3001 -p 127.0.0.1:6099:6099 \
  mlikiowa/napcat-docker:latest
```

> **安全提示**：3001 与 6099 端口均只监听 `127.0.0.1`，云服务器安全组仅需放行 22 端口 (SSH)。

### 3.2 扫码登录与 WebSocket 配置

1. 在本地电脑开启 SSH 隧道映射端口：
   ```bash
   ssh -L 6099:127.0.0.1:6099 -L 8080:127.0.0.1:8080 user@你的服务器IP
   ```
2. 本地浏览器打开 `http://127.0.0.1:6099/webui`，根据页面指引扫码登录 QQ 小号。
3. 登录后进入 WebUI 设置，新建一个 **WebSocket 服务端**：
   - 端口：`3001`
   - Access Token：自定义一个安全密钥（需与 `config.toml` 中一致）
   - 开启消息上报

---

## 4. 机器人配置与启动

### 4.1 配置文件

复制配置模板：
```bash
cp config.example.toml config.toml
```

编辑 `config.toml`：
```toml
[account]
allowed_user_id = 123456789        # 机主大号 QQ 号（只响应此人）
bot_qq = 987654321                 # 小号 QQ 号

[onebot]
ws_url = "ws://127.0.0.1:3001"
access_token = "与 NapCat WebUI 中一致的 token"

[llm]
current = "deepseek"          # 当前激活的模型预设，切换厂商只改这一行
thinking_chat = true
thinking_effort_chat = "low"
thinking_tasks = false
thinking_effort_tasks = "low"

[models.deepseek]
provider = "deepseek"         # deepseek / minimax / openai
base_url = "https://api.deepseek.com"
api_key = "sk-xxxxxxxx"
chat = "deepseek-v4-pro"
vision = "deepseek-flash"
tasks = "deepseek-flash"

[models.minimax]
provider = "minimax"
base_url = "https://api.minimaxi.com/v1"
api_key = "xxxxxxxx"
chat = "MiniMax-M3"
vision = "MiniMax-M3"
tasks = "MiniMax-M2.7"

[character]
path = "characters/example"        # 激活的角色卡目录
```

### 4.2 验证运行

检查当前伴侣状态：
```bash
./venv/bin/python -m companion.main --status
```

首次手动前台启动：
```bash
./venv/bin/python -m companion.main
```
此时用机主大号给小号发送一条消息，应能收到伴侣以角色口吻回复的连发短消息。

---

## 5. 角色卡编写教程

> **想把它变成你自己的伴侣？** 先读 [docs/CUSTOMIZE.md](docs/CUSTOMIZE.md)（五步定制路径）。
> **方法论长文**：[docs/CARD_CRAFT.md](docs/CARD_CRAFT.md)《怎么写出不像 AI 的角色卡》——十几版迭代攒下的改卡方法（病灶驱动、反面教材库、废话流基线、阶段化人格、盲测验证）。

角色卡是伴侣灵魂的**唯一载体**，存放在 `characters/<角色名>/` 目录下：

```
characters/my_character/
├── character.json       # 角色卡核心配置文件
└── stickers/            # 角色专属表情包目录
    ├── index.json       # 表情包索引
    ├── 探头.png
    └── 开心.png
```

### 5.1 character.json 结构说明

```json
{
  "_schema": 1,
  "name": "苏晓棠",
  "user_address": "你",
  "core_description": "22岁，插画专业大四学生。性格温和有灵气，喜欢在窗边画画喝茶。",
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
    // ...必须恰好 10 个阶段 (0~9)
  ],
  "daily_routine": [
    { "start": 0, "end": 7, "activity": "在睡梦中" },
    { "start": 7, "end": 9, "activity": "起床洗漱喝温水" }
    // ...覆盖全天 24 小时
  ],
  "personal_memories": [
    { "title": "童年老屋", "content": "老家院子里有一棵很大的柿子树", "emotion": "怀念" }
  ],
  "habits": ["画画卡壳时会咬笔帽"],
  "stickers_dir": "stickers"
}
```

### 5.1.1 三个可选字段（卡内容的落脚点）

这三项以前硬编码在代码里，现在**只从角色卡读**（公开代码里没有任何具体角色设定）。
不填就是"功能关掉"，绝不会报错；照抄 `characters/example/character.json` 里的示例改成你自己的即可。

| 字段 | 作用 | 不填时 |
|---|---|---|
| `long_holiday_activity` | 长假（连续法定节假日 ≥4 天）当天的活动文案。长假期间不匹配 `daily_routine`，看板与提示词统一用这一句 | 用通用默认「放长假中，回老家，不在学校」 |
| `calendar_anchors` | 日历/校历锚点：`[["起(MM-DD)", "止(MM-DD)", "一句话场景"], ...]`。生活主线生成时注入"当前锚点 + 下一个锚点"，让你编出来的事踩在真实节奏上。止 < 起 表示跨年区间（如 `["12-31","01-06","元旦假期"]`） | 空 = 无锚点功能（生成时提示词注明"锚点缺失"，只按月份节奏来） |
| `life_arc_seed_pool` | 生活主线的**取材范围**：她能提到的课程、场所、烦恼等事实清单（整段字符串，或字符串数组按行拼接）。 | 空 = **整条跳过生活主线生成**（宁可没有主线，也不让模型凭空编卡里不存在的事） |

> 想让生活主线真的跑起来，`life_arc_seed_pool` 必填；`calendar_anchors` 建议填满全年（每一天都能落到某段锚点），
> 否则那些天生成时只能按"现在是几月"大致感觉。

### 5.2 表情包配置 (stickers/index.json)

```json
{
  "猫猫探头": {
    "file": "猫猫探头.png",
    "desc": "好奇地探头张望"
  }
}
```
当模型在回复中输出 `[sticker:猫猫探头]` 时，机器人将自动将其替换为图片消息段混排发送。

---

## 6. 状态仪表盘访问 (SSH 隧道)

为保障安全，仪表盘 HTTP 服务仅绑定 `127.0.0.1:8080`，不对外网开放。

### 访问方式

在本地电脑终端执行：
```bash
ssh -L 8080:127.0.0.1:8080 user@你的服务器IP
```
连接成功后，在本地浏览器打开：`http://localhost:8080`

### 页面功能

- `/` **总览**：好感度六维雷达图（内联 SVG）、阶段进阶进度条、六维进度条、关系档案（认识天数、累计对话、里程碑时间线）、PAD 情绪与安心度、主动消息未回计数；
- `/memory` **记忆**：情景日记列表（含实时遗忘曲线强度与衰减进度条）、语义记忆清单、待跟进事项、欲言又止池；
- `/debug` **调试**：最近一次发给模型的完整 system prompt、最近 5 次观察者原始 JSON；
- `/costs` **计费**：API 调用次数与 Token 费用累计、按业务分类统计、近 24 小时开销；
- `/stickers` **表情包**：表情包图库缩略图浏览与描述；
- `/logs` **日志**：实时查看 `data/logs/bot.log` 最近 200 行运行日志。

> **安全提示**：仪表盘默认只监听 `127.0.0.1`，页面本身零鉴权（读的是记忆、日志、计费与关系状态），
> 靠"不对外开放 + SSH 隧道"防护。若你把 `[admin].host` 改成非回环地址，**必须同时在
> `[admin].token` 设一个令牌**——届时**所有页面与 `/api/status` 都要凭令牌访问**
> （URL 加 `?token=你的令牌` 或请求头 `X-Admin-Token`；页面内链接会自动带上令牌）。
> 令牌留空（默认）时一切照旧，不需要任何参数。详见第 10 节。

---

## 7. Systemd 服务守护与开机自启

### 7.1 配置服务

将 `deploy/qq-companion.service` 复制到 systemd 目录：
```bash
sudo cp deploy/qq-companion.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qq-companion
```

### 7.2 运维常用命令

- 查看运行状态：`sudo systemctl status qq-companion`
- 实时查看日志：`tail -f /opt/qq-companion/data/logs/bot.log`
- 重启机器人服务：`sudo systemctl restart qq-companion`
- 停止服务：`sudo systemctl stop qq-companion`

---

## 8. 工具与辅助命令 (CLI Tools)

### 8.1 仿真对话沙箱 (`companion.chat`)

无需打开 QQ，直接在终端与伴侣全真对话验证人设文笔与交互逻辑：
```bash
./venv/bin/python -m companion.chat
```
- **生产数据零副作用**：启动时自动将 `data/companion.db` 复制为临时沙箱副本 `data/chat-sandbox.db`，全部引擎（记忆、好感、情绪、观察者结算）均在沙箱正常运转；
- **自动清理**：退出时自动清理沙箱文件，生产库数据毫发无伤；
- **内置指令**：
  - `/prompt`：查看最近一次组装的完整 System Prompt；
  - `/status`：查看当前好感度六维与 PAD 心境；
  - `/quit`：退出仿真会话。

### 8.2 数据重置与备份 (`companion.reset`)

一键清空关系数据，从头开始相处：
```bash
./venv/bin/python -m companion.reset
```
- **自动备份**：清空前自动将当前数据库完整备份到 `data/backup/daily/companion-YYYYMMDD-HHMMSS.db`（与该目录的 14 份轮转共用）；
- **清空范围**：清空聊天记录、日记、语义记忆、待跟进事项、好感与情绪状态；默认**保留**表情包图库 (`stickers`) 与计费历史 (`llm_calls`)；
- **参数说明**：
  - `--purge-all`：连同表情包与计费记录彻底清空；
  - `--yes` 或 `-y`：跳过交互式二次确认。

### 8.3 状态看板查看 (`companion.main --status`)

在终端中快速以字符进度条查看当前伴侣好感度、心境、记住的事实与最新日记：
```bash
./venv/Scripts/python.exe -m companion.main --status
```

---

## 9. 青梓小窝 · Windows 桌面控制台 (launcher/)

为机主本地 Windows 电脑开发的轻量级深色桌面小控制台，无需第三方依赖（基于 Python 标准库 tkinter），降低日常运维监控与人设调试门槛。

### 9.1 创建桌面快捷方式

在本地项目根目录运行以下 PowerShell 脚本：
```powershell
powershell -ExecutionPolicy Bypass -File ".\launcher\创建桌面快捷方式.ps1"
```
脚本将自动在你的 Windows 桌面上创建「青梓小窝」快捷方式，并绑定 `pythonw.exe` 实现**后台静默启动（无黑控制台窗口）**。

### 9.2 控制台主要功能

1. **三盏状态监控灯（每 10 秒自动轮询）**：
   - **隧道灯**：检测本地 `127.0.0.1:8080` 端口转发是否通畅；
   - **看板灯**：检测 Web 仪表盘是否能够正常返回网页；
   - **伴侣灯**：检测伴侣进程及 OneBot (NapCat) 协议端是否在线。
2. **一键连接/断开 SSH 隧道**：后台静默拉起 `ssh -N -L 8080:127.0.0.1:8080`，退出程序时自动安全终止隧道进程；
3. **打开监控看板**：一键调用系统默认浏览器打开 `http://localhost:8080`；
4. **一键拉取备份**：通过 SCP 自动将服务器最新备份 `latest.db` 下载到本地 `data/backups/latest.db`，并更新记录上次同步时刻；
5. **仿真对话**：新开 CMD 终端直接连入服务器进入 `companion.chat` 沙箱对话，随时调试人设文笔；
6. **掉线提醒**：当检测到机器人掉线时，界面伴侣灯变红并在桌面弹出提示框：“青梓掉线了，该去 NapCat 扫码了”（每 10 分钟最多提醒一次）；
7. **快捷链接**：一键打开项目本地文件目录与 `/logs` 运行日志页面。

> **注意**：如果你给服务器设了 `[admin].token` 令牌，小窝的状态灯、看板按钮与掉线提醒会因为
> 拿不到令牌而报"看板不可达"（这是刻意的：仪表盘连读都要求令牌）。此时用浏览器
> 打开 `http://localhost:8080/?token=你的令牌` 照常能看；给小窝加"带令牌访问"是后续待办。

---

## 10. 管理控制台 (`/admin`) 与状态 API (`/api/status`)

### 10.1 状态 API (`GET /api/status`)

返回当前机器人的运行健康度、关系复合分与开销状态（JSON 格式）：
```json
{
  "bot_alive": true,
  "onebot_connected": true,
  "stage_name": "相识",
  "composite": 23.4,
  "today_cost": 0.42,
  "last_backup_time": "2026-09-30 04:17",
  "uptime_minutes": 735
}
```

> `/api/status` 与仪表盘各页面同一条鉴权规则：`[admin].token` 为空时裸读（本地/隧道模式）；
> 设了令牌后要带 `?token=` 或 `X-Admin-Token`（它带着 `stage_name`/`composite`/`today_cost`，
> 不只是"活着没"的健康检查，所以没有开例外）。

### 10.2 `/admin` 管理控制台

在浏览器访问 `http://localhost:8080/admin`，支持三大核心日常运维操作：
- **立即备份**：立刻对当前生产数据库执行在线热备份，生成带时间戳快照并同步更新 `latest.db`；
- **重启服务**：向服务端发出重启请求，进程将在 5 秒后安全退出，由 Linux systemd 守护进程 (`Restart=always`) 重新自动拉起，实现代码与配置即时热生效；
- **重置数据 (高危)**：清空所有关系好感与对话记忆，恢复到初始相识阶段。**执行前强制要求手敲输入大写 `YES` 校验**，且会自动在 `data/backup/daily/` 生成一份完整前置快照，表情包资产与调用计费记录默认安全保留。

> **安全提示**：默认只靠"仅监听 `127.0.0.1` + `confirm=YES`"防护。若把 `[admin].host` 改成非回环地址（如 `0.0.0.0`、内网 IP），**必须同时在 `[admin].token` 设一个令牌**——届时：
> - **写操作**（`/admin/backup|restart|reset`）要带请求头 `X-Admin-Token` 或表单/JSON 里的 `token` 字段（管理页表单已自带隐藏域，浏览器直接用即可）；
> - **读页面**（`/`、`/memory`、`/debug`、`/costs`、`/stickers`、`/logs`、`/admin` 与 `/api/status`）同样要带 `?token=` 或请求头——读侧泄漏的是记忆、日志、计费与关系状态，不能只挡写不挡读；页面内链接与表情包图片会自动带上令牌，点一下不会掉线；
> 否则一律 403。**token 留空（默认）时行为与旧版完全一致**：读写全部放行、页面里不多出任何令牌字样，日常 SSH 隧道访问零打扰。

---

## 11. 应用内每日自动备份 (`companion/backup`)

不依赖 Linux 系统 crontab，在伴侣机器人进程内部由 asyncio 调度器接管：
- **定时热备**：每天凌晨 **04:17** 自动执行 SQLite 在线一致性备份到 `data/backup/daily/companion-YYYYMMDD-HHMM.db`；
- **启动补备**：进程启动时会自动检测当天是否已有备份，若尚未备份则立刻补做一次；
- **自动轮转淘汰**：保留最近 **14 份**历史每日备份，更早的旧备份自动淘汰清理；
- **固定同步镜像**：每次备份完成后同步覆盖 `data/backup/daily/latest.db`，方便桌面控制台及外部工具固定路径拉取。

---

## 12. 开发者：如何跑测试

> 拿到公开 clone（没有私有角色卡、没有 `config.toml`、没有 `data/`）时先看这一节。

### 12.1 运行整套单元测试

本机（Windows，PowerShell/CMD 或 Git Bash 均可，在项目根目录执行）：

```bash
./venv/Scripts/python.exe -m unittest discover -s tests
```

服务器（Linux）：

```bash
./venv/bin/python -m unittest discover -s tests
```

全部用例本地构造、**零真实网络与真实 API 调用**，正常几分钟内跑完。

### 12.2 角色卡依赖（公开 clone 无需私有卡）

代码仓库只带公开模板卡 `characters/example/`，私有卡（如 `characters/qingzi/`）被 `.gitignore` 排除。测试对角色卡的解析优先级是：

1. 环境变量 `QQC_TEST_CARD`（显式指定要用的卡目录）；
2. `characters/qingzi/` 若存在则用它（所有者本机保持原有覆盖）；
3. 回落到仓库自带的 `characters/example/`。

因此公开 clone 直接跑测试就会自动用 `characters/example/`。想显式指定某张卡：

```bash
# Git Bash
QQC_TEST_CARD=characters/example ./venv/Scripts/python.exe -m unittest discover -s tests
```

```powershell
# PowerShell
$env:QQC_TEST_CARD="characters/example"; ./venv/Scripts/python.exe -m unittest discover -s tests
```

少数需要核对"阶段名 / 作息文案"的用例不依赖任何真实卡，而是当场在临时目录里写一张最小夹具卡（`tests/helpers.py` 的 `make_fixture_card()`），所以换卡、无卡都不会再牵动它们。

### 12.3 为什么会看到一批 skip（跳过）

公开 clone 里通常会有约二三十条用例显示 `skipped`，原因只有一个：**对聊仿真器（duo_sim / final_rehearsal）依赖私有画像简报** `data/duo_sim/user_persona_brief.md`。该简报由私有 QQ 语料经下面的脚本生成，落在被 `.gitignore` 排除的 `data/` 下，**不会入库**：

```bash
./venv/Scripts/python.exe scripts/sim/duo_sim_persona.py --export "导出文件路径"
```

生成简报后这些用例会真正执行；没生成时它们统一 skip，而不是 FAIL/ERROR——这是刻意设计，避免公开 clone 一开箱就是红。

### 12.4 `scripts/smoke/` 是真实 API 冒烟，别随手跑

`scripts/smoke/` 下的脚本会调用**真实**的 LLM / TTS 接口，产生真实费用，请勿在无预期时批量运行。需要验证提示词人设或端到端管线时，优先用零成本的仿真沙箱 `companion.chat`（见 8.1）与上面的单元测试。

