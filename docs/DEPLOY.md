# 部署操作指南（DEPLOY）

> 把本项目从 Windows 开发机部署到腾讯云 Ubuntu 22.04 服务器的完整流程。
> 按顺序执行，每步都有验证方法。预计 40~60 分钟。
> 约定：`服务器IP` 替换为你的腾讯云公网 IP；本地命令在 Windows PowerShell 或 Git Bash 执行，服务器命令以 `# 服务器` 标注。

---

## 第 0 步：本地准备（在 Windows 开发机上）

### 0.1 填写 config.toml

打开项目根目录的 `config.toml`，填 5 处：

```toml
[account]
allowed_user_id = 11111111     # ← 你的大号 QQ 号
bot_qq = 22222222              # ← 小号 QQ 号

[onebot]
ws_url = "ws://127.0.0.1:3001"
access_token = "编一串32位随机字符"   # ← 自己编，例如用密码生成器；记下来，NapCat 要填同一个

[llm]
api_key = "sk-你的key"          # ← https://platform.deepseek.com → API keys → 创建
# 其余保持默认（三个模型均为 deepseek-flash）

[character]
path = "characters/example"     # ← 改成你的角色卡目录名
```

可选：`[llm.pricing] holidays` 填入今年法定节假日（如 `["2026-10-01", "2026-10-02"]`），费用统计更准确。

### 0.2 确认角色卡

- 角色卡目录（如 `characters/example/`）里 `character.json` 和 `stickers/`（含 index.json 和图片）齐全；
- 本地跑一下确认能加载：

```bash
./venv/Scripts/python.exe -c "from companion.persona import Persona; p=Persona.load('characters/example'); print('OK:', p.name, len(p.stages), '个阶段')"
```

### 0.3 小号检查

- 小号是注册过一段时间、有正常使用的 QQ 号（新号直接挂协议端易触发风控）；
- 小号在手机 QQ 上能正常登录，**加你的大号为好友**（否则私聊可能被拦）；
- 手机保持电量充足，扫码登录那一步要用。

---

## 第 1 步：安全组（腾讯云控制台）

控制台 → 云服务器 → 实例 → 安全组 → 确认**入站规则只放行 22（SSH）**。

> 不需要放行任何其他端口。NapCat WebUI（6099）、状态仪表盘（8080）、OneBot（3001）全部绑定 127.0.0.1，通过 SSH 隧道访问，公网摸不到。

---

## 第 2 步：上传代码

二选一。

**方式 A：scp 直传（简单直接，推荐首次使用）**

```bash
# 本地，Git Bash，在项目上一级目录执行：
cd /d
tar --exclude='QQ chatter/venv' --exclude='QQ chatter/data' --exclude='QQ chatter/.git' \
    --exclude='QQ chatter/config.toml' \
    -czf qq-companion.tar.gz "QQ chatter"
scp qq-companion.tar.gz ubuntu@服务器IP:/home/ubuntu/
```

```bash
# 服务器：
sudo mkdir -p /opt/qq-companion
sudo tar -xzf /home/ubuntu/qq-companion.tar.gz -C /opt/qq-companion --strip-components=1
sudo chown -R ubuntu:ubuntu /opt/qq-companion
```

> **死规则：服务器上的 `config.toml` 一律就地编辑，禁止用本地上传覆盖。**
> 覆盖会**静默清空**服务器已填的节假日（本地 `holidays = []`）——计费把长假按工作日高峰算、
> 作息把长假当上课日；若本地的 QQ 号 / access_token 与服务器不一致，还会直接登不上。
> 上面的打包命令已用 `--exclude='QQ chatter/config.toml'` 把它排除在外，解包只会带代码与角色卡。
>
> 首次部署时服务器上还没有 `config.toml`：在服务器上 `cp /opt/qq-companion/config.example.toml /opt/qq-companion/config.toml`，再用 `nano` 就地逐项填写（字段说明见第 0 步 0.1）。此后**任何一次**上传 / 解包都不再包含它，配置改动一律在服务器上改。

**方式 B：GitHub 中转（适合以后长期迭代）**

```bash
# 本地：先确认 config.toml、data/、characters/example/ 不在 git 跟踪里
cd "/d/qq-companion" && git status --short
# 然后提交推送，服务器上 git clone；
# 注意：clone 后服务器上没有 config.toml 和你的私有角色卡，需单独 scp：
# ⚠ 这两条 scp 只限**首次安装**（服务器上还没有 config.toml 时）执行；
#   服务器已有 config.toml 时禁止覆盖，改动一律就地编辑（见方式 A 的死规则）。
scp "/d/qq-companion/config.toml" ubuntu@服务器IP:/opt/qq-companion/
scp -r "/d/qq-companion/characters/example" ubuntu@服务器IP:/opt/qq-companion/characters/
```

---

## 第 3 步：服务器系统环境

```bash
# 服务器：
sudo apt update

# 时区（必做，第一条就做）：云服务器默认时区多为 UTC，不改的话
# 作息表（早八/熄灯）、法定节假日判定、主动消息免打扰时段会整体偏 8 小时
# ——北京时间凌晨她当傍晚（该静的时候发消息），白天她当清晨。
sudo timedatectl set-timezone Asia/Shanghai
timedatectl    # 应显示 Time zone: Asia/Shanghai (CST, +0800)

# Ubuntu 22.04 自带 Python 3.10，本项目需要 3.11+（tomllib），用 deadsnakes 源安装：
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.11 python3.11-venv ffmpeg docker.io

# 验证
python3.11 --version    # 应显示 3.11.x
ffmpeg -version | head -1
docker --version
date    # 应显示北京时间（CST）
```

## 第 4 步：Python 环境

```bash
# 服务器：
cd /opt/qq-companion
python3.11 -m venv venv
./venv/bin/pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

> `requirements.txt` 里四个依赖都钉了 `==` 版本（可复现优先：本项目是 7×24 长跑服务，
> 不能让 pip 静默升到新大版本）。**已经在跑的服务器**若只是想拿某次代码改动，
> 不必重跑这条命令；只有要顺带升级依赖时才重跑，跑完必须
> `sudo systemctl restart qq-companion` 并确认日志无异常。
>
> sherpa-onnx 包较大（含 ONNX 运行时），下载耐心等待。它只服务语音输入。

## 第 5 步：语音识别模型（SenseVoice，约 200MB，**不会自动下载**）

> **必须手动下载**：代码里没有"首次运行自动下载模型"的逻辑（`companion/voice.py` 只按
> `[voice].model_dir` 去读本地文件）。文件不在时语音消息降级成占位符
> `[对方发来一条语音，但没能听清]`，并在 `data/logs/bot.log` 留一条警告，**不会报错崩掉**。
>
> 语音输入依赖两样手工资产：
> 1. 系统工具 **ffmpeg**（第 3 步 `apt install` 已装）：把 QQ 语音（SILK）转成 16000Hz 单声道 wav；缺了它一律降级占位符；
> 2. **sherpa-onnx SenseVoice int8 模型**（下面这段）：含 `model.int8.onnx` 与 `tokens.txt`
>    两个文件，缺任一个都降级占位符；识别在本机 CPU 上跑（`asyncio.to_thread`，不阻塞事件循环），不联网、不额外收费。

```bash
# 服务器：
mkdir -p /opt/qq-companion/data/models/sensevoice
cd /opt/qq-companion/data/models/sensevoice
wget https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2
tar xvf sherpa-onnx-sense-voice-*.tar.bz2 --strip-components=1
rm sherpa-onnx-sense-voice-*.tar.bz2
ls    # 应看到 model.int8.onnx 和 tokens.txt
```

如果服务器连 GitHub 太慢/失败：在本地浏览器下载该压缩包，然后

```bash
# 本地 Git Bash：
scp sherpa-onnx-sense-voice-*.tar.bz2 ubuntu@服务器IP:/home/ubuntu/
# 服务器：
tar xjf /home/ubuntu/sherpa-onnx-sense-voice-*.tar.bz2 -C /opt/qq-companion/data/models/sensevoice --strip-components=1
ls /opt/qq-companion/data/models/sensevoice   # 应看到 model.int8.onnx 和 tokens.txt
```

> 不用语音功能可跳过本步，并在 config.toml 设 `[voice] enabled = false`。
> 装完想验证：用大号发一条语音，`tail -f /opt/qq-companion/data/logs/bot.log` 里应出现
> `[Voice] SenseVoice ASR 识别器加载成功`；若看到"模型文件未找到"，多半是上面 `ls` 里的
> 两个文件没落在 `data/models/sensevoice/` 根下（解压多套了一层目录）。

## 第 6 步：NapCat 协议端

```bash
# 服务器：
cd /opt/qq-companion
docker run -d --name napcat --restart always \
  -e NAPCAT_GID=$(id -g) -e NAPCAT_UID=$(id -u) \
  -v ./napcat-data:/app/.config/QQ \
  -v ./napcat-cache:/app/napcat/cache \
  -p 127.0.0.1:3001:3001 -p 127.0.0.1:6099:6099 \
  mlikiowa/napcat-docker:latest

docker ps    # 确认 napcat 在运行
```

### 6.1 核对 QQ 系统表情能否发出（FIXES20 发侧版本闸，必做）

> **为什么必须做**：NapCat 发送一条 `face` 段时，如果这个 id 不在**它自己那份**
> `face_config.json` 里，会**静默丢弃整段**——不报错、不重试、消息照样发出去，
> 症状是"她明明要发表情，屏幕上什么都没有"，事后极难查（NapCat issue #1987）。
> 本地清单是照 NapCat **2026-10-04 的 main 分支**核过的；服务器上是
> `mlikiowa/napcat-docker:latest`，版本可能更旧或更新，**以服务器那份为准**。

```bash
# 服务器：
cd /opt/qq-companion
docker exec napcat sh -c "ls /app/napcat/core/external/face_config.json 2>/dev/null \
  || find / -name face_config.json -path '*napcat*' 2>/dev/null | head -1"
```

拿到路径后，把 `companion/faces.py` 里的 `PROMPT_FACE_LIST` 26 个标签逐个对一遍：
用服务器上那份 `face_config.json` 的 `sysface` 数组查标签名 → id，查不到的从清单剔除。

```bash
# 服务器：把服务器那份表与本地清单对账（输出"本地清单里这台机器发不出的标签"）
docker exec napcat cat <上面查到的face_config.json路径> > /tmp/server_face_config.json
./venv/bin/python - <<'PY'
import json, sys
sys.path.insert(0, ".")
from companion.faces import QQ_FACE_ID_BY_NAME, QQ_FACE_TAGS
from companion.prompts import PROMPT_FACE_LIST
srv = {x["QDes"].lstrip("/") for x in json.load(open("/tmp/server_face_config.json", encoding="utf-8"))["sysface"]}
bad = [n for n, _ in PROMPT_FACE_LIST if n not in srv or QQ_FACE_ID_BY_NAME.get(n) not in QQ_FACE_TAGS]
print("这台机器发不出的标签：", bad or "无（全部可发）")
PY
```

- 输出「无」→ 直接进第 7 步；
- 输出里有标签 → 把它们从 `companion/prompts.py` 的 `PROMPT_FACE_LIST` 里剔除，
  改完**必须**重跑一次 `./venv/bin/python -m unittest discover -s tests`（版本闸有单测守着），
  再重新 scp 上传那两个文件。

## 第 7 步：扫码登录小号 + 配置 OneBot 接口（本地浏览器操作）

```bash
# 本地（PowerShell 或 Git Bash），保持此窗口不断开：
ssh -L 6099:127.0.0.1:6099 -L 8080:127.0.0.1:8080 ubuntu@服务器IP
```

本地浏览器打开 `http://localhost:6099/webui`：

1. **登录**：按页面提示用**手机 QQ 小号扫码**登录（手机和小号保持在线有助于稳定）；
   - 若提示风险/需要验证码：按 NapCat 页面指引处理，常见做法是先让小号在手机端正常使用几天；
2. **配置 WebSocket 服务端**：左侧「网络配置」→ 新建 → 类型选 **WebSocket 服务端**：
   - 端口：`3001`
   - 启用：开
   - Token：填和 `config.toml` 里 `access_token` **完全相同**的那串
   - 保存并启用；
3. 在 NapCat 日志里确认看到 WebSocket 服务已监听。

## 第 8 步：手动冒烟测试（先别挂守护）

```bash
# 服务器：
cd /opt/qq-companion
./venv/bin/python -m companion.main
```

看到连接 OneBot 成功的日志后，**用大号 QQ 给小号发一条消息**（比如"在吗"）。

- ✅ 小号回复了 → Ctrl+C 停掉，进入第 9 步；
- ❌ 没反应 → 看下方「故障排查」。

顺手再测两条：发一张照片（她应该描述内容）、发一条语音（她应该听懂；第 5 步没做则回复听不清的占位符）。

### 8.1 核对 NapCat 输入状态事件（FIXES23，必做 · 本地无法替代）

> **为什么必须做**：「他打字她等」这个功能依赖 NapCat 上报的**对方输入状态**
> （`notice` / `sub_type=input_status`）。这个功能是**纯增强**——事件收不到时
> 机器人行为与改动前逐字节一致，不会瘫痪、不会饿死回复。但也正因为收不到就静默
> 降级，**光看"用起来正常"证明不了事件真的到了**。
>
> 事件里的 `event_type` **极性**（1=正在输入 还是 0=正在输入）目前**没有权威文档**：
> NapCat 自己的 `set_input_status` 动作用 1=正在输入，而跨协议库 nagisa 的注释
> 写的是 0=输入中并自标"端相关"，两边说法相反。代码按前者实现（见
> `companion/onebot.py` 的 `INPUT_STATUS_TYPE_ON`）。**取错极性的后果被两层兜底
> 夹住**（协议层 15 秒自愈 + 聚合器 30 秒绝对上限），最坏只是"等他"的时机对调、
> 退化成约等于现状——但观察期指标会全是"因输入状态延长 0 次"，看起来像功能没上。

**第一步：抓一条真实事件原文**

```bash
# 服务器：
docker logs -f napcat 2>&1 | grep -A2 -i "输入状态\|input_status"
```

让**大号在手机 QQ 上给小号发一条消息，且在发送前先停顿几秒不放手**（真实打字）。
NapCat 日志里应出现 `[Notice] [输入状态] ...` 的行。若**一条都没有**：

- 说明这个 NapCat 版本/配置不上报该事件 → 功能会静默不生效，属预期降级，
  不用改代码，在本节末尾记一行"本环境无该事件"即可；
- 若日志有但 `bot.log` 里没有 `[OneBot] 对方开始输入`，说明报文形状与解析不符，
  把 NapCat 那条日志原文贴回来，据此调 `companion/onebot.py` 的
  `parse_input_status_event`。

**第二步：核对极性（事件到了才做）**

```bash
# 服务器：
tail -f /opt/qq-companion/data/logs/bot.log | grep -E "对方开始输入|对方停止输入|输入状态自愈|他还在打字|他已停手"
```

用大号在手机 QQ 输入框里**打字但先不发送**，停 5 秒以上：

- 看到 `对方开始输入` → 极性正确，`1` 就是"正在输入"，**不用动代码**；
- 看到的是 `对方停止输入`（或干脆没反应、紧接着一条"对方开始输入"出现在你真正
  发消息之后）→ 极性反了，把 `companion/onebot.py` 里
  `INPUT_STATUS_TYPE_ON` / `INPUT_STATUS_TYPE_OFF` 两个常量**对调**，
  重新 scp 上传该文件并重启服务：

```bash
# 服务器：
sudo systemctl restart qq-companion
```

**第三步：看聚合器有没有真的等他**（同上 grep，多加一条）

```bash
# 服务器：
tail -f /opt/qq-companion/data/logs/bot.log | grep "Aggregator"
```

大号连发 3 条（每条之间故意停 7 秒以上），应看到 `他还在打字，暂不插话`，
且这 3 条**合成一轮**提交（日志里是 `提交一轮: 3 条`）。若 3 条被切成 3 轮，
说明输入状态没到聚合器，回第一步。

> **观察期指标**：聚合日志里 `他还在打字，暂不插话` 的出现频率。
> 频率**接近零**说明 NapCat 事件没到——该去查，而不是该庆幸。

## 第 9 步：systemd 守护（24 小时运行）

```bash
# 服务器：
sudo cp /opt/qq-companion/deploy/qq-companion.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qq-companion
sudo systemctl status qq-companion     # 应显示 active (running)
```

日志查看：`tail -f /opt/qq-companion/data/logs/bot.log`

崩溃自愈验证：`sudo kill <机器人PID>`，约 5 秒后 `systemctl status` 应看到它已被自动拉起。

## 第 10 步：日常使用入口

**看她的"脑内状态"仪表盘：**

```bash
# 本地，每次想看时先开隧道（窗口保持开着）：
ssh -L 8080:127.0.0.1:8080 ubuntu@服务器IP
# 浏览器打开：http://localhost:8080
```

五个页面：总览（好感度六维/情绪/阶段）、记忆（日记+衰减强度）、调试（最近完整提示词）、计费（缓存命中率/今日费用）、表情包库。

> 若你在 `[admin].token` 设了令牌（只建议在把 `[admin].host` 改成非回环地址时设），
> 那么**读写所有页面**都要凭令牌：浏览器访问 `http://localhost:8080/?token=你的令牌`
> 即可，页面内链接会自动带上令牌；命令行取健康数据用
> `curl -s "http://127.0.0.1:8080/api/status?token=你的令牌"`。令牌留空（默认）时
> 一切照旧，无需任何参数。

**以后更新代码：**

```bash
# 本地修改后（方式 B 工作流）：
git push
# 服务器：
cd /opt/qq-companion && git pull && sudo systemctl restart qq-companion
```

---

## 故障排查

| 现象 | 排查 |
|---|---|
| 机器人日志反复 `Connection refused 3001` | NapCat 容器没起来（`docker ps`）或 WebSocket 服务端没配/端口不对/token 不一致 |
| NapCat 扫码提示风控/登录失败 | 小号太新。手机端正常养号几天再试；或按 NapCat 文档换登录方式/手表协议 |
| 发消息完全没反应 | ① 大号小号是否互为好友 ② `allowed_user_id` 是否填的大号 ③ `tail -f data/logs/bot.log` 看事件有没有到达 |
| 回复"刚刚走神了……" | DeepSeek 调用失败：检查 api_key、账户余额、服务器能否访问 api.deepseek.com（`curl https://api.deepseek.com`） |
| 语音回复"没能听清" | 第 5 步模型没装好：确认 `data/models/sensevoice/` 下有 `model.int8.onnx` 和 `tokens.txt`；`ffmpeg -version` 正常 |
| 识图没反应/说看不到 | config.toml 的 `vision_model` 被留空了，应填 `deepseek-flash` |
| 仪表盘打不开 | 隧道命令里的 8080 两端都要写；机器人进程必须在运行 |
| 仪表盘所有页面都 403「未授权」 | 你在 `[admin].token` 设了令牌：访问时要在地址后加 `?token=你的令牌`（页面内链接会自动带上）；`/api/status` 同理 |
| 作息/免打扰时段整体偏 8 小时（凌晨来消息） | 服务器时区不是北京时间：`sudo timedatectl set-timezone Asia/Shanghai` 后重启服务（见第 3 步） |
| 日志里 `模型文件未在 ... 找到` | 第 5 步模型没装好：`data/models/sensevoice/` 根下应有 `model.int8.onnx` 与 `tokens.txt` |

## 验收

部署完成后按 `PLAN.md` §15 的 10 条总验收逐项过一遍（都是聊天实操）。
