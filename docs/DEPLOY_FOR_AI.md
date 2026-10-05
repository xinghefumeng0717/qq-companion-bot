# 给 AI 助手的零基础部署说明书（DEPLOY_FOR_AI）

> **这份文档写给 AI 助手。** 如果你是人类用户：把它丢给你的 AI 助手（Claude Code / Kimi Code / Cursor / 网页版大模型都行）即可，不用自己读。如果你是 AI 助手：**从现在起你接管这次部署**，负责把用户从"什么都没准备"带到"在手机 QQ 上收到她的回复"，可选再开通语音。
>
> 用户是完全的技术小白——不知道"语音 API"是什么、没打开过终端、不知道什么是 SSH。他不需要懂，但他必须自己按键盘：**你只能给指令和判读结果，不能替他操作，所以要写得让他闭着眼也能执行。**
>
> **有两条路线，动手前先让用户选（见第 0 节的「两条路线」）**：
> - **路线 A：云服务器**（第 3~10 节）——租一台 Ubuntu 服务器，她 24 小时在线，电脑关机也照样回话；
> - **路线 B：用户自己的 Windows 电脑**（第 11 节）——不花服务器的钱，先零成本跑起来；代价是电脑关机/睡眠她就下线。
> 两条路线只有"跑在哪儿"不同，角色卡、配置字段、试跑判据完全一样；第 11 节只写**与路线 A 的差异**，相同的部分直接指回前面的小节。

---

## 0. 开工前先守住这七条守则（比任何命令都重要）

1. **一次只给一件事**：一条命令（或语义上不可分的一小组），等用户回报结果，再给下一步。绝不要一次甩一大段命令让他全跑完。
2. **每条命令都用大白话解释"这条在干什么"**：例如"这条是把服务器时间调成北京时间，不调的话她的作息会整体晚 8 小时"。用户看得懂的是意图，不是命令本身。
3. **报错一律让用户"原样贴回"**：让他把**命令和完整输出**（截图也可以）贴给你，你诊断后再给下一步。明确告诉他：不要自己猜、不要自己改、不要重复执行有破坏性的步骤。
4. **说清楚"在哪儿执行"**：本说明书每条命令都标了【本地电脑执行】或【服务器终端执行】。这两类绝不能混：本地的命令拿到服务器上跑不通，反之亦然。给用户时也要再说一遍"这一步在你自己电脑上敲"。**走路线 B 时没有服务器**：所有【服务器终端执行】的步骤都换到用户自己的 Windows 电脑上（本机 PowerShell），对应写法见第 11 节。
5. **本说明书里所有值都是示例，必须换成用户的真实值**：服务器 IP 一律写 `203.0.113.10`、大号 `123456789`、小号 `987654321`、密钥 `sk-xxxxxxxx`。每次给命令前，先明确告诉他"把 203.0.113.10 换成你自己的服务器 IP"，并且**把替换后的完整命令写出来**再让他复制。
   - 仓库根目录的 `AGENTS.md` 里写着"禁止占位符、必须填真实值、项目根目录固定为 `D:\qq-companion`、部署路径固定为 `/opt/qq-companion`"。那是**原作者本人**的私有协作规范，只约束他本地的执行代理，**不适用于你和你的用户**。你的用户是新部署者：服务器 IP、QQ 号、API key 全都要现收集。不要去翻仓库里任何"看起来像真实值"的东西来填，也不要因为那条规矩就把示例值当成真值。
6. **有破坏性的操作先讲后果再动手**：重置数据、`--purge-all`、覆盖文件、改安全组、改 systemd 配置，都要先说"这一步会发生什么、能不能撤销"，得到用户确认再执行。
7. **机密不外传**：用户的 `config.toml` 里存着 QQ 号、OneBot 密钥、API Key。不要建议他提交到 git、贴到公开论坛或 issue；连日志里都可能带 QQ 号，贴出去之前提醒他先把数字打码。

### 0.1 两条路线：先问用户选哪条，再动手

在第 0 步收东西之前，先问清楚他要哪条（原话与判断标准见第 2 节的两个问题）。**不要替用户默认走服务器路线**——"先在自己电脑上零成本跑起来"是完全正当的选择，代价只要讲清楚就行。

| | 路线 A：云服务器 | 路线 B：用户自己的 Windows 电脑 |
|---|---|---|
| 章节 | 第 3~10 节 | 第 11 节（只写差异） |
| 花钱 | 服务器（几十元/月）+ LLM key | 只有 LLM key |
| 她在线时长 | 24 小时，用户电脑关机也在线 | 只有电脑开着且没睡的时候在线 |
| 需要什么前置 | 服务器 + QQ 小号 + LLM key | QQ 小号 + LLM key |
| 看板怎么看 | SSH 隧道 + 浏览器 | 浏览器直接开 `http://localhost:8080` |
| 适合谁 | 想要"永远在线"的伴侣 | 想先零成本试、或不想租机器 |

两条路线**共用**的东西：QQ 小号、LLM key、NapCat 的 WebUI 操作（端口 6099/3001、扫码、新建 WebSocket 服务端）、`config.toml` 的字段与判断标准、试跑成功判据、可选增强。所以路线 B 里"照路线 A 的 X.X 做"就是真的一样做，只是命令换成 Windows 写法（第 11.1 节给了对照表）。

---

## 1. 进度总表（照这个顺序走，别跳步）

| 阶段 | 谁动手 | 干什么 | 成功判据（达不到就别往下走） |
|---|---|---|---|
| 第 0 步 | 用户 | 先选路线，再备齐东西：QQ 小号 + LLM API key（走路线 A 再加一台服务器） | 收齐（收齐前不许敲命令） |
| 1 | 用户（本地） | 从自己电脑 SSH 连上服务器 | 命令行提示符变成服务器上的 `ubuntu@...` |
| 2 | 用户（服务器） | 时区、Python 3.11、ffmpeg、Docker | `timedatectl` 显示 `Asia/Shanghai`，三条版本命令都有输出 |
| 3 | 用户（服务器） | 拉代码 + 建 venv + 装依赖 | `依赖 OK`，`/opt/qq-companion` 下能看到 `companion/` |
| 4 | 用户（服务器+本地浏览器） | 起 NapCat、扫码登录小号、配 WebSocket 服务端 | WebUI 里显示已登录 + WS 服务端已启用监听 |
| 5 | 用户（服务器） | 填 `config.toml` | 校验脚本打印的 QQ 号/token/角色卡都对得上 |
| 6 | 用户（服务器+手机） | 前台试跑，收到第一条回复 | 大号给小号发消息，她回了 —— **里程碑达成** |
| 7 | 用户（服务器） | systemd 守护 + 开机自启 | `systemctl status` 显示 `active (running)` |
| 8 | 用户（本地浏览器） | 开隧道看看板 | `http://localhost:8080` 能打开总览页 |
| 可选 A/B/C | 用户 | 语音输入 / 语音回复 / 换自己的角色 | 各自小节里有判据（**不是必须的**） |

> **走路线 B（用户自己的 Windows 电脑）时**：**跳过阶段 1、2、7**（那三步是"SSH 登录 / 服务器环境 / systemd 守护"），阶段 3~6 与阶段 8 照走，但命令全部换成 Windows 写法——差异清单与替代方案见**第 11 节**，不要照抄上面的 Linux 命令。

预计总耗时 40~70 分钟（路线 B 一般更快）；用户中途任何一步卡住都正常，卡住就停下来贴输出。

---

## 2. 第 0 步：先问两个问题，再收齐东西（硬性前置）

**先问清楚下面两件事，答案决定"要不要买东西、要不要多装组件、走哪条路线"。** 问完把两个答案记下来，后面每一步都按它走；收齐该收的东西之前，不要敲任何命令。

### 2.0 必须问的两个问题（原话照问）

**问题 ①：要不要语音功能？**

> "你想让她能听懂你发的语音、也能用语音回你吗？（要的话部署时多装一样组件、多下一个模型文件；不要的话这些整段跳过，一条命令都不用多敲）"

- 回答**要** → 路线 A 在阶段 2 照常装 ffmpeg（已在命令里）；路线 B 按 **11.9** 装 ffmpeg。两条路线都等文字聊天跑通后，再回来做「可选增强 A / B」。
- 回答**不要** → **「可选增强」整段跳过**（第 12 节一句都不用看），ffmpeg 也不必装。她收到语音只会回一句"没能听清"的占位文字，不会报错、不会崩——以后想要，随时可以补。

**问题 ②：想让她 24 小时在线，还是先在自己电脑上零成本跑起来？**

> "你希望她在你电脑关机、合盖之后也一直在线吗？还是先不花钱，先跑在你自己电脑上试试？"

- 选**要 24 小时在线** → **路线 A：云服务器**（2.1 开始准备；每月几十元 + LLM 花费）；
- 选**先零成本跑起来** → **路线 B：跑在用户自己的 Windows 电脑上**（只有 LLM key 的花费）。**先把代价讲清楚**：电脑关机或睡眠她就下线（主动消息也停），想长时间在线得改电源设置；Windows 半夜自动重启也会打断她。他接受再开工，全部差异见**第 11 节**。

> 两条路线**共用**：QQ 小号（2.2）、LLM API key（2.3），以及第 4~8 节的所有知识。

### 2.1 一台 Ubuntu 云服务器（**仅路线 A 需要**，路线 B 整节跳过）

- **已有服务器**：问清三件事——公网 IP、登录用户名、登录密码（或密钥文件）。必须是 Ubuntu 22.04（其他 Linux 也能跑，但本说明书只按 Ubuntu 22.04 写）。
- **没有就指导他买**（腾讯云 / 阿里云都可以，哪个顺眼买哪个）：
  1. 注册账号并完成**实名认证**（国内云服务强制要求，没有它买不了）；
  2. 产品选「**轻量应用服务器**」（最便宜的档位就够，别买错成"云服务器 CVM/ECS"的高配）；
  3. 地域挑离用户近的（国内节点即可）；
  4. 镜像选 **Ubuntu 22.04**（不要选 CentOS、不要选"宝塔面板"、"WordPress"这类预装镜像）；
  5. 套餐选最低档（2 核 2G / 系统盘 40G 以上）；磁盘建议 ≥ 20G；
  6. 创建时记下**公网 IP**、**登录用户名**（腾讯云 Ubuntu 镜像一般是 `ubuntu`，阿里云轻量 Ubuntu 一般是 `root`）和**登录密码**（设一个自己记得住的，或下载密钥文件存好）；
  7. **防火墙 / 安全组只放行 22 端口（SSH）**。不要放行其他任何端口。本项目的 NapCat 管理页（6099）、机器人接口（3001）、状态看板（8080）全部只绑在服务器本机，靠 SSH 隧道访问，公网摸不到。
- 让他把 **IP、用户名、密码（或密钥路径）** 发给你（他要是不放心，可以让你只用到时再给，但你需要知道有没有）。

### 2.2 一个 QQ 小号（**两条路线都要**，机器人要用它登录）

- 要求：**能在手机 QQ 上正常扫码登录**的号，最好是注册了一段时间、有正常使用记录的（新注册的号直接挂协议端极易触发风控）；
- 重要的现实提醒，**必须原话告诉他**：这个号是通过第三方协议端（NapCat）登录的，**有被腾讯风控、被限制登录甚至被封的结构性风险**。请用小号，不要用主号。他不接受这个风险，就到此为止，别往下走；
- 让他顺便**用小号加他的大号为好友**（机器人只回他一个人，但好友关系能减少私聊被拦的几率）；
- 记下**大号 QQ 号**和**小号 QQ 号**两个数字，后面 `config.toml` 要用。

### 2.3 一个 LLM API key（**两条路线都要**，她的"脑子"）

- 推荐 **DeepSeek 官方开放平台**：浏览器打开 `https://platform.deepseek.com` → 注册 → 充值（**几块钱就能跑很久**，先充最低档）→ 左侧「API keys」→ 创建 → 把 `sk-` 开头的那串复制给你。
  - key 只在创建时完整显示一次，让他立刻存到记事本；别发到公开群里。
- 说明清楚：本项目需要**三个模型槽位**——`chat`（跟她聊天）、`vision`（看她发的图片）、`tasks`（后台写日记、观察者）。默认配置里这三个都指向 DeepSeek，所以**一个 DeepSeek key 就够跑通全部文字功能**，不需要开三家的账号。
- 可选：如果用户以后要用 MiniMax 音色的语音回复，才需要再注册 MiniMax 开放平台（见「可选增强 B」，第 12 节），现在不需要。

### 2.4 前置清单（按路线对一遍，缺一样都别开工）

| 路线 | 必须到手 |
|---|---|
| A（云服务器） | 公网 IP + 登录用户名 + 密码/密钥（2.1）、大号与小号 QQ 号（2.2）、LLM key（2.3） |
| B（本机 Windows） | 大号与小号 QQ 号（2.2）、LLM key（2.3）；外加"电脑能长时间开着、不睡眠"这个承诺 |

---

## 3. 阶段 1：让用户从自己电脑连上服务器（SSH）

> **路线 B 跳过本阶段**（没有服务器可连）：选路线 B 的直接去第 11 节，从 11.2 开始。

**让用户做什么**

先确认他的电脑是什么系统，然后给对应的说法：

- **Windows 10/11**：让他打开 **PowerShell**（开始菜单搜 "PowerShell" 就行），在窗口里输入下面这条；
- **macOS / Linux**：让他打开「终端 / Terminal」，输入同样这条。

```bash
# 【本地电脑执行】把 203.0.113.10 换成用户的服务器公网 IP
ssh ubuntu@203.0.113.10
```

- 如果他的服务器登录用户是 `root`（阿里云轻量常见），命令改成 `ssh root@203.0.113.10`；如果他用的是密钥文件而不是密码，命令改成 `ssh -i C:\Users\他的用户名\.ssh\密钥文件.pem ubuntu@203.0.113.10`。
- 第一次连接会问 `Are you sure you want to continue connecting (yes/no)?` —— 让他输入 `yes` 再回车（这是正常的指纹确认，不是错误）。
- 然后会要他输密码。**提醒他：输密码时屏幕上不会有任何显示**（不显示星号也不动光标），这是 Linux 的正常行为，不是卡住了；输完直接回车。

**预期看到什么**

提示符从本机的样子变成服务器的样子，类似：

```
ubuntu@VM-0-1-ubuntu:~$
```

看到这个 —— 人已经在服务器里了。以后凡是他自己敲的命令，"服务器终端执行"那些就在这个窗口里敲。

**失败了往哪查**

| 现象 | 让用户检查 | 你给他的下一步 |
|---|---|---|
| `Permission denied` / `密码错误` | 密码是否复制错了（区分大小写）；用密钥的话路径对不对 | 让他重新输一次；还不行，回云控制台**重置服务器密码**后重试 |
| `Connection timed out` / 卡住不动 | 安全组/防火墙有没有放行 22 端口；IP 是否抄错 | 回云控制台，安全组入站规则加一条：22 端口，来源 `0.0.0.0/0`（或他自己的出口 IP） |
| `No such file or directory` + `.pem` | 密钥文件路径写错 | 让他把密钥文件拖到某个易找的位置（如 `C:\Users\他的用户名\Downloads\`）再引用完整路径 |
| `Connection refused` | 服务器是不是还没开好/正在重装系统 | 登录云控制台看实例状态，等"运行中"再试 |
| ssh 命令本身不存在（Windows 很老的版本） | 系统太旧 | 让他装 Windows 自带的"OpenSSH 客户端"（设置 → 应用 → 可选功能） |

---

## 4. 阶段 2：服务器环境（时区 / Python 3.11 / ffmpeg / Docker）

> **路线 B 跳过本阶段的大部分**：本机不装 Docker、不用设时区；Python 与 git 按 **11.2** 用官方安装包装，ffmpeg 只在用户要语音时按 **11.9** 装。

以下全部**在服务器终端执行**（就是阶段 1 那个窗口）。一组一组给，别一次全贴。

### 4.1 设时区（第一条就做，不做会出怪事）

```bash
# 【服务器终端执行】
sudo timedatectl set-timezone Asia/Shanghai
timedatectl
```

解释给用户听：云服务器默认时区常常是 UTC（比北京时间晚 8 小时）。不改的话，她的作息表（早八、熄灯）、"免打扰时段"、长假判定会**整体偏 8 小时**——北京时间凌晨她当傍晚，会挑不该说话的时候发消息。

**预期看到什么**：输出里有一行 `Time zone: Asia/Shanghai (CST, +0800)`。也让他顺手跑 `date`，显示的应该是真实的北京时间。

### 4.2 装 Python 3.11、ffmpeg、Docker

```bash
# 【服务器终端执行】
sudo apt update

sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update

sudo apt install -y python3.11 python3.11-venv ffmpeg docker.io git
```

解释：Ubuntu 22.04 自带的 Python 是 3.10，这个项目要 3.11+，所以从 deadsnakes 源补装一个 3.11（不影响系统自带的 3.10）。`ffmpeg` 是"语音输入"要用的组件，现在不做语音也建议一起装（一条命令的事，以后要用不用回头补）。`docker.io` 用来跑 NapCat。`git` 用来拉代码。

> 如果用户的系统不是 22.04：先让他跑 `python3 --version`；若已经是 3.11 或更高（例如 Ubuntu 24.04 是 3.12），**跳过 `add-apt-repository` 那两行**，只装 `python3-venv ffmpeg docker.io git`，后面命令里的 `python3.11` 一律换成 `python3`。

**预期看到什么**：三条版本命令都有正常输出。

```bash
# 【服务器终端执行】
python3.11 --version
ffmpeg -version | head -1
docker --version
```

分别应打印 `Python 3.11.x`、`ffmpeg version ...`、`Docker version ...`。

### 4.3 让当前用户可以免 sudo 用 docker

```bash
# 【服务器终端执行】
sudo usermod -aG docker $USER
```

然后**让他输入 `exit` 退出 SSH，再用阶段 1 的命令重新连一次**。解释：加进 docker 组这件事只在**新的登录会话**里生效，不重连的话下一步会报 `permission denied while trying to connect to the Docker daemon`。

**预期看到什么**（重连之后）：

```bash
# 【服务器终端执行】
docker ps
```

应输出一行表头（`CONTAINER ID IMAGE ...`）且**不报错**。报 `permission denied` → 说明还没重连，或让他执行 `newgrp docker` 再试。

---

## 5. 阶段 3：把代码放上服务器 + 装 Python 依赖

### 5.1 拉代码到 `/opt/qq-companion`

```bash
# 【服务器终端执行】
cd ~
git clone https://github.com/xinghefumeng0717/qq-companion-bot.git
sudo mv qq-companion-bot /opt/qq-companion
sudo chown -R ubuntu:ubuntu /opt/qq-companion
```

> 如果用户是从别的地址/fork 拿到这份说明书的，把上面 clone 的网址换成他拿到的那个仓库地址；不确定就让他在仓库页面点绿色的 `Code` 按钮、复制 `HTTPS` 那一行发给你。

解释：先克隆到自己家目录（这样 `.git` 目录属于他，以后能 `git pull` 升级），再用 `sudo` 挪到正式部署路径 `/opt/qq-companion`，最后把整个目录的属主改成他（不然机器人写不了数据库和日志）。`chown` 里的 `ubuntu` 只有在他登录用户就叫 `ubuntu` 时才对——如果他是 `root` 登录，把 `ubuntu:ubuntu` 换成 `root:root`。

**预期看到什么**：`ls /opt/qq-companion` 能看到 `companion`、`characters`、`deploy`、`docs`、`requirements.txt` 等。注意此时**没有** `config.toml`、也**没有** `data/` —— 这两个都被 `.gitignore` 排除，这是正常的，后面会创建。

### 5.2 建虚拟环境 + 装依赖

```bash
# 【服务器终端执行】
cd /opt/qq-companion
python3.11 -m venv venv
./venv/bin/pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

解释：`venv` 是在项目里隔离出一个专属的 Python 环境，不动系统 Python；`-i https://mirrors.aliyun.com/pypi/simple/` 是走阿里云的软件源，比默认源快很多（**别去掉**）。这一步里 `sherpa-onnx` 包很大（含语音识别运行时），下载慢是正常的，让他耐心等。

**预期看到什么**：

```bash
# 【服务器终端执行】
./venv/bin/python -c "import aiohttp, aiosqlite, PIL, tomllib; print('依赖 OK')"
```

应打印 `依赖 OK`。

**失败了往哪查**

- `pip` 卡住 / 超时 / `Read timed out` → 让他**原样重跑一次**这条 pip 命令（镜像偶尔抽风，重跑通常就好）；连续两次失败，把完整输出贴给你。
- `No module named venv` → 说明 `python3.11-venv` 没装上，回 4.2 补装。
- `python3.11: command not found` → 回 4.2，或按那条注解改用 `python3`。

---

## 6. 阶段 4：NapCat（协议端）——小白最容易卡住的一步

NapCat 是"替小号挂在 QQ 上的那层协议"，跑在 Docker 容器里；机器人通过本机的 3001 端口跟它说话。这一段要慢慢来，分成 4 个小步。

### 6.1 生成一个 OneBot 密钥（先备着）

```bash
# 【服务器终端执行】生成一串 32 位随机密钥
openssl rand -hex 16
```

解释：这是机器人和 NapCat 之间的"暗号"，两边必须填一模一样的。**把输出复制到记事本存好**（下一步 NapCat 和阶段 5 的 `config.toml` 都要用它）。如果这台服务器提示 `openssl: command not found`，改用这条等效命令：`python3 -c "import secrets; print(secrets.token_hex(16))"`。

### 6.2 启动 NapCat 容器

```bash
# 【服务器终端执行】
cd /opt/qq-companion
docker run -d --name napcat --restart always \
  -e NAPCAT_GID=$(id -g) -e NAPCAT_UID=$(id -u) \
  -v ./napcat-data:/app/.config/QQ \
  -v ./napcat-cache:/app/napcat/cache \
  -p 127.0.0.1:3001:3001 -p 127.0.0.1:6099:6099 \
  mlikiowa/napcat-docker:latest
```

解释给用户：`-d` 是后台运行，`--restart always` 是服务器重启/容器崩溃后自动拉起来，`-v` 是把 QQ 的登录状态存在服务器磁盘上（这样重启不用反复扫码），`-p 127.0.0.1:...` 是关键——两个端口**只对本机开放**，公网访问不到，所以安全组不用放行它们。

**预期看到什么**：

```bash
# 【服务器终端执行】
docker ps
```

应看到一行 `mlikiowa/napcat-docker:latest`、状态是 `Up ...`，名字是 `napcat`。（第一次运行要先下载镜像，可能等几分钟，`docker ps` 一开始看不到是正常的。）

**失败了往哪查**：`docker: command not found` → 回 4.2；`name "napcat" is already in use` → 容器已存在，改用 `docker start napcat`。

### 6.3 开 SSH 隧道（把服务器的管理页"搬"到用户自己的浏览器）

```bash
# 【本地电脑执行】把 203.0.113.10 换成用户的服务器 IP；用户名按实际情况（ubuntu 或 root）
ssh -L 6099:127.0.0.1:6099 -L 8080:127.0.0.1:8080 ubuntu@203.0.113.10
```

解释：这条命令前半段和普通登录一样，`-L 6099:127.0.0.1:6099` 的意思是"把我本机（用户自己的电脑）的 6099 端口，转发到服务器的 6099 端口"。因为服务器的 6099 只对本机开放，只有靠这条隧道才能从外面看到它。**这个窗口必须一直开着**——关掉窗口隧道就断，浏览器立刻打不开页面；想继续看就重新连。

**预期看到什么**：和阶段 1 一样登录成功、出现服务器提示符。这个窗口之后一直别动。

### 6.4 浏览器里：扫码登录小号 + 新建 WebSocket 服务端

1. 让用户在**自己的浏览器**打开：`http://127.0.0.1:6099/webui`
2. 按页面指引**用手机 QQ 小号扫码登录**（手机和小号保持在线，登录更稳）。
   - 如果页面要求输入 WebUI 的登录密码/令牌：让他先在**另一个服务器终端窗口**执行 `docker logs --tail 50 napcat`，把输出整段贴给你，你据此判断该填什么。**不要让他自己猜**。
   - 如果提示风控/需要验证码：按页面指引处理；常见解法是让小号在手机上正常用几天再回来扫。
3. 登录成功后，进左侧「**网络配置**」（有的版本叫"网络设置"）→ 点「**新建**」→ 类型选「**WebSocket 服务端**」，然后：
   - **端口**：`3001`
   - **启用**：打开
   - **Token / Access Token**：填 6.1 生成的那串密钥（**和后面 `config.toml` 的 `access_token` 必须逐字符相同**）
   - 勾选/开启 **消息上报**（上报类型含消息与事件）
   - 保存，并确认这一条是"已启用"状态。
4. 让他在 NapCat 的日志/页面里确认 WebSocket 服务已经"在监听"（不同版本措辞不一样，可能是 `listening`、`服务已启动` 之类；**把实际输出贴给你判断，不要照着某一种措辞硬找**）。

> 两个容易混的"token"：NapCat **WebUI 自己的登录凭据**，和 **OneBot 服务端的 Access Token**，是两码事。前者用来进管理页面，后者用来让机器人连上 3001。你给用户的每一处都要说清是哪一个。

**预期看到什么**：WebUI 里左边显示小号已登录（能看见昵称/头像），网络配置列表里那条 WebSocket 服务端是启用状态。

**失败了往哪查**

- 打开 `http://127.0.0.1:6099/webui` 报"拒绝连接" → 隧道窗口是不是关了？容器起没起（`docker ps`）？
- 扫码后立刻掉线/被踢 → 小号太新，被风控。让他先在手机上正常用几天；同时可以 `docker restart napcat` 后重新扫码。
- WebUI 打不开但容器在跑 → 确认隧道命令里 `6099` **两端都要写**。

---

## 7. 阶段 5：填 `config.toml`（机密文件）

### 7.1 先复制模板

```bash
# 【服务器终端执行】
cd /opt/qq-companion
cp config.example.toml config.toml
chmod 600 config.toml
ls -l config.toml
```

解释：`config.example.toml` 是公开的模板（里面全是假值），`cp` 出一份真的来填。`chmod 600` 是让这个文件只有他自己能读——因为里面会有 QQ 号、密钥和 API key。

### 7.2 用 nano 编辑（把保存退出按键写给他）

```bash
# 【服务器终端执行】
nano config.toml
```

nano 操作说明（**原话给用户**）：

- 用**方向键**移动光标；
- 改完保存：按 `Ctrl` 不放再按字母 `O`（是字母 O，不是零），屏幕下方出现文件名，直接按**回车**；
- 退出：按 `Ctrl` 不放再按字母 `X`；
- 或直接退出：`Ctrl+X`，它会问 `Save modified buffer?`，按 `Y` 再回车。

### 7.3 必须改的 5 处（照这个清单逐项确认）

| 位置 | 填什么 | 不改会怎样 |
|---|---|---|
| `[account] allowed_user_id` | **大号** QQ 号（只响应这个人） | 机器人不认识他，发消息没反应 |
| `[account] bot_qq` | **小号** QQ 号 | 日志与小号自我识别会错 |
| `[onebot] access_token` | 6.1 生成、并填在 NapCat 里的那串密钥，**逐字符相同** | 连不上 NapCat（日志会反复 `Connection refused 3001` 或握手失败） |
| `[models.deepseek] api_key` | 用户申请的 DeepSeek key（`sk-` 开头） | 每次调用都 401，全程降级 |
| `[voice] enabled` | 第一次跑通**改成 `false`** | 保持 `true` 时，用户发语音会收到"没能听清"的占位文字（不崩，但看起来像坏了） |

另外：`[character] path` **先别动**，保持默认的 `characters/example`（仓库自带的示例角色）。先把文字聊天跑通，想换自己的角色时再按「可选增强 C」做。

### 7.4 别动的部分（有默认值兜底，改了反而容易出问题）

- `[llm] current`（保持 `"deepseek"`）、`thinking_*` 系列（保持原样）；
- `[models.minimax]`、`[models.seed]` 段：这次用不到，`api_key` 留假值也**不会**影响运行（只有选了对应 provider 才会用）；
- `[reply]`、`[proactive]`、`[timing]`、`[tts]`、`[admin]`、`[llm.pricing]`：全部保持默认。特别是 `[tts] enabled` 保持 `false`（语音回复默认就是关的），`[admin] host` 保持 `127.0.0.1`。
- 不要新增、不要删除任何段落，只改上面那 5 处的值。

### 7.5 校验（用程序自己读一遍，别靠肉眼看）

```bash
# 【服务器终端执行】只读校验，不改任何文件
cd /opt/qq-companion
./venv/bin/python - <<'PY'
from companion.config import Config
c = Config.load("config.toml")
p = c.llm.active()
print("大号 allowed_user_id =", c.account.allowed_user_id)
print("小号 bot_qq          =", c.account.bot_qq)
print("OneBot ws_url        =", c.onebot.ws_url)
print("OneBot token 长度    =", len(c.onebot.access_token))
print("模型预设 / base_url  =", c.llm.current, "/", p.base_url)
print("api_key 前缀/长度    =", p.api_key[:3], "/", len(p.api_key))
print("角色卡目录            =", c.character.path)
print("语音输入 enabled      =", c.voice.enabled)
print("语音回复 tts.enabled  =", c.tts.enabled, "| provider =", c.tts.provider)
print("看板 host:port        =", f"{c.admin.host}:{c.admin.port}", "| token 长度 =", len(c.admin.token))
PY
```

**预期看到什么**：`大号`/`小号` 是用户的真实 QQ 号；`ws_url` 是 `ws://127.0.0.1:3001`；`token 长度` 是 32；`api_key 前缀` 是 `sk-`；`角色卡目录` 是 `characters/example`；`语音输入 enabled = False`；`tts.enabled = False`。

**一个必须知道的坑**：如果用户不小心在错误的目录下启动程序，程序找不到 `config.toml` 会**静默退回读 `config.example.toml`**（于是大号变成 `123456789`、小号变成 `987654321`，看起来"什么都没报错就是没反应"）。所以每次启动前都要确认在 `/opt/qq-companion` 目录下；上面这个校验脚本如果打印出 `123456789`，就是读到了模板，让他检查文件名和目录。

**红线**：`config.toml` **永远不进 git、永远不贴到公开地方**。`.gitignore` 已经拦住了它，也不要手动 `git add -f`。

---

## 8. 阶段 6：前台试跑，收到第一条回复（里程碑）

### 8.1 手动跑一次，看日志

```bash
# 【服务器终端执行】
cd /opt/qq-companion
./venv/bin/python -m companion.main
```

解释：这是"手动、前台"启动——日志会直接打在屏幕上，出问题能立刻看见。`Ctrl+C` 可以停掉。

**预期看到什么**：启动日志里有关键两行：

```
[OneBot] 正在连接 OneBot 服务: ws://127.0.0.1:3001
[OneBot] WebSocket 连接成功！
```

### 8.2 让用户用大号发一条消息

让他用手机 QQ 的**大号**，给小号发一句"在吗"。

**预期看到什么 —— 里程碑**：小号（她）回复了，而且是像真人一样分成几条短消息发的。

> **先打预防针**：第一条回复**可能要等 5 秒到 10 分钟**。项目默认开了"回复时机人格化"——她在忙（上课/睡觉）时会等 1~10 分钟才回，闲着时 5~30 秒；一旦聊起来就快了。**这是设计，不是坏了**，让他别在 3 分钟没回时就判定失败。屏幕上应该能看到"对方正在输入"的提示。

### 8.3 试跑通过后

让他按 `Ctrl+C` 停掉（这是刻意的：确认能手动跑通，再交给后台守护）。停之前可以顺手再试两件事：发一张图片（她应该能描述内容）、发一条语音（阶段 5 里把 `[voice] enabled` 设成 `false` 了，所以她听不懂，会回一句听不清的占位文字）。

### 8.4 常见报错对照

| 屏幕上的现象 | 原因 | 你给的下一步 |
|---|---|---|
| 反复 `Connection refused` / 连不上 3001 | NapCat 容器没起来，或 WS 服务端没配/端口不对，或 token 不一致 | `docker ps` 看容器；`docker restart napcat` 等 10 秒；回去核对 6.4 的端口与 Token 是否与 `config.toml` 逐字符一致 |
| `401` / `Unauthorized` / 日志里 401 | LLM 的 api_key 填错，或账户欠费 | 回 7.3 核对 `[models.deepseek] api_key`；去 DeepSeek 平台确认余额 |
| 日志有 `[OneBot] WebSocket 连接成功`，但发消息完全没反应 | 大号/小号不是好友，或 `allowed_user_id` 填成了小号/填错 | 核对 `allowed_user_id` 是**大号**；让小号加一下大号好友 |
| 她回复"刚刚走神了……"之类 | 调用 DeepSeek 失败（key/余额/网络） | 服务器上执行 `curl https://api.deepseek.com` 看通不通；核对 key 与余额 |
| 启动了但报 `配置文件未找到` 或行为像模板值 | 不在 `/opt/qq-companion` 目录下，读到了 `config.example.toml` | `cd /opt/qq-companion` 再启动；用 7.5 的脚本确认 |
| `ModuleNotFoundError` | 依赖没装全 | 回 5.2 重跑 pip |

---

## 9. 阶段 7：交给 systemd 守护（开机自启、崩溃自动拉起）

> **路线 B 换做法**：Windows 上没有 systemd，用**任务计划程序**实现"开机自启"，并配合**电源设置**防止她睡着——见 **11.7 / 11.8**。

### 9.1 先确保日志目录存在

```bash
# 【服务器终端执行】
mkdir -p /opt/qq-companion/data/logs
```

解释：程序来自公开仓库时**没有 `data/` 目录**（它被 git 忽略），而 systemd 要把日志写到 `data/logs/bot.log`，目录不存在服务会起不来。这条命令创建它。

### 9.2 安装并启用服务

```bash
# 【服务器终端执行】
sudo cp /opt/qq-companion/deploy/qq-companion.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qq-companion
sudo systemctl status qq-companion --no-pager | head -12
```

解释：`cp` 是把项目自带的守护配置装到系统里；`daemon-reload` 让 systemd 重新读一遍配置；`enable --now` 一次性做到"开机自启 + 立刻启动"。

**如果用户不是 `ubuntu` 用户登录**（比如 root 登录的阿里云服务器）：那个服务文件里写死了 `User=ubuntu`，服务器上没有这个用户时服务会起不来。先让他跑 `id ubuntu` 确认，若提示 `no such user`，执行：

```bash
# 【服务器终端执行】只在这台机器上没有 ubuntu 用户时才需要
sudo sed -i "s/^User=ubuntu$/User=$(id -un)/; s/^Group=ubuntu$/Group=$(id -gn)/" /opt/qq-companion/deploy/qq-companion.service
sudo cp /opt/qq-companion/deploy/qq-companion.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl restart qq-companion
grep -E "^(User|Group)=" /etc/systemd/system/qq-companion.service
```

### 9.3 成功判据与日常命令

**预期看到什么**：`status` 里出现 `active (running)`。

```bash
# 【服务器终端执行】日常运维三件套
sudo systemctl status qq-companion --no-pager | head -6    # 看状态
tail -f /opt/qq-companion/data/logs/bot.log                # 实时看日志（Ctrl+C 退出）
sudo systemctl restart qq-companion                        # 重启（改完配置用它生效）
```

再让用户用大号发一条消息，确认后台模式下也能收到回复。

**失败了往哪查**：`systemctl status` 显示 `activating (auto-restart)` 循环 → 执行 `journalctl -u qq-companion -n 50 --no-pager`，让用户把输出整段贴给你。

---

## 10. 阶段 8：状态看板（SSH 隧道里看）

```bash
# 【本地电脑执行】窗口保持开着；把 203.0.113.10 换成用户的 IP
ssh -L 8080:127.0.0.1:8080 ubuntu@203.0.113.10
```

然后在自己浏览器打开 `http://localhost:8080`。

| 页面 | 看什么 |
|---|---|
| `/` 总览 | 好感度六维雷达、关系阶段、情绪、认识天数、主动消息未回复数 |
| `/memory` 记忆 | 情景日记（含遗忘衰减）、语义记忆、待跟进事项 |
| `/debug` 调试 | 最近一次发给模型的完整提示词、观察者原始 JSON |
| `/costs` 计费 | 调用次数与 Token 费用、按用途分类、近 24 小时开销 |
| `/stickers` 表情包 | 表情包图库与描述 |
| `/logs` 日志 | `data/logs/bot.log` 最近 200 行 |
| `/admin` 管理 | 立即备份 / 重启服务 / 重置数据（重置要手敲大写 `YES`） |

解释给用户：这个看板默认只监听服务器本机、页面本身没有密码，靠"不对外开放 + SSH 隧道"保护。所以**不要**把它暴露到公网。

---

## 11. 路线 B：部署在用户自己的 Windows 电脑上

> **什么时候走这条**：用户不想租服务器，想零成本先跑起来（唯一的钱是 LLM API key）。**主程序在 Windows 上零代码改动就能跑**（信号处理已做 Windows 兼容、路径已有 `os.name == "nt"` 特判、依赖全部有 Windows 版），所以这条路线不是"凑合"，是正经可用。
>
> **必须一次讲清的代价**（含糊过去，用户第二天就会来投诉）：
> - **电脑关机、合盖睡眠 = 她下线**，连主动消息也一起停。默认电源设置下电脑闲置十几分钟就会睡，所以 11.8 那一步不是可选项；
> - Windows 自动更新可能半夜重启、拔网线/断网期间她也不在线；
> - 想要真正的 24 小时在线，终究要回到路线 A。但**先跑起来不浪费**：数据可以搬——本地 `data/backup/daily/latest.db` 恢复成服务器上的 `data/companion.db` 就能接着养（备份机制见 README 第 11 节，让接手的 AI 去读，别在这里展开）。
>
> **贯穿全线的两个坑**：① 所有路径**不能带中文、不能带空格**（项目目录和 NapCat 解压目录都一样，推荐 `D:\qq-companion` 和 `D:\napcat`）；② 本路线所有命令都在**本机 PowerShell** 里执行（不是服务器），凡是路线 A 里写【服务器终端执行】的，这里都换成本机窗口。

### 11.1 与路线 A 的差异总览

| 路线 A 的步骤 | 路线 B 怎么做 |
|---|---|
| 阶段 1：SSH 连服务器 | **跳过**。全程在本机 PowerShell 里敲 |
| 阶段 2：服务器时区/Python/ffmpeg/Docker | **只装 Python 与 git**（11.2）；**不装 Docker、不用设时区**（本机本来就是北京时间）；ffmpeg 只在"问题 ① 答要语音"时装（11.9） |
| 阶段 3：拉代码 + venv + 依赖 | 命令换成 Windows 写法（11.3） |
| 阶段 4：NapCat（Docker） | 改用 **NapCat 官方 Windows 一键包**（11.4），WebUI 里的配置操作**照 6.4 做，一模一样** |
| 阶段 5：填 `config.toml` | **字段与判断标准完全同 7.3 / 7.4**，只是编辑器和校验命令换成 Windows 写法（11.5） |
| 阶段 6：前台试跑 | 同 8.1~8.4，命令换成 11.6 |
| 阶段 7：systemd 守护 | **换任务计划程序**（11.7）+ 电源设置（11.8） |
| 阶段 8：看板 | 更简单：**不用隧道**，浏览器直接开 `http://localhost:8080`（11.10） |

命令对照速查（后面各节也会重复给）：

| 路线 A（Linux） | 路线 B（Windows PowerShell） |
|---|---|
| `./venv/bin/python -m companion.main` | `.\venv\Scripts\python.exe -m companion.main` |
| `./venv/bin/pip install -r requirements.txt` | `.\venv\Scripts\pip install -r requirements.txt` |
| `./venv/bin/python -m companion.reset` | `.\venv\Scripts\python.exe -m companion.reset` |
| `tail -f data/logs/bot.log` | `Get-Content data\logs\bot.log -Wait -Tail 50` |
| `sudo systemctl restart qq-companion` | 任务管理器结束 `python.exe` / `pythonw.exe`，再重新启动任务（或重跑 11.6 的命令） |

### 11.2 装 Python 3.11+ 与 git（差异：官方安装包，且必须勾 PATH）

- Python：去 `https://www.python.org/downloads/windows/` 下 3.11 或更高版本的 **Windows installer (64-bit)**，安装时**务必勾选 `Add python.exe to PATH`**（不勾后面全报"找不到 python"）；
- git：去 `https://git-scm.com/download/win` 下 Windows 版，一路默认下一步即可；
- 装完**关掉并重开 PowerShell**（PATH 只在新窗口生效），然后验证：

```powershell
# 【本地电脑执行】PowerShell
python --version
git --version
```

**预期看到什么**：`Python 3.11.x`（或更高）与 `git version 2.x`。

**失败了往哪查**：`python` 打开了微软应用商店 / 提示找不到 → 装的时候没勾 `Add python.exe to PATH`，重跑安装包选 Modify 补勾，再重开 PowerShell。

> 顺带提醒：本项目的单元测试平时就是在 Windows 上跑的（README 第 12 节有 Windows 命令），所以这条路线的兼容性不是新赌注。

### 11.3 把代码放到本机（差异：克隆到无中文无空格的路径）

```powershell
# 【本地电脑执行】PowerShell
cd D:\
git clone https://github.com/xinghefumeng0717/qq-companion-bot.git qq-companion
cd D:\qq-companion
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
.\venv\Scripts\python.exe -c "import aiohttp, aiosqlite, PIL, tomllib; print('依赖 OK')"
```

- 等价于路线 A 的 5.1 + 5.2，注意点相同：`-i https://mirrors.aliyun.com/pypi/simple/` 别去掉；`sherpa-onnx` 包大、下载慢是正常的；
- **不要** clone 到 `D:\我的东西\` 这类带中文或空格的目录；
- 这里**刻意不激活 venv**（不跑 `Activate.ps1`）：直接用 `.\venv\Scripts\python.exe` 这样写路径，能绕开 PowerShell 的执行策略限制（`Activate.ps1` 经常被策略拦住，是新手常见卡点）。

**预期看到什么**：`依赖 OK`；`dir D:\qq-companion` 能看到 `companion`、`characters`、`deploy`、`requirements.txt`（此时没有 `config.toml`、没有 `data\`，正常）。

**失败了往哪查**：pip 超时 → 原样重跑一次；`python : 无法将"python"项识别为...` → 回 11.2（PATH 没配好）；提示路径含非法字符 → 换到 `D:\qq-companion`。

### 11.4 NapCat：官方 Windows 一键包（差异：不装 Docker）

1. 打开 `https://github.com/NapNeko/NapCatQQ/releases`，下载形如 **`NapCat.Shell.Windows.<版本>.zip`** 的一键包（**内置 QQ 本体**，所以不用自己装 QQ，也不用 Docker）；官方文档：`https://napneko.github.io`；
2. 解压到 **`D:\napcat`**（**绝不能带中文或空格**，否则起不来；也不要在 `C:\Program Files\` 这种需要管理员权限的目录）；
3. 双击目录里的 **`launcher.bat`** 启动（文件名接近，以压缩包内实际文件为准；如果是 Windows 10，可能叫 **`launcher-win10.bat`**）。会弹出一个黑窗口，**这个窗口不能关**，关了她就掉线；
4. 浏览器打开 `http://127.0.0.1:6099/webui` —— **端口、扫码登录、新建 WebSocket 服务端（端口 3001、填 Access Token、开消息上报）全部照路线 A 的 6.4 做**，一个字段都不差；
5. OneBot 的 Access Token 在 Windows 上这样生成：

```powershell
# 【本地电脑执行】PowerShell：生成 32 位随机密钥
-join ((1..32) | ForEach-Object { '{0:x}' -f (Get-Random -Max 16) })
```

（嫌麻烦就自己编一串 32 位字母数字，只要和 `config.toml` 里填的**逐字符相同**即可。）

> 路线 A 的 6.4 里那条"WebUI 要登录令牌就去 `docker logs napcat` 看"的命令，在 Windows 上换成：直接看 launcher 那个黑窗口里打印的内容（把它贴回给你判断）。

**预期看到什么**：NapCat 黑窗口里没有红色报错；WebUI 能打开、扫码后能看到小号昵称；网络配置里那条 WebSocket 服务端是启用状态。

**失败了往哪查**：双击 `launcher.bat` 一闪而过 → 路径带中文/空格，换到 `D:\napcat` 重试；WebUI 打不开 → 黑窗口是不是被关掉/被安全软件拦了；提示缺运行库 → 按一键包内附的说明或 napneko.github.io 的 Windows 章节处理（把提示原样贴回）。

### 11.5 填 `config.toml`（与路线 A 完全一样）

```powershell
# 【本地电脑执行】PowerShell，必须在项目目录里
cd D:\qq-companion
Copy-Item config.example.toml config.toml
```

用记事本或 VS Code 编辑 `D:\qq-companion\config.toml`：**要改的 5 处、不要动的段落、判断标准，全部照 7.3 / 7.4 执行**（`allowed_user_id` 填大号、`bot_qq` 填小号、`access_token` 与 NapCat 一致、`[models.deepseek] api_key` 填自己的 key、第一次跑通把 `[voice] enabled` 改成 `false`）。

改完在**项目目录里**跑同一份校验脚本（PowerShell 没有 `<<` heredoc，所以用这种写法）：

```powershell
# 【本地电脑执行】在 D:\qq-companion 里执行；只读校验，不改任何文件
@'
from companion.config import Config
c = Config.load("config.toml")
p = c.llm.active()
print("大号 allowed_user_id =", c.account.allowed_user_id)
print("小号 bot_qq          =", c.account.bot_qq)
print("OneBot ws_url        =", c.onebot.ws_url)
print("OneBot token 长度    =", len(c.onebot.access_token))
print("模型预设 / base_url  =", c.llm.current, "/", p.base_url)
print("api_key 前缀/长度    =", p.api_key[:3], "/", len(p.api_key))
print("角色卡目录            =", c.character.path)
print("语音输入 enabled      =", c.voice.enabled)
print("看板 host:port        =", f"{c.admin.host}:{c.admin.port}")
'@ | .\venv\Scripts\python.exe -
```

**预期看到什么**：与 7.5 的判据完全相同（真实 QQ 号、`ws://127.0.0.1:3001`、token 长度 32、`api_key` 前缀 `sk-`、角色卡 `characters/example`、`[voice] enabled = False`）。注意 `@'` 和结尾的 `'@` 都必须**顶格单独一行**，这是 PowerShell 的固定写法。

**失败了往哪查**：报 `ModuleNotFoundError` → 没在 `D:\qq-companion` 里执行（`cd` 过去再跑）；打印出 `123456789` → 读到了模板，检查 `config.toml` 是否真的存在、文件名有没有变成 `config.toml.txt`（记事本另存为常见的坑）。

### 11.6 前台试跑（差异：命令写法，判据同路线 A）

```powershell
# 【本地电脑执行】PowerShell，在 D:\qq-companion 里
.\venv\Scripts\python.exe -m companion.main
```

- **成功判据完全同 8.2/8.3**：日志出现 `[OneBot] WebSocket 连接成功！`，然后用大号给小号发"在吗"，她会回（首条可能等 1~10 分钟，这是设计，不是卡住）；试通后按 `Ctrl+C` 停掉；
- 报错对照同样见 8.4；
- Windows 特有的两件事：① 首次运行时 Windows **防火墙**可能弹窗问是否允许联网——让她联网（她只往外部连，不需要任何人从外面进来，所以"专用网络"勾上就行，**不要**为了让别人访问而去开端口）；② 杀毒软件/安全软件可能拦 `python.exe`，把提示贴回给你判断。

### 11.7 开机自启（差异：任务计划程序替代 systemd）

**推荐做法**（无窗口，但**没有任何日志**，出问题不好查）：

1. 开始菜单搜「任务计划程序」→ 右侧「创建任务」（不是"创建基本任务"）；
2. **常规**：名称填 `qq-companion`；勾选「不管用户是否登录都要运行」需要填 Windows 密码，嫌麻烦就选「只在用户登录时运行」（电脑登录后她自动上线）；
3. **触发器**：新建 → 「登录时」；
4. **操作**：新建 → 程序或脚本填 `D:\qq-companion\venv\Scripts\pythonw.exe`，添加参数填 `-m companion.main`，**起始于**（很重要）填 `D:\qq-companion`；
5. 保存。之后每次登录 Windows，她就自动起来（`pythonw` 不弹黑窗口）。

**想留日志的替代做法**（会留一个黑窗口，但日志落到文件、看板 `/logs` 页也能读）：

```powershell
# 【本地电脑执行】先建日志目录
New-Item -ItemType Directory -Force D:\qq-companion\data\logs
```

任务里改成：程序或脚本 `cmd.exe`，添加参数 `/c "D:\qq-companion\venv\Scripts\python.exe -m companion.main >> D:\qq-companion\data\logs\bot.log 2>&1"`，起始于 `D:\qq-companion`。

> 提醒（别承诺做不到的事）：任务计划程序**不保证进程崩溃后自动拉起**，Windows 也没有 systemd 那种 `Restart=always`。想要更稳，用户可以自己加一个守护脚本，**不是必须的**。

**怎么判断她起来了**：浏览器打开 `http://localhost:8080` 能看到总览（总览里 OneBot 状态是"已连接"），或命令行：

```powershell
# 【本地电脑执行】
curl.exe -s http://127.0.0.1:8080/api/status
```

返回一段 JSON 且 `onebot_connected` 是 `true` 就是活着。

**失败了往哪查**：开机后没反应 → 任务计划程序里右键那条任务「运行」看是否报错；多半是"起始于"没填（于是找不到 `config.toml`）或程序路径写错。

### 11.8 电源设置：别让她"睡着"（路线 B 特有，必做）

- 「设置 → 系统 → 电源和电池」（Win10 里叫「电源和睡眠」）：把**接通电源时**的「睡眠」改成**从不**；屏幕关闭时间随意（关屏幕不影响她）；
- 笔记本再加一步：「控制面板 → 电源选项 → 选择关闭盖子的功能」→ **接通电源时**改成「不采取任何操作」，否则合盖就断；
- 台式机若无电压稳定顾虑，可在电源计划里把「硬盘」也设为不关闭。

> 如果不做这步，现象是"聊得好好的，十几分钟后她就不回了，动一下鼠标她又活了"——这不是 bug，是电脑睡了。

### 11.9 可选增强在路线 B 的差异（其余同第 12 节）

- **语音输入**：先装 ffmpeg——
  ```powershell
  # 【本地电脑执行】装了 winget 的 Win10/11 可直接用；装完必须重开 PowerShell
  winget install ffmpeg
  ```
  或用免安装包解压后把它的 `bin` 目录加进系统 PATH。然后下载 SenseVoice 模型、解压到 `D:\qq-companion\data\models\sensevoice`（**该目录下要直接看到 `model.int8.onnx` 和 `tokens.txt`**；Windows 没有 `wget`，用浏览器下载再用资源管理器解压即可），最后把 `[voice] enabled` 改成 `true`；**成功判据同 12.A**（她听懂语音；`data\logs\bot.log` 里出现 `[Voice] SenseVoice ASR 识别器加载成功`）。若在 11.7 里选了 `pythonw` 那种无日志方案，日志文件不存在是正常的——这时看聊天行为判断即可。
  - 注意：ffmpeg 没装/没进 PATH 时，语音消息只会降级成"没能听清"，**不会崩**。
- **语音回复 TTS**：与路线 A **完全一致**（`edge-tts` 已经在 requirements 里装好了；`provider`、`daily_limit = 30`、`max_chars = 90`、作息闸门都一样，见 12.B）。唯一区别是电脑睡着时她发不出语音（同 11.8）。
- **换角色卡**：同 12.C，把 `[character] path` 改成自己的目录即可（Windows 上路径用 `characters/my_character` 这种正斜杠写法就行）。

### 11.10 看板与运维（路线 B 的优势）

- 看板：**不用 SSH 隧道**，浏览器直接开 `http://localhost:8080`，各页面同第 10 节的表。注意 `/logs` 页只在 11.7 选了"留日志"那种配法时才有内容——用 `pythonw` 无日志方案时它一直是空的，**这是正常的**，别当成故障；
- 升级代码：在 `D:\qq-companion` 里 `git pull` → `.\venv\Scripts\pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/` → 按 11.1 的对照表重启（等价于第 14 节）；
- 重置数据：先停掉她（任务管理器结束 `python.exe`/`pythonw.exe`），再 `.\venv\Scripts\python.exe -m companion.reset`（规则同第 15 节，`--purge-all` 同样建议不要加）。

---

## 12. 可选增强（**不是必须**，先把文字聊天跑通再回来弄）

> **路线 B（Windows）的差异只有四处**：ffmpeg 改用 `winget install ffmpeg`（见 11.9）；SenseVoice 模型解压到本机 `data\models\sensevoice`（Windows 没有 `wget`，用浏览器下载再解压）；重启/启用配置换成 11.1 对照表里的写法；下面 C 里的复制角色卡改成 `Copy-Item -Recurse characters\example characters\my_character`。其余步骤与判据完全一样，差异清单见 **11.9**。

### A. 语音输入（她能"听懂"用户发的语音）

前置：`ffmpeg` 已在阶段 2 装好。还差一个约 200MB 的离线识别模型（**不会自动下载**，缺了只是降级成"没能听清"，不会崩）。

```bash
# 【服务器终端执行】
mkdir -p /opt/qq-companion/data/models/sensevoice
cd /opt/qq-companion/data/models/sensevoice
wget https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2
tar xvf sherpa-onnx-sense-voice-*.tar.bz2 --strip-components=1
rm sherpa-onnx-sense-voice-*.tar.bz2
ls
```

**预期看到什么**：`ls` 里有 `model.int8.onnx` 和 `tokens.txt` 两个文件（**必须直接在这个目录下**，多套一层子目录就会失效）。若提示 `wget: command not found`，先执行 `sudo apt install -y wget` 再重跑下载那条命令。

如果服务器连 GitHub 太慢/下不动：让用户在**自己电脑的浏览器**里下载上面那个压缩包，然后:

```bash
# 【本地电脑执行】Git Bash 或 PowerShell 都可以；把路径、用户名和 IP 换成真实值
scp sherpa-onnx-sense-voice-*.tar.bz2 ubuntu@203.0.113.10:/home/ubuntu/
```
```bash
# 【服务器终端执行】
tar xjf /home/ubuntu/sherpa-onnx-sense-voice-*.tar.bz2 -C /opt/qq-companion/data/models/sensevoice --strip-components=1
ls /opt/qq-companion/data/models/sensevoice
```

然后打开语音输入：

```bash
# 【服务器终端执行】
cd /opt/qq-companion
nano config.toml      # 找到 [voice]，把 enabled = false 改成 enabled = true；Ctrl+O 回车 → Ctrl+X
sudo systemctl restart qq-companion
```

**成功判据**：用大号发一条语音，她回的内容对得上；日志里能看到 `[Voice] SenseVoice ASR 识别器加载成功`。若日志出现 `[Voice] SenseVoice 模型文件未在 ... 找到` → 回上一步确认那两个文件的位置（多半是解压多套了一层目录）。

### B. 语音回复 TTS（她用语音回用户）

打开方式：

```bash
# 【服务器终端执行】
cd /opt/qq-companion
nano config.toml      # 找到 [tts]，把 enabled = false 改成 enabled = true，并选 provider
sudo systemctl restart qq-companion
```

`provider` 二选一（`[tts] provider = "edge"` 或 `"minimax"`）：

| | `edge` | `minimax` |
|---|---|---|
| 音色 | 一般（微软在线音色，默认 `zh-CN-XiaoxiaoNeural`，语速 `rate = "-8%"`） | 明显更好（默认音色 `Chinese (Mandarin)_Gentle_Senior`，即"温柔学姐"） |
| 费用 | 零成本 | 需要 MiniMax 开放平台的 API key 与**账户余额**（按量计费，量级约每月几元） |
| key 从哪来 | 不需要 key | **复用 `[models.minimax] api_key`**：去 MiniMax 开放平台注册、充值、建 key，填到 `[models.minimax] api_key` 那一行（不要在 `[tts]` 里另填 key，那里没有这个字段） |
| 依赖 | `edge-tts` 已在 requirements 里装好了 | 同上，无额外依赖 |

> `[tts]` 里的 `model = "speech-2.8-hd"`、`speed`、`group_id`（留空）保持默认即可，一般不用改。

两道保险丝的含义，给用户解释清楚（**她不是每句都发语音，这是正常的**）：

- `daily_limit = 30`：每天最多 30 条语音，超过就自动退回纯文字；
- `max_chars = 90`：单条语音最多 90 字（约 30 秒）。长回复会被截到句读处再说；
- 另外还有一道**作息闸门**：她在睡觉/上课之类"不方便说话"的活动里，会自动改用文字。

**成功判据**：聊天里偶尔出现语音条。若一直只有文字，先看 `tail -f /opt/qq-companion/data/logs/bot.log | grep TTS`：显示已达上限 → 明天再试；显示合成失败 → 检查 provider 与 key。

### C. 换成用户自己的角色（不要用示例角色谈恋爱）

- 定制路径（怎么换）：`docs/CUSTOMIZE.md`；
- 方法论长文（怎么写得不像 AI）：`docs/CARD_CRAFT.md`；
- 最省事的做法：`cp -r characters/example characters/my_character`，改 `characters/my_character/character.json`，再把 `[character] path` 改成 `"characters/my_character"`。这样升级代码时不会碰到他自己写的卡。

---

## 13. 排障速查表

| 症状 | 多半是什么 | 让用户做什么 |
|---|---|---|
| 小号掉线/被踢/要重新扫码 | QQ 侧风控或登录态过期 | 重启 NapCat（路线 A：`docker restart napcat`；路线 B：关掉黑窗口重新双击 `launcher.bat`），回 WebUI 重新扫码。提醒：小号越新越容易被踢，先在手机端正常养几天 |
| 日志反复 `Connection refused 3001` | NapCat 没起 / WS 服务端没配 / token 不一致 | 确认 NapCat 在跑（路线 A `docker ps`；路线 B 看那个黑窗口）；回去核对 6.4 的端口与 Token 和 `config.toml` 是否一致 |
| 回复很慢（几分钟才回） | 正常的"首条延迟"（忙碌时 1~10 分钟） | 先观察，不是故障。想确认她在想什么，看 `/debug` 页的完整提示词 |
| 想确认花了多少钱 | `/costs` 页有累计费用与按用途明细 | 提醒：`[llm.pricing] holidays` 不填最新法定节假日，峰谷价会算不准（只是统计偏差，不影响运行） |
| 磁盘满 / 写不进去 | 日志与备份堆积 | 路线 A：`df -h`；`du -sh /opt/qq-companion/data/*`；必要时 `sudo journalctl --vacuum-size=200M`；清理 `/tmp`。路线 B：`Get-PSDrive D` 看剩余空间，清理 `data\backup\` 里过老的备份。两边都可以 `docker system prune -f`（只清未使用镜像，**确认 napcat 在跑**）——路线 B 没用 Docker，跳过这条 |
| 升级到最新代码 | 新版本有新依赖 | 见下面第 14 节 |
| 想彻底重来（清空关系数据） | 好感度/日记/记忆全归零 | 见下面第 15 节 |
| 看板页面全部 403「未授权」 | 用户自己设了 `[admin].token` | 访问 `http://localhost:8080/?token=他的令牌`（页面内链接会自动带令牌，留空则无此问题） |
| 她凌晨发消息 / 作息整体偏 8 小时 | **路线 A 专属**：服务器时区不是北京时间 | `sudo timedatectl set-timezone Asia/Shanghai` 后 `sudo systemctl restart qq-companion`（路线 B 用本机时间，不会有这个问题） |
| 她偶尔完全不回话 | 这是"沉默权"机制（该收尾时可以选择不接） | 正常，不是坏了 |
| 该是语音却收到文字 | 触发了语音闸门（每日上限/字数/作息） | 正常；想调就看可选增强 B 里的三个参数 |
| 表情包发出去没显示 | NapCat 那份表情表里没有这个 id，会被静默丢弃 | 属 NapCat 版本差异，先记录、不用改代码（详见 `docs/DEPLOY.md` 第 6.1 节） |
| **（路线 B）**聊得好好的，十几分钟后她突然不回，动一下鼠标又活了 | **电脑睡眠了**（默认电源设置就会） | 按 11.8 把"睡眠"改成"从不"；笔记本额外改"关闭盖子"的动作。顺手也检查会不会是她被"暂停"（任务管理器里看 `python.exe` 还在不在） |
| **（路线 B）**开机后她没起来 | 任务计划没配好（最常见是"起始于"没填，于是找不到 `config.toml`） | 按 11.7 逐项核对；在任务计划程序里右键那条任务点「运行」，看是否报错并把提示贴回 |
| **（路线 B）**NapCat 一键包双击一闪而过 / 起不来 | 解压路径带中文或空格，或被安全软件拦下 | 换到 `D:\napcat` 重新解压再试；仍不行把安全软件的拦截提示或窗口内容贴回 |
| **（路线 B）**Windows 半夜自动更新重启后她不在线 | 重启打断了进程，且没设开机自启 | 按 11.7 配任务计划（登录时触发），并把 Windows 更新设为"活动时间"内不自动重启 |

---

## 14. 以后怎么升级代码

```bash
# 【服务器终端执行】
cd /opt/qq-companion
git pull
./venv/bin/pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
sudo systemctl restart qq-companion
tail -n 20 /opt/qq-companion/data/logs/bot.log
```

解释：`git pull` 只更新仓库里跟踪的文件，`config.toml` 被 `.gitignore` 排除，**不会被覆盖**；新版本如果加了依赖，必须重跑 pip；重启后看日志确认没有报错。

> **路线 B**：把上面四行换成 11.1 对照表里的 Windows 写法（`git pull` 一样；pip 用 `.\venv\Scripts\pip`；重启=结束进程后重新启动，或直接重启电脑）。

## 15. 怎么彻底重置数据

```bash
# 【服务器终端执行】
sudo systemctl stop qq-companion
cd /opt/qq-companion
./venv/bin/python -m companion.reset
sudo systemctl start qq-companion
```

解释：它会把聊天记录、好感度、情绪、日记、记忆全部清空，回到"刚认识"的状态。**执行前会自动备份一份**到 `data/backup/daily/`，所以可以反悔。交互会要求手敲大写 `YES` 确认（加 `--yes` 可跳过）。

- 默认**保留**表情包图库和计费历史；只有加了 `--purge-all` 才会连它们一起清掉 —— **建议永远不要加**，除非用户明确要求"彻底清空一切"；
- 重置后 40 分钟内她的"最近生活"是空的、话变少、不主动，全部正常（好感度要重新涨）。

> **路线 B**：先停掉她（任务管理器结束 `python.exe` / `pythonw.exe`），再在 `D:\qq-companion` 里执行 `.\venv\Scripts\python.exe -m companion.reset`，最后重新启动。

---

## 16. 安全红线（AI 必须替用户守住）

1. **路线 A：3001 / 6099 / 8080 一律不对外开放**：NapCat 的运行命令里已经绑死 `127.0.0.1`，安全组只放行 22。如果用户问"能不能从手机在外面看她的状态"，答案是"再开一次 SSH 隧道"，而不是改安全组。
2. **路线 B：同样只绑 127.0.0.1，不要为了让手机在外访问而改绑定地址**。想在 Windows 上被外部访问，就得把 NapCat / 看板 / OneBot 绑到 `0.0.0.0` 并在防火墙开端口——那等于把她的记忆、日志和 QQ 登录态挂到公网上，**不要做**。真要在外面看，回到路线 A（服务器 + SSH 隧道）。
3. **看板的 `[admin].host` 保持 `127.0.0.1`**（两条路线都一样）。真要改成非回环地址（如 `0.0.0.0`），**必须同时**在 `[admin].token` 设一个令牌，否则等于把她的记忆、日志、计费和关系状态公开在互联网上（读写都会裸奔）。
4. **`config.toml` 永不入库、永不外传**：里面有 QQ 号、OneBot 密钥、API key。不要给用户任何"上传/覆盖服务器 `config.toml`"的命令——改配置一律就地改（路线 A 在服务器上用 `nano`，路线 B 在本机用记事本/VS Code）。
5. **贴日志先打码**：日志里会出现 QQ 号，贴到公开 issue/论坛前要替换掉；API key 更不能出现在任何截图里。
6. **风险要讲在前面**：用小号挂协议端有被封的风险；这一点在起步时就要说，不要等出了事才说。
7. **破坏性操作先确认**：`companion.reset`、`--purge-all`、`docker system prune`、`rm`、改 systemd 文件或 Windows 任务计划，执行前告诉用户会发生什么。

---

## 17. 交付给用户的三句话（部署完成时说）

1. 她现在是**怎么个在线法**：路线 A——24 小时在线，电脑关机也在；路线 B——你电脑开着且没睡眠的时候在线（所以别让她睡着，见 11.8）；
2. 机器人只回他自己的大号，别人发消息她不会理；
3. 想换人设看 `docs/CUSTOMIZE.md`；想看她"心里在想什么"就看状态看板。
