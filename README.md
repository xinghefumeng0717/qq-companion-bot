# QQ 伴侣机器人 (QQ Companion Bot)

一个在 QQ 上和你一对一私聊的角色扮演伴侣。它 24 小时挂在一台 Linux 服务器或你自己的 Windows 电脑上，用一个 QQ 小号登录，只回你一个人。

她是谁由一张角色卡决定，代码里不写任何具体角色。她会记事、会忘事，有心情和关系进度，有自己的作息和生活，偶尔主动找你，也能发语音。

> **想把部署交给 AI 助手？** 仓库里有一份专门写给 AI 的部署说明书 [`docs/DEPLOY_FOR_AI.md`](docs/DEPLOY_FOR_AI.md)，覆盖服务器和本机两条路线。把它丢给你的 AI 助手，照着带你做就行。

---

## 目录

1. [她能做什么](#1-她能做什么)
2. [这个项目在意什么](#2-这个项目在意什么)
3. [一条消息怎么变成她的回复](#3-一条消息怎么变成她的回复)
4. [安装](#4-安装)
5. [部署 NapCat](#5-部署-napcat)
6. [配置与启动](#6-配置与启动)
7. [写你自己的角色卡](#7-写你自己的角色卡)
8. [看板](#8-看板)
9. [后台常驻运行](#9-后台常驻运行)
10. [出了问题怎么查](#10-出了问题怎么查)
11. [测试](#11-测试)
12. [隐私与安全](#12-隐私与安全)
13. [代码地图](#13-代码地图)
14. [更多文档](#14-更多文档)

---

## 1. 她能做什么

**聊天**
- 像真人用手机聊天那样说话：短句，一次回一到几条，不写括号里的动作和心理描写。
- 等你把话说完再回。你连发几条，她会当成一轮来读。
- 看得懂你发的图片、QQ 小黄脸和你引用的旧消息。
- 你要语音时发语音，聊到晚安、想你这类话时也会自己挑一句念出来。语音没发出去会原话改成文字；你要的那条还会在下一轮补发一次。
- 偶尔顺手发一个表情包或 QQ 小黄脸，也会收藏你发的图。
- 不秒回：她拿起手机的第一条会先晾一会儿，忙的时候晾得更久；回之前你能看到"对方正在输入"。
- 你说了去忙、去睡之后只补一个表情，她不会再追一句。

**记忆**
- 最近 10 轮对话原样记得。
- 更早的聊天会写成日记，按遗忘曲线慢慢淡掉，被提起时又会想起来。
- 关于你的事、她自己说过的事、答应过要跟进的事、约好的见面，分别记着。

**状态**
- 六个维度的好感和十个关系阶段，决定她此刻跟你多亲近。
- 连续变化的心情，会被聊天内容影响，也会自己慢慢回到平常的样子。冷落她会不高兴。
- 自己的作息表和持续好几天的生活事件，所以她有的可聊，也有不方便的时候。

**主动**
- 按规则、心情和手头的素材决定要不要主动找你。你一直不回，她会降低频率直到不再发。

---

## 2. 这个项目在意什么

**说的和做的要对得上。** 她说了什么、系统实际发出去了什么、数据库里存了什么、下一轮模型读到什么，这四样必须一致。这是整条主链唯一的硬要求，后面的设计都是为它服务的。

**模型只负责写话。** 发不发语音、带不带表情、引用哪条消息、这一轮回不回，都由程序决定。模型不需要学任何标记，输出里出现标记会被当成无效回复。

**原话不改。** 模型写出来的话只生成一次，之后不润色、不裁剪、不重写，也不会让第二个模型来补救。

**角色和代码分开。** 换一个角色只需要换一张卡，不用改代码。

---

## 3. 一条消息怎么变成她的回复

```text
你发消息
  │
  ▼
聚合器         等你停手，把连发的几条合成一轮
  │
  ▼
先存你的话     之后哪一步出事，这句都不会丢
  │
  ▼
晾一会儿       只有她"拿起手机"的第一条才等，聊开了就不等
  │
  ▼
准备上下文     角色卡 + 此刻的心情和关系 + 记忆 + 最近 10 轮对话
  │            （要不要发语音在这里就定好，并作为事实告诉她）
  ▼
模型写一次     得到原稿，按换行拆成几条气泡
  │
  ▼
正在输入       按字数演一会儿；同时由系统决定要不要补一个表情
  │
  ▼
逐条发送       每条都等 QQ 的回执
  │
  ▼
只存发到了的   被拒绝就停在那里；不确定有没有发到就不重发
  │
  ▼
事后结算       另一个模型在后台更新好感、心情、记事，不拖慢回复
```

几个值得知道的细节：

- **发到一半停机**：已经送达的那几条照样会存下来。
- **模型出错**：发一句固定的兜底短句（比如"你刚才说什么？再说一次"），不会换个模型重来。
- **语音**：哪一句是用语音发的、哪次没发出去，会作为一条投递记录跟在那一轮对话后面。她下一轮读得到，所以知道刚才那条语音你到底有没有收到。
- **你发的表情**：小黄脸会带上一句意思再给她看，免得她把"吃糖"当成你真在吃糖。
- **她发的表情**：她写完话之后，由一次很小的后台调用决定要不要顺手补一个，跟在最后一条后面。她的原话不会因此改动；最近几轮发过就不再发，所以只是偶尔出现。可以用 `[reply].expressions = false` 关掉。

更完整的机制说明见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

---

## 4. 安装

需要 Python 3.10 以上，以及用来跑 NapCat 的 Docker。

```bash
git clone <你的仓库地址>
cd <仓库目录>

python3 -m venv venv
source venv/bin/activate        # Windows: .\venv\Scripts\activate

pip install -r requirements.txt
```

---

## 5. 部署 NapCat

NapCat 负责登录 QQ 小号，并通过 OneBot v11 WebSocket 和机器人通信。

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

然后：

1. 浏览器打开 `http://<服务器地址>:6099/webui`；
2. 用机器人的 QQ 小号扫码登录；
3. 在网络配置里启用 **WebSocket 服务端**，端口 `3001`，访问 Token 设成一串随机字符。

---

## 6. 配置与启动

复制配置模板再编辑：

```bash
cp config.example.toml config.toml
```

最少要填这些：

```toml
[account]
allowed_user_id = 123456789        # 你自己的 QQ 号，她只回这个人
bot_qq = 987654321                 # 机器人小号

[onebot]
ws_url = "ws://127.0.0.1:3001"
access_token = "和 NapCat 里设的 Token 一致"

[llm]
current = "deepseek"               # 用哪一家模型，对应下面的 [models.xxx]

[models.deepseek]
provider = "deepseek"
base_url = "https://api.deepseek.com"
api_key = "sk-xxxxxxxx"
chat = "deepseek-flash"            # 主聊
vision = "deepseek-flash"          # 看图，留空则不看图
tasks = "deepseek-flash"           # 后台结算、日记等

[character]
path = "characters/example"        # 用哪张角色卡
```

其余选项在 `config.example.toml` 里都有注释。语音默认关闭，要用的话把 `[tts].enabled` 改成 `true`；想用火山引擎的音色，开通步骤见 [`docs/VOLCENGINE_SETUP.md`](docs/VOLCENGINE_SETUP.md)。

启动：

```bash
python -m companion.main
```

看到 OneBot 连接成功后，用你的 QQ 给小号发一句话试试。

---

## 7. 写你自己的角色卡

仓库自带一张公开的模板卡 `characters/example/`，复制一份改就行。

```text
characters/your_character/
├── character.json       # 人设、说话方式、关系阶段、作息、生活主线
└── stickers/
    ├── index.json       # 表情包索引
    └── *.png / *.jpg
```

- [`docs/CUSTOMIZE.md`](docs/CUSTOMIZE.md)：每个字段是干什么的；
- [`docs/CARD_CRAFT.md`](docs/CARD_CRAFT.md)：怎么写才不像 AI。

一条经验：模型主要是照着示范的样子说话，其次才是听规则。想让她怎么说，就把那样的句子写进示范；光写"不要怎样"效果有限，有时还会适得其反。

---

## 8. 看板

机器人自带一个网页看板，默认端口 `8080`，可以看好感走势、当前心情、她记得的事和费用。

看板默认只监听本机。在服务器上跑的话，从你的电脑开一条 SSH 隧道：

```bash
ssh -N -L 8080:127.0.0.1:8080 ubuntu@<你的服务器地址>
```

然后浏览器打开 `http://127.0.0.1:8080`。

---

## 9. 后台常驻运行

Linux 服务器上用 systemd：

```bash
sudo cp deploy/qq-companion.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qq-companion
```

- 看状态：`sudo systemctl status qq-companion`
- 看日志：`journalctl -u qq-companion -f`
- 重启：`sudo systemctl restart qq-companion`

升级前先备份代码目录和 `data/companion.db`。主对话只有一条链路，没有可以切回旧版的开关，出问题时靠恢复备份回退。

---

## 10. 出了问题怎么查

每一轮对话都会在 `data/turn_traces/` 里按天记一份 JSONL。同一个 `trace_id` 下面有：

- 发给模型的完整提示词；
- 她写的原稿；
- 实际发出去的每一条和 QQ 的回执。

想知道"她为什么这么说"就看提示词，想知道"到底发没发出去"就看投递结果。

另外两点：

- `model_raw` 里的 `served_model` 是接口实际返回的模型名。配置里写的可能只是个别名，厂商换了底层版本从这里看得出来。
- 这些文件不会自动清理，里面是完整的聊天内容。长期运行请定期删旧的，也别把它们发给别人。

---

## 11. 测试

```bash
pip install pytest anyio
python -m pytest -q tests
```

不需要任何私有文件就能跑，默认用 `characters/example/` 这张卡；依赖私有角色卡或档案的用例会自动跳过。想指定别的卡：

```bash
QQC_TEST_CARD=characters/example python -m pytest -q tests
```

主链的行为集中在 `tests/test_main_chain.py`：用真实的数据库和真实的投递器，只把模型和 QQ 换成脚本化的假件。改主链时先看它。

`scripts/smoke/` 下的脚本会调用真实的在线接口并产生费用，不要批量跑。

---

## 12. 隐私与安全

- `config.toml` 里有你的 QQ 号、OneBot Token 和模型密钥，已被 `.gitignore` 拦截，不要提交。
- `data/` 里是聊天记录、记忆和追踪文件，同样不入库。
- 你自己的角色卡默认也不入库，仓库只带 `characters/example/`。
- 不要把服务器地址、本机路径写进代码或文档。
- 她只回 `allowed_user_id` 这一个号，别人发的消息会被忽略。

---

## 13. 代码地图

```text
companion/
├── main.py                 # 入口：把各部分接起来，连接 QQ，负责真实发送
├── aggregator.py           # 等你说完，把连发的消息合成一轮
├── onebot.py               # 和 NapCat 的 WebSocket 通信
├── kernel/                 # 主对话
│   ├── production.py       # 一轮的全过程：先存输入、生成、投递、入库
│   ├── kernel.py           # 准备上下文并调用模型一次
│   ├── context.py          # 提示词怎么拼
│   ├── semantic.py         # 把输入整理成自然的话；原稿拆气泡
│   ├── delivery.py         # 逐条发送，只认回执
│   ├── expression.py       # 系统替她挑表情
│   ├── memory_view.py      # 每轮带进提示词的记忆
│   ├── psychology.py       # 每轮的心情与关系快照
│   ├── contracts.py        # 上面这些模块之间传递的数据结构
│   └── tracing.py          # 轮次追踪
├── turn_runtime.py         # 语音闸门、作息、首条延迟与"正在输入"
├── voice_planner.py        # 什么算"要语音"，没要的时候挑哪句念
├── tts.py                  # 语音合成与每日上限
├── observer.py             # 事后结算：好感、心情、记事、见面
├── memory.py               # 聊天记录、日记、事实、待跟进
├── affection.py / mood.py  # 好感与心情的计算
├── proactive.py            # 主动消息的调度
├── arcs.py                 # 她自己的生活事件
├── meetups.py              # 约好的见面与事后纪要
├── persona.py / lorebook.py# 角色卡与世界书的读取
├── stickers.py / faces.py  # 表情包与 QQ 小黄脸
├── runtime_state.py        # 真实动作与语音结果的账本
├── admin.py / dashboard*.py# 看板与管理页
├── backup.py / reset.py    # 每日备份与数据重置
└── replier.py / assembler.py / prompts.py / chat.py
                            # 早期流水线留下的模块，仍被后台任务和工具复用
```

---

## 14. 更多文档

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)：原理篇，讲记忆、心情、关系、主动消息这些机制是怎么算的。
- [`docs/DEPLOY.md`](docs/DEPLOY.md)：完整的部署手册。
- [`docs/DEPLOY_FOR_AI.md`](docs/DEPLOY_FOR_AI.md)：写给 AI 助手的部署说明书。
- [`docs/CUSTOMIZE.md`](docs/CUSTOMIZE.md) 与 [`docs/CARD_CRAFT.md`](docs/CARD_CRAFT.md)：角色卡。
- [`docs/RELEASE_NOTES_v2.2.0.md`](docs/RELEASE_NOTES_v2.2.0.md)：这一版改了什么、升级要注意什么。
- [`docs/PLAN.md`](docs/PLAN.md) 与 [`docs/REFACTOR.md`](docs/REFACTOR.md)：早期的设计蓝图和重构任务书，属于历史记录，细节可能已经和现在的代码不一致。

License：MIT。
