# QQ 伴侣机器人 (QQ Companion Bot)

[![tests](https://github.com/xinghefumeng0717/qq-companion-bot/actions/workflows/tests.yml/badge.svg)](https://github.com/xinghefumeng0717/qq-companion-bot/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)

一个住在 QQ 私聊里的角色伴侣。她不是一问一答的客服：她有自己的作息和生活，会记得你说过的事，有情绪、有关系进展，会在合适的时候主动找你，看得懂你发的图，也能听语音、发语音。

人设全部写在角色卡里，代码里没有任何特定角色——换一张卡，就是另一个人。

基于 Python 3.11+ 与 OneBot v11 协议（NapCat），支持 Linux 服务器 24 小时守护运行，也支持在自己的 Windows 电脑上运行。

> **从哪开始**
> - 🤖 **想让 AI 助手带你部署？** 把 [`docs/DEPLOY_FOR_AI.md`](docs/DEPLOY_FOR_AI.md) 发给你的 AI 助手（Claude Code / Kimi Code / Cursor 等）。它包含服务器与本地电脑两条路线、从小白到老手三档带法和一页速通清单，AI 会一步步带你做完。
> - 🎨 **想做一个属于自己的角色？** 看 [`docs/CUSTOMIZE.md`](docs/CUSTOMIZE.md)（五步换角色）；角色卡怎么写得像真人，见 [`docs/CARD_CRAFT.md`](docs/CARD_CRAFT.md)。
> - 📖 **想自己从头搭？** 往下读本文第 1~6 节，更细的环境说明见 [`docs/DEPLOY.md`](docs/DEPLOY.md)。
> - 🔍 **想知道她是怎么"活"起来的？** 看原理篇 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)：一条消息的旅程、为什么不秒回、遗忘曲线、情绪与好感、主动消息、语音选句，带图。

---

## 她能做什么

**像真人一样聊天**
- 只发聊天文字，不写 `（低头）`、`*笑了笑*` 这类小说旁白；
- 长回复按意思拆成几条短消息，带"正在输入"和打字停顿；
- 不秒回：第一条回复按她当时在做什么延迟（忙的时候 1~10 分钟，闲的时候几秒到半分钟），聊开了就正常接话；
- 会引用你的某一句来回、会用 QQ 小黄脸、会发角色卡自带的表情包，也会收藏你发来的表情包。

**记得你**
- 三层记忆：最近的对话、按遗忘曲线淡出的日记、长期记住的关于你的事；
- 可选开启：记住她自己说过的事（前后对得上）、记下你们约好的见面并在之后补一段纪要、对你形成"看法"（只影响她怎么接话，不说出口）。

**有自己的生活**
- 角色卡里的作息表决定她此刻在干什么，忙的时候回得慢、说话也简短；
- 生活主线：她自己会有"接下来几天要做的事"，聊天时自然带出；
- 世界书：她所在世界的设定按关键词带入提示词，聊到了才出现，不会照搬整本设定。

**有情绪，关系会变化**
- 三维情绪（愉悦、激活、安心）加一个"冷落感"，受对话影响会起伏，也会随时间回落到她的基调；你很久不理她，她会真的闹情绪；
- 六维好感度（温暖、信任、亲密、好奇、耐心、张力）与 10 个关系阶段，不同阶段说话方式不同。

**会主动找你**
- 隔一段时间"醒来"看看要不要找你，有话才说；
- 免打扰时段不发；连发两条你没回，当天就不再发；你好几天不回会降频，再久就停，你一回话就恢复。

**看图、听语音、发语音**
- 看图：能直接看图的模型一步完成，或先看图再转述给主聊模型；
- 听语音：本地离线识别（SenseVoice），不上传音频；
- 发语音：支持免费的微软 Edge-TTS 和音色更好的 MiniMax，默认关闭，文字稳定后再开。

**安全兜底**
- 你说出明显的危机信号（如"不想活"）时，提示词会切到安全指引；撒娇话（"想死你了"）不会误伤。这条规则不可关闭。

**看板与小窝**
- 网页看板：好感度雷达、情绪、记忆、调用费用、表情包、日志一目了然；
- 桌面小窝：在自己电脑上一键连服务器、看她在不在线、打开看板、拉备份、开沙盒试聊。

---

## 设计原则

- **只认机主一个人**：一对一私聊，严格校验消息来源，只响应配置里授权的 QQ 号，其余一律忽略。
- **人设与代码彻底解耦**：仓库只带通用示例卡 `characters/example/`，不含任何特定角色、机主画像或私人聊天语料。
- **结果以真实回执为准**：语音、表情包、引用是否发出去，以 QQ 协议端的回执为准，她不会说"发了"其实没发。
- **新功能先默认关闭**：语音回复、自我记忆、见面纪要、多候选等功能都默认关，确认文字聊天稳定后再逐个打开。
- **开源安全**：仓库已脱敏，不含真实 IP、QQ 号、个人姓名、私有卡名、私有路径或 API 密钥。

---

## 语音是怎么发出去的（v2.1.0）

v2.1.0 重构了语音合成与投递链路，原则是"**模型只写自然正文，程序负责选句和传输，结果以真实协议回执为准**"：

1. **主聊只写正文**：模型不需要学任何语音标记，提示词里没有语音格式的负担；不再有二次整理格式的模型调用，也不再生成"语音没发出去"的道歉话。
2. **程序从原话里逐字选句**：`VoicePlanner` 按标点和语义完整性，从她的回复里挑一段连续的原句去合成；语音和其余文字按原来的顺序发出，不改写。
3. **失败就原地转文字**：TTS 没开、合成失败、QQ 没确认收到时，选中的那句原样当文字发出，聊天不中断，也不会多出一句编的解释。
4. **主聊和主动消息同一套**：两边共用语音规划、每日条数上限和投递记录；普通文字消息不会冲掉最近一次真实的语音记录。
5. **并发不超发**：一把锁覆盖"复查额度 → 合成 → 发送 → 记账"全过程，两路消息同时到也不会超出每日上限。
6. **只记真发出去的**：只有 QQ 协议端明确收下语音，才记为一次语音并扣额度，她的记忆和实际聊天记录一致。

选句规则和流程图见原理篇 [第 10 节](docs/ARCHITECTURE.md#10-语音是怎么挑出来的)，版本变化见 [`docs/RELEASE_NOTES_v2.1.0.md`](docs/RELEASE_NOTES_v2.1.0.md)。

---

## 目录

1. [环境要求与依赖准备](#1-环境要求与依赖准备)
2. [NapCat 协议端部署 (Docker)](#2-napcat-协议端部署-docker)
3. [机器人配置与启动](#3-机器人配置与启动)
4. [角色卡结构与配置](#4-角色卡结构与配置)
5. [状态看板与 Web 管理端](#5-状态看板与-web-管理端)
6. [Systemd 服务常驻与守护](#6-systemd-服务常驻与守护)
7. [命令行工具与常用运维](#7-命令行工具与常用运维)
8. [自动化测试与二次开发](#8-自动化测试与二次开发)
9. [项目结构](#9-项目结构)
10. [常见问题](#10-常见问题)
11. [许可证与致谢](#11-许可证与致谢)

---

## 1. 环境要求与依赖准备

- **操作系统**：Ubuntu 22.04 LTS（推荐），或任意装有 Python 3.11+ 的 Linux / Windows。
- **Python**：≥ 3.11（配置解析用标准库 `tomllib`）。
- **基础依赖**：`aiohttp`、`aiosqlite`、`Pillow` 等，版本在 `requirements.txt` 里全部固定，保证部署可复现。
- **语音（按需）**：
  - **听语音 (ASR)**：系统工具 **ffmpeg**（`sudo apt install ffmpeg`，把 QQ 语音解码成 WAV）+ **SenseVoice 离线模型**（约 200MB，放在 `data/models/sensevoice/`，含 `model.int8.onnx` 和 `tokens.txt`）。没装也能跑，语音消息会降级成文字提示。
  - **发语音 (TTS)**：内置微软 **Edge-TTS**（免费、免配置）和 **MiniMax（海螺语音）**（音色更自然）。

### 基础环境初始化

```bash
# 1. 克隆代码到部署目录（以 Linux 常见路径为例）
git clone https://github.com/xinghefumeng0717/qq-companion-bot.git /opt/qq-companion
cd /opt/qq-companion

# 2. 创建 Python 虚拟环境并安装依赖
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 3. 把系统时区设为北京时间（否则她的作息和定时任务会整体偏移）
sudo timedatectl set-timezone Asia/Shanghai && timedatectl
```

---

## 2. NapCat 协议端部署 (Docker)

NapCat 是 OneBot v11 协议端，跑在独立的 Docker 容器里，负责登录 QQ 小号并提供 WebSocket 服务。

### 2.1 启动 NapCat 容器

```bash
docker run -d --name napcat --restart always \
  -e NAPCAT_GID=$(id -g) -e NAPCAT_UID=$(id -u) \
  -v ./napcat-data:/app/.config/QQ \
  -v ./napcat-cache:/app/napcat/cache \
  -p 127.0.0.1:3001:3001 -p 127.0.0.1:6099:6099 \
  mlikiowa/napcat-docker:latest
```

> **安全提示**：`3001` 和 `6099` 只绑在本机回环地址 `127.0.0.1`，不要对公网开放。云服务器安全组只需要放行 SSH (`22`)。

### 2.2 登录 QQ 小号并配置 WebSocket

1. **在自己电脑上建 SSH 隧道**：
   ```bash
   ssh -L 6099:127.0.0.1:6099 -L 8080:127.0.0.1:8080 user@your-server-ip
   ```
2. 浏览器打开 `http://127.0.0.1:6099/webui`，用手机 QQ 扫码登录小号。
3. 登录后在 NapCat WebUI 里新建一个 **WebSocket 服务端**：
   - 监听端口：`3001`
   - 访问密钥 (Access Token)：自己定一个安全字符串（要和 `config.toml` 里一致）
   - 勾选：启用消息上报

---

## 3. 机器人配置与启动

### 3.1 生成配置文件

```bash
cp config.example.toml config.toml
```

`config.example.toml` 里每一项都有中文注释。第一次跑起来，**只需要改下面这几处**：

```toml
[account]
allowed_user_id = 123456789        # 你的大号 QQ（她只回这个人）
bot_qq = 987654321                 # 她登录的小号 QQ

[onebot]
ws_url = "ws://127.0.0.1:3001"
access_token = "与 NapCat WebUI 中配置的一致"

[llm]
current = "deepseek"               # 用哪一套模型预设，换模型只改这一行

[models.deepseek]
api_key = "sk-xxxxxxxx"            # 你的 API Key

[character]
path = "characters/example"        # 用哪张角色卡

[tts]
enabled = false                    # 语音回复先关着，文字稳定后再开
```

其余常用设置（都在 `config.example.toml` 里，有注释）：

| 位置 | 管什么 |
|---|---|
| `[models.deepseek]` / `[models.minimax]` / `[models.seed]` | 三套模型预设，各自填 `api_key`；`chat` 主聊、`vision` 看图、`tasks` 后台任务 |
| `[character]` 下的 `self_memory` / `meetup_recap` / `fact_layers` / `impressions` | 自我记忆、见面纪要、事实分层、对你的看法（默认关；也可以由角色卡自己声明） |
| `[reply]` | 拆几条发、多候选择优、首泡回声裁剪、一步看图、语音频率等 |
| `[proactive]` | 主动消息：唤醒间隔、免打扰时段、没回几条就停 |
| `[timing]` | 首条回复延迟、"正在输入"时长；两个开关都关 = 秒回 |
| `[tts]` | 语音引擎、音色、每日上限、单条字数上限 |
| `[admin]` | 看板监听地址、访问令牌、主题 |

> 没有 `config.toml` 时程序会退回读 `config.example.toml`，表现是"不报错但她不理你"。部署后务必确认 `config.toml` 存在、QQ 号填对。

### 3.2 运行与冒烟验证

查看她的状态（不启动聊天）：
```bash
./venv/bin/python -m companion.main --status
```

前台运行：
```bash
./venv/bin/python -m companion.main
```
用你的大号给小号发一条消息。第一条回复可能要等一会儿（见[常见问题](#10-常见问题)），收到分段回复就说明跑通了。

---

## 4. 角色卡结构与配置

角色卡是她的人格载体，放在 `characters/<角色目录>/`：

```
characters/example/
├── character.json       # 核心人格配置
└── stickers/            # 她的表情包
    ├── index.json       # 表情包索引
    ├── 发呆.png
    ├── 开心.png
    └── 猫猫探头.png
```

卡目录里还可以放（都是可选的）：
- `lorebook.json`：世界书，她所在世界的设定词条，按关键词带入；
- `theme/skin.json`：看板和小窝的皮肤（头像、配色、光标等）。

### 4.1 character.json 核心字段

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
    // 由浅入深共 10 个阶段（0~9）
  ],
  "daily_routine": [
    { "start": 0, "end": 7, "activity": "在睡梦中" },
    { "start": 7, "end": 9, "activity": "起床洗漱吃早餐" }
    // 覆盖全天 24 小时
  ],
  "life_arc_seed_pool": "学校、画室、图书馆、专业课、期末作业、食堂晚餐",
  "calendar_anchors": [
    ["09-01", "09-05", "新学期开学周"],
    ["12-25", "12-31", "期末复习周"]
  ],
  "stickers_dir": "stickers"
}
```

完整字段与写法见 [`docs/CUSTOMIZE.md`](docs/CUSTOMIZE.md) 和 [`docs/CARD_CRAFT.md`](docs/CARD_CRAFT.md)。

### 4.2 表情包 (stickers/index.json)

```json
{
  "猫猫探头": {
    "file": "猫猫探头.png",
    "desc": "好奇地探头张望"
  }
}
```
她在回复里写 `[sticker:猫猫探头]`，程序会把它换成对应的图片消息发出去。`desc` 写清楚这张图表达什么情绪，她才会用在对的地方。

---

## 5. 状态看板与 Web 管理端

内置一个轻量网页看板，默认只监听 `127.0.0.1:8080`。

### 5.1 怎么打开

- **SSH 端口转发（推荐）**：在自己电脑上执行
  ```bash
  ssh -L 8080:127.0.0.1:8080 user@your-server-ip
  ```
  然后浏览器打开 `http://127.0.0.1:8080`。
- **Tailscale 私网**：服务器上执行 `sudo tailscale serve --bg http://127.0.0.1:8080`，授权设备即可通过私网域名访问，不用开公网端口。

### 5.2 页面

- `/` **总览**：好感度六维雷达、关系阶段、情绪、相处天数与消息统计；
- `/memory` **记忆**：日记与遗忘曲线、记住的事、待跟进事项；
- `/debug` **调试**：最近一轮完整的系统提示词和观察者的原始输出；
- `/costs` **费用**：Token 用量与费用（区分峰谷时段和用途）；
- `/stickers` **表情包**：当前角色卡的表情包和描述；
- `/logs` **日志**：实时滚动的运行日志；
- `/api/status`：给脚本或监控用的 JSON 状态接口。

看板有三种主题：`journal`（手账纸，默认）、`plain`（白底）、`endfield`（终末地工业风）。不写 `[admin].theme` 时跟随角色卡：卡里有 `theme/skin.json` 就用皮肤对应的主题。

### 5.3 管理页 (`/admin`) 与安全

- **鉴权**：默认只监听本机，适合 SSH 隧道访问。如果把 `[admin].host` 改成非回环地址，**必须先设置 `[admin].token`**——设置后所有读页面和写操作都要带令牌（URL 参数 `?token=xxx` 或请求头 `X-Admin-Token`），否则一律 403，防止聊天记忆外泄。
- **运维操作**：
  - **立即备份**：在线热备数据库，生成快照并更新 `latest.db`；
  - **重启服务**：安全退出进程，由 Systemd 重新拉起，载入新代码或配置；
  - **重置数据**：清空相处记忆与好感度，需输入大写 `YES` 确认，清空前强制备份。

---

## 6. Systemd 服务常驻与守护

把她注册成系统服务，开机自启、异常自动重启：

```bash
mkdir -p /opt/qq-companion/data/logs
sudo cp deploy/qq-companion.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qq-companion
```

> 服务文件里写的是 `User=ubuntu`、路径 `/opt/qq-companion`，用户名或路径不同要先改。

### 常用命令

- 查看状态：`sudo systemctl status qq-companion`
- 实时日志：`tail -f /opt/qq-companion/data/logs/bot.log`
- 重启：`sudo systemctl restart qq-companion`
- 停止：`sudo systemctl stop qq-companion`

---

## 7. 命令行工具与常用运维

### 7.1 终端里直接和她聊 (`companion.chat`)

不经过 QQ，在终端里以机主身份和她对话，用来试人设、试回复：
```bash
./venv/bin/python -m companion.chat
```
- **不污染真实数据**：启动时把当前数据库复制成临时副本 `data/chat-sandbox.db`，聊天产生的记忆和好感变化都只在副本里；
- **退出自动清理**：结束后副本自动删除；
- **内置指令**：`/prompt` 看提示词、`/status` 看数值、`/quit` 退出。

### 7.2 重置关系数据 (`companion.reset`)

```bash
./venv/bin/python -m companion.reset
```
- 清空前自动全量备份到 `data/backup/daily/companion-YYYYMMDD-HHMMSS.db`；
- 默认清空聊天、日记、好感度与记忆，保留已收藏的表情包和历史计费；
- 加 `--purge-all` 彻底清空全部数据。

### 7.3 自动备份与回滚

- **每日热备**：每天凌晨 **04:17** 自动在线备份，保留最近 14 份，并刷新 `data/backup/daily/latest.db`；
- **启动补备**：启动时发现当天还没备份，会立即补一份；
- **回滚**：
  ```bash
  # 1. 停止服务
  sudo systemctl stop qq-companion

  # 2. 用备份快照覆盖数据库
  cp data/backup/daily/companion-YYYYMMDD-HHMMSS.db data/companion.db

  # 3. 重新启动
  sudo systemctl start qq-companion
  ```

### 7.4 桌面小窝 (`launcher/home.py`)

在自己的 Windows 电脑上用的控制台。界面是本机的一个小网页，优先用 Edge 应用模式弹成独立窗口：
- 一键连 SSH 隧道，看三盏灯：隧道、看板、她是否在线；
- 显示她此刻的心情和关系阶段，一键打开看板；
- 一键把服务器上的最新备份拉回本地；
- 开沙盒试聊。

她的名字、头像和皮肤从本地 `config.toml` 指向的角色卡里读，换卡后小窝跟着变。只监听 `127.0.0.1`，按钮操作带本次启动随机生成的口令；窗口关掉 90 秒后自动退出并断开隧道。

```powershell
pythonw launcher\home.py              # 启动并弹出窗口
python  launcher\home.py --shortcut   # 在桌面建「<她的名字>的小窝」快捷方式
```

> `launcher/qingzi_home.py` 是早期的 tkinter 版，功能相同，保留作备用。

---

## 8. 自动化测试与二次开发

仓库自带上千项单元测试，覆盖提示词装配、模型输出解析、数值与情绪变化、记忆遗忘曲线、主动消息调度和语音规划链路。

### 8.1 运行测试

测试全部用本地 Mock，**不联网、不产生任何 API 费用**：

```bash
# Linux
./venv/bin/python -m unittest discover -s tests

# Windows (PowerShell)
.\venv\Scripts\python.exe -m unittest discover -s tests
```

每次推送，GitHub Actions 会把 `tests/` 下的每个测试文件拆成独立任务并行跑。**新加的测试请用标准库 `unittest` 写**（继承 `unittest.TestCase`），CI 环境不安装 pytest。

### 8.2 角色卡与测试解耦

- 测试默认加载公开示例卡 `characters/example/`；
- 可以用环境变量 `QQC_TEST_CARD` 指定测试用的角色卡：
  ```bash
  QQC_TEST_CARD=characters/example ./venv/bin/python -m unittest discover -s tests
  ```
- 依赖特定作息或阶段的用例，会在临时目录里动态生成专用的夹具卡，不依赖本地配置。

### 8.3 关于跳过的测试

运行时看到少量 `skipped` 是正常的：对聊仿真测试依赖私有的 `data/duo_sim/user_persona_brief.md`（已被 `.gitignore` 忽略），缺这个文件时会自动跳过，不影响整体通过。

### 8.4 全流程沙箱

`scripts/sim/full_sandbox.py` 会起一个和生产同款的机器人，配上假 QQ、假语音合成和假时钟，把几天的剧情（讨要语音、合成失败、约见面、重启后考记忆等）一次跑完并自动打分。它会调用真实模型 API，**会产生少量费用**；日常开发优先用单元测试和 `companion.chat`。

---

## 9. 项目结构

```
companion/            # 机器人本体
  main.py             #   入口与消息收发
  turn_handler.py     #   一轮对话的完整处理流程
  assembler.py        #   系统提示词装配
  prompts.py          #   提示词文本
  replier.py          #   回复解析与分段
  memory.py           #   三层记忆
  mood.py             #   PAD 情绪
  affection.py        #   六维好感与阶段
  proactive.py        #   主动消息调度
  observer.py         #   每轮之后的观察与记录
  persona.py          #   作息与当前活动
  arcs.py             #   生活主线
  lorebook.py         #   世界书
  meetups.py          #   见面约定与纪要
  voice.py / tts.py / voice_planner.py   # 听语音 / 发语音 / 语音选句
  admin.py            #   看板与管理页
  chat.py / reset.py  #   终端试聊 / 重置数据
characters/example/   # 通用示例角色卡
launcher/             # 桌面小窝
deploy/               # systemd 服务文件
scripts/              # 仿真、评测、审计等开发工具
tests/                # 单元测试
docs/                 # 部署、定制、角色卡写法等文档
config.example.toml   # 配置模板（每项都有注释）
```

---

## 10. 常见问题

**发了消息她没反应？**
1. 确认 `config.toml` 存在（没有时会静默读示例配置，QQ 号对不上就不回）；
2. 确认 `allowed_user_id` 是你发消息用的那个号；
3. 确认 NapCat 里 WebSocket 服务端已启用，端口和 Access Token 与 `config.toml` 逐字一致；
4. 看日志 `data/logs/bot.log` 有没有"WebSocket 连接成功"和"处理新一轮输入"这两类记录。

**第一条回复要等好几分钟？**
这是设计：她忙的时候（按作息表），第一条回复会延迟 1~10 分钟，像真人看到消息才回；聊开之后就正常接话了。不想要可以在 `[timing]` 里关掉 `timing_enabled`。

**她不发语音？**
依次检查：`[tts].enabled` 是否打开；今天是否已达到 `daily_limit`；她此刻是否在睡觉或忙（作息表命中时不方便说话，你点名要语音时会放行）。

**想换模型？**
在 `[models.<名字>]` 里填好那家的 `api_key` 和模型名，然后把 `[llm].current` 改成那个名字即可。看图需要模型支持多模态，不支持的话把 `vision` 留空，就不识图。

**会不会很费钱？**
后台任务和主聊默认都用便宜的 flash 档模型，系统提示词按缓存友好的顺序装配；每一笔调用的花费都能在看板 `/costs` 页看到。多候选（`candidates`）会让主聊费用按条数翻倍，按需开启。

**数据存在哪？**
全部在本地 SQLite 文件 `data/companion.db`，不上传任何第三方（除了调用你配置的模型 API）。备份在 `data/backup/daily/`。

---

## 11. 许可证与致谢

本项目以 [MIT License](LICENSE) 开源。

感谢以下项目：
- [NapCat](https://github.com/NapNeko/NapCatQQ)：OneBot v11 协议端；
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) 与 SenseVoice：离线语音识别；
- [edge-tts](https://github.com/rany2/edge-tts)：免费语音合成；
- [霞鹜文楷 LXGW WenKai](https://github.com/lxgw/LxgwWenKai)：看板手写字体（SIL OFL 1.1，许可文件见 `companion/assets/`）。
