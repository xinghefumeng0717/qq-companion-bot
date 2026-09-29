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
path = "characters/qingzi"     # ← 改成你的角色卡目录名
```

可选：`[llm.pricing] holidays` 填入今年法定节假日（如 `["2026-10-01", "2026-10-02"]`），费用统计更准确。

### 0.2 确认角色卡

- 角色卡目录（如 `characters/qingzi/`）里 `character.json` 和 `stickers/`（含 index.json 和图片）齐全；
- 本地跑一下确认能加载：

```bash
./venv/Scripts/python.exe -c "from companion.persona import Persona; p=Persona.load('characters/qingzi'); print('OK:', p.name, len(p.stages), '个阶段')"
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
    -czf qq-companion.tar.gz "QQ chatter"
scp qq-companion.tar.gz ubuntu@服务器IP:/home/ubuntu/
```

```bash
# 服务器：
sudo mkdir -p /opt/qq-companion
sudo tar -xzf /home/ubuntu/qq-companion.tar.gz -C /opt/qq-companion --strip-components=1
sudo chown -R ubuntu:ubuntu /opt/qq-companion
```

> config.toml 会被一起传上去（服务器需要它），它不进 GitHub 但要上传服务器。

**方式 B：GitHub 中转（适合以后长期迭代）**

```bash
# 本地：先确认 config.toml、data/、characters/qingzi/ 不在 git 跟踪里
cd "/d/qq-companion" && git status --short
# 然后提交推送，服务器上 git clone；
# 注意：clone 后服务器上没有 config.toml 和你的私有角色卡，需单独 scp：
scp "/d/qq-companion/config.toml" ubuntu@服务器IP:/opt/qq-companion/
scp -r "/d/qq-companion/characters/qingzi" ubuntu@服务器IP:/opt/qq-companion/characters/
```

---

## 第 3 步：服务器系统环境

```bash
# 服务器：
sudo apt update

# Ubuntu 22.04 自带 Python 3.10，本项目需要 3.11+（tomllib），用 deadsnakes 源安装：
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.11 python3.11-venv ffmpeg docker.io

# 验证
python3.11 --version    # 应显示 3.11.x
ffmpeg -version | head -1
docker --version
```

## 第 4 步：Python 环境

```bash
# 服务器：
cd /opt/qq-companion
python3.11 -m venv venv
./venv/bin/pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

> sherpa-onnx 包较大（含 ONNX 运行时），下载耐心等待。

## 第 5 步：语音识别模型（SenseVoice，约 200MB）

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
```

> 不用语音功能可跳过本步，并在 config.toml 设 `[voice] enabled = false`。

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

## 验收

部署完成后按 `PLAN.md` §15 的 10 条总验收逐项过一遍（都是聊天实操）。
