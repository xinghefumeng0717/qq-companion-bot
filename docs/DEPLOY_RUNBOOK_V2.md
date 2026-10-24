# 总部署 Runbook V2（第二轮大版本上线 + 第四次 reset）

> 这份手册是给**所有者本人**照着一条条执行的：每一步都写清了"在哪儿执行、命令是什么、成功长什么样、不对怎么办"。
> 命令都是真实值（IP `SERVER_IP`、路径 `/opt/qq-companion`），可以直接复制粘贴，不需要改任何东西。
> **出任何报错：把命令和完整输出一起原样贴回，不要自己猜、不要自己改代码。** 有破坏性的步骤（清理 / 清档 / 还原）出错时先停下来问，不要重跑。
>
> 本手册吸收了三份材料：`data/deep_audit/面CD_部署与遗留.md`（C-1 升级路径 / C-3 新依赖与清档 / C-4 sticker 25 行清理 / C-11 回滚 / C-13 验证序列）、`docs/DEPLOY.md`（6.1 表情版本闸、8.1 输入状态极性实测）、`STATUS.md` 第 6 节"明天接班包"②。

---

## 0. 所有者已拍板的决策（本手册所有步骤的依据，执行中不许翻案）

1. **一次性总部署，不拆批**：本地囤积的 FIXES12~17、19~24 全部代码 + V3 角色卡，一次推上服务器（服务器现在停在 FIXES11 时代）。
2. **第四次 reset 与本次部署绑定执行**：FIXES8 之后阈值表重映射、FIXES16 生活主线等新机制都要"从初识开始"才准，清档零迁移成本。
3. **服务器 `config.toml` 零改动上线**：`[timing]`、`[tts]`、`thinking_tasks_purposes` 全部靠代码默认值兜底；TTS 默认关 = **装死上线**（音色还没放行，且开 TTS 前有必修项）。**任何一步都不许把本地 `config.toml` 传到服务器覆盖。**
4. **V3 角色卡随本次部署同步**（含 FIXES18 S1 追加的 2 条规则 + 2 条反面教材）：所有者 2026-10-04 晚已当面确认，走"本地定稿 → 所有者确认 → 同步"的红线流程。
5. **服务器 `stickers` 表清理 25 行**（13 张真删除 + 12 个改名旧名行），**不是 15 行**；顺序固定为：备份 → 白名单清理 → 重启补插 → 回读 `58 → 33 → 43`。
6. **reset 不加 `--purge-all`**：只清关系数据（对话/好感度/日记/生活主线），表情包库与计费历史保留。
7. **部署日三项实测必做**：NapCat 表情版本闸（阶段 G）、输入状态极性实测（阶段 I）、sticker 回读 43（阶段 H）。
8. **reset 后 40 分钟内【她最近的生活】为空属正常**，别误判功能没上（生活主线要等第一个主动消息周期或凌晨 04:17 钩子）。
9. **挑时段**：全程机器人离线 30~60 分钟，选"他不需要她回话"的清醒时段做。

---

## 1. 阶段总览

| 阶段 | 在哪儿做 | 干什么 | 预计 |
|---|---|---|---|
| 前夜检查表 | 本地 | 只读核对 6 项，不动服务器 | 10 分钟 |
| A | 本地 | 打 tag + 卡目录快照 | 2 分钟 |
| B | 服务器 | 停服 + 回滚三件套备份 | 5 分钟 |
| C | 本地→服务器 | 打包 → 传到 /tmp → 解包（先传后挪） | 10 分钟 |
| D | 服务器 | 装依赖（新增 edge-tts） | 3 分钟 |
| E | 服务器 | 核对 config.toml（只读为主） | 5 分钟 |
| F | 服务器 | stickers 表 25 行清理 | 3 分钟 |
| G | 服务器 | NapCat 表情版本闸（DEPLOY 6.1） | 5 分钟 |
| H | 服务器+本地 | 启动服务 + 启动验证（含看板/计费/sticker 回读 43） | 15 分钟 |
| I | 服务器+手机 | 输入状态极性实测（DEPLOY 8.1） | 10 分钟 |
| J | 服务器 | 生活主线观察 | 0~40 分钟（可并行摸鱼等） |
| K | 服务器 | 第四次 reset | 3 分钟 |
| L | 服务器+本地 | reset 后验证 | 10 分钟 |
| 回滚预案 | — | 出事才用 | — |

> 建议切成两段做：**第一段 A~G**（服务处于停机状态，做完可以喘口气）；**第二段 H~L**（要跟手机互动）。

---

## 2. 部署前夜检查表（全部只读，本地，约 10 分钟）

五道检查（2.1~2.5）全过才准开工；任何一项不对，先停下来说，别硬上。最后一条 2.6 是开工时要记下的信息。

### 2.1 工作区干净、HEAD 正确

```powershell
# 【本地电脑执行】
cd "D:\qq-companion"
git status --short
git log -1 --oneline
```

- 期望：`git status --short` **没有输出**（工作区干净）；`git log -1` 是 `327c24c 封线：明天接班包…`。
- 不对：有未提交改动 → 先告诉我是哪些文件，别自己 commit。

### 2.2 本地测试全绿

```powershell
# 【本地电脑执行】约 1 分钟
cd "D:\qq-companion"
.\venv\Scripts\python.exe -m unittest discover -s tests
```

- 期望：最后两行是 `Ran 910 tests in ...s` 和 `OK`。
- 不对：把红色报错原文贴回。

### 2.3 V3 角色卡自检（43 键、图片齐全、无空栏）

```powershell
# 【本地电脑执行】
cd "D:\qq-companion"
.\venv\Scripts\python.exe -c "import json,os;d='characters/example/stickers';idx=json.load(open(d+'/index.json',encoding='utf-8'));print('index 键数:',len(idx));print('引用但缺图片:',[k for k,v in idx.items() if not os.path.exists(os.path.join(d,v.get('file','')))] or '无');print('缺 meaning/usage:',[k for k,v in idx.items() if not v.get('meaning') or not v.get('usage')] or '无')"
```

- 期望：`index 键数: 43`、`引用但缺图片: 无`、`缺 meaning/usage: 无`。
- 不对：把输出贴回（这张卡是红线文件，不经确认不许改）。

### 2.4 免密登录 + 服务器现状（只读）

```powershell
# 【本地电脑执行】
ssh ubuntu@SERVER_IP "echo 免密OK; uptime; df -h /opt | tail -1; docker ps --format '{{.Names}}  {{.Status}}'; ls /opt/qq-companion/companion/arcs.py 2>/dev/null || echo '服务器无 arcs.py（FIXES11 时代，符合预期）'"
```

- 期望：出现 `免密OK`；NapCat 容器在 `Up`；磁盘 `/opt` 剩余 ≥ 2G；最后一行是 `服务器无 arcs.py（FIXES11 时代，符合预期）`。
- 不对：
  - 免密失败 → 先解决登录，别继续；
  - NapCat 没在跑 → 先在服务器执行 `docker start napcat`，再看 `docker ps`。

### 2.5 服务器 config.toml 现状（只看，不动）

```powershell
# 【本地电脑执行】
ssh ubuntu@SERVER_IP "grep -n 'thinking_tasks\|holidays' /opt/qq-companion/config.toml"
```

- 期望：能看到 `thinking_tasks = false` 和一行 `holidays = [...]`（应含 2026 国庆 8 天）。
- 若**没有** `thinking_tasks_purposes` 那一行：正常，代码默认值自带 `life_arc`，本次不需要填。
- 若**有**那一行且里面没有 `life_arc`：到阶段 E 再处理（那里有就地编辑的完整指引）。
- 提醒：这是**看**，不是**改**。服务器的 config.toml 里存着真实 QQ 号、OneBot token、API Key，**覆盖即瘫痪**。

### 2.6 记下开工信息

- 开始时间：`____:____`
- 本地 tar 包名：`qq-companion-v2-20261005.tar.gz`（放在 `D:\` 根目录）
- 卡快照：`D:\qq-companion\data\backups\characters-qingzi-v3-20261005.tar.gz`
- 卡内 `index.json` 的 SHA256（阶段 C 会用来对账）：`70AF696F1A1E3EFAA1CFBE1AD8082E31E7FE0DE66A8BECDD6AE2C49E19C07628`

---

## 3. 阶段 A：打 tag + 本地留底【本地电脑执行】

### A1 打部署 tag（回滚时的版本锚）

```powershell
# 【本地电脑执行】
cd "D:\qq-companion"
git tag -a v2-deploy-20261005 -m "第二轮 V2 总部署基线（FIXES12~24 + V3 卡，HEAD 327c24c）"
git tag --list "v2-deploy*"
```

- 期望：打印出 `v2-deploy-20261005`。
- 说明：这个 tag 是"部署前那一刻的代码长什么样"的锚点。服务器上大概率没有 `.git`（当年是打包传的），所以回滚主要靠阶段 B 的备份包，tag 是本地保险。

### A2 角色卡整目录快照（V3 卡 + 43 键索引 + 所有图片）

```powershell
# 【本地电脑执行】
cd "D:\qq-companion\data\backups"
tar -czf "characters-qingzi-v3-20261005.tar.gz" -C "D:\qq-companion\characters" qingzi
(Get-Item "characters-qingzi-v3-20261005.tar.gz").Length
tar -tzf "characters-qingzi-v3-20261005.tar.gz" | Select-Object -First 3
```

- 期望：文件大小约 1800 万字节（18MB 左右）；能列出 `qingzi/` 开头的条目。
- 说明：这张卡不在 git 里（`.gitignore` 排除），一旦服务器上被覆盖又没有备份，就只剩本地这一份——所以必须先留底。
- 顺带记一下索引指纹：

```powershell
# 【本地电脑执行】
(Get-FileHash "D:\qq-companion\characters\example\stickers\index.json" -Algorithm SHA256).Hash
```

期望值应与 2.6 记的 `70AF696F…7628` 一致。

---

## 4. 阶段 B：停服 + 回滚三件套备份【服务器终端执行】

### B1 停服，并趁机看一次"优雅停机"

```bash
# 【服务器终端执行】
sudo systemctl stop qq-companion
sleep 3
sudo systemctl is-active qq-companion
tail -n 15 /opt/qq-companion/data/logs/bot.log
```

- 期望：`is-active` 输出 `inactive`；日志尾部能看到**一整串**：

  ```
  [Bot] 正在关闭伴侣机器人...
  [Bot] 正在取消后台任务
  [Bot] 正在关闭 后台调度器 (aggregator/proactive/backup)
  [Bot] 正在关闭 OneBot 客户端
  [Bot] 正在关闭 Admin 仪表盘
  [Bot] 正在关闭 LLM 网关
  [Bot] 正在关闭 数据库
  [Bot] 关闭完成
  ```

  （这是管道③修的停机顺序，本轮是第一次有机会看到完整序列。）
- 不对：只看到第一行就没了 → **不影响本次部署**，继续往下，部署结束时把这段日志贴回，那属于要另立项排查的老账。
- 为什么先停服：备份数据库、清理 stickers 表都必须"没有进程在写库"，否则备份可能是残缺的。

### B2 回滚三件套（代码+角色卡 / 数据库 / 配置）

```bash
# 【服务器终端执行】
cd /opt
sudo tar --exclude='qq-companion/venv' --exclude='qq-companion/data' --exclude='qq-companion/napcat-data' --exclude='qq-companion/napcat-cache' -czf /home/ubuntu/qqc-backup-code-and-card-20261005.tar.gz qq-companion
sudo cp /opt/qq-companion/data/companion.db /home/ubuntu/qqc-backup-companion-db-20261005.db
sudo cp /opt/qq-companion/config.toml /home/ubuntu/qqc-backup-config-toml-20261005.toml
sudo chown ubuntu:ubuntu /home/ubuntu/qqc-backup-code-and-card-20261005.tar.gz /home/ubuntu/qqc-backup-companion-db-20261005.db /home/ubuntu/qqc-backup-config-toml-20261005.toml
ls -lh /home/ubuntu/qqc-backup-*
```

- 期望：三件都在，且大小合理（tar 约几十 MB；db 几百 KB~几 MB；toml 几 KB）。
- 说明：
  - 第一件是"代码 + `characters/`（角色卡目录）"的打包，排除掉体积大且回滚用不到的 venv、NapCat 数据、以及 `data/`（数据库单独备份，日志不用回滚）；**回滚代码和回滚卡都用它**；
  - 第二件是数据库**停止写入状态下**的干净副本（单独一件，这样"只回代码"时不会顺手把新对话冲掉）；
  - 第三件是配置副本（只用于"万一被误改"的核对与还原，正常情况永远用不上）。

### B3 记下清理前的 sticker 行数（留作阶段 F 的对账）

```bash
# 【服务器终端执行】
cd /opt/qq-companion
./venv/bin/python -c "import sqlite3;c=sqlite3.connect('data/companion.db');print('部署前 stickers 行数:', c.execute('SELECT COUNT(*) FROM stickers').fetchone()[0])"
```

- 期望：`部署前 stickers 行数: 58`。
- 不是 58 也没关系：阶段 F 的清理脚本是"白名单式"的，不依赖这个数；但请把真实数字记下来。

---

## 5. 阶段 C：传代码（先传后挪）【本地电脑执行 → 服务器终端执行】

### C1 本地打包（魔鬼在 excludes：`config.toml` 绝不能进包）

```powershell
# 【本地电脑执行】
cd "D:\"
tar -czf "qq-companion-v2-20261005.tar.gz" --exclude="QQ chatter/venv" --exclude="QQ chatter/data" --exclude="QQ chatter/.git" --exclude="QQ chatter/config.toml" --exclude="*/__pycache__" --exclude="*.pyc" "QQ chatter"
(Get-Item "qq-companion-v2-20261005.tar.gz").Length
```

- 期望：大小约 1900 万字节（19MB 左右）。
- 为什么排除这些：
  - `venv`、`data`、`.git` —— 服务器上已有各自的一份，不需要也不该被替换；
  - **`config.toml` —— 高压红线**：服务器那份填着真实账号与节假日，本机这份是空壳（`holidays = []`），传上去会**静默清空**服务器已填的 2026 国庆 8 天（计费按高峰算、作息把长假当上课日），账号/token 不一致时还会直接登不上。

### C2 打包自检（这一步是保险丝，别跳过）

```powershell
# 【本地电脑执行】检查 1：包里绝不允许出现 config.toml
cd "D:\"
tar -tzf "qq-companion-v2-20261005.tar.gz" | Select-String -Pattern "QQ chatter/config.toml"
```

- 期望：**没有任何输出**。只要有输出 → 停，把输出贴回。
- 检查 2：

```powershell
# 【本地电脑执行】检查 2：新模块与新卡都在包里
cd "D:\"
tar -tzf "qq-companion-v2-20261005.tar.gz" | Select-String -Pattern "companion/(arcs|faces|tts)\.py|characters/example/stickers/index.json"
```

- 期望：至少 4 行（`arcs.py`、`faces.py`、`tts.py`、`index.json`）。

### C3 上传（先传后挪第一步：只进 /tmp）

```powershell
# 【本地电脑执行】先在服务器建好接收目录（不假定 /tmp 已有东西）
ssh ubuntu@SERVER_IP "mkdir -p /tmp/qqc-deploy-20261005 && rm -f /tmp/qqc-deploy-20261005/*.tar.gz"
cd "D:\"
scp "qq-companion-v2-20261005.tar.gz" ubuntu@SERVER_IP:/tmp/qqc-deploy-20261005/
ssh ubuntu@SERVER_IP "ls -lh /tmp/qqc-deploy-20261005/"
```

- 期望：看到 `qq-companion-v2-20261005.tar.gz`，约 19MB。
- 说明：先把包放进 `/tmp`（临时区），确认真到了、大小对，再在服务器上"挪"进部署目录——这就是"先传后挪"，中间任何一步失败都不会污染正在运行的部署目录。

### C4 服务器解包（先传后挪第二步：挪进部署路径 + 修属主）

```bash
# 【服务器终端执行】
sudo tar -xzf /tmp/qqc-deploy-20261005/qq-companion-v2-20261005.tar.gz -C /opt/qq-companion --strip-components=1
sudo chown -R ubuntu:ubuntu /opt/qq-companion
```

- `--strip-components=1` 的作用：包里第一层目录叫 `QQ chatter/`，去掉它，文件才会落到 `/opt/qq-companion/companion/...` 而不是 `/opt/qq-companion/QQ chatter/...`。
- `chown` 的作用：解包是 root 做的，不修属主的话机器人（ubuntu 用户）可能写不了库和日志。

### C5 解包后三连验

```bash
# 【服务器终端执行】
cd /opt/qq-companion
ls -l companion/arcs.py companion/faces.py companion/tts.py
./venv/bin/python -c "import json;print('卡内 index 键数:', len(json.load(open('characters/example/stickers/index.json',encoding='utf-8'))))"
diff /home/ubuntu/qqc-backup-config-toml-20261005.toml /opt/qq-companion/config.toml && echo "config.toml 未被改动（与备份逐字节一致）"
```

- 期望：三个新文件都在；`卡内 index 键数: 43`；最后一行打印"config.toml 未被改动（与备份逐字节一致）"。
- 不对：
  - 键数不是 43 → 包里带错了卡，停下贴回；
  - `diff` 有输出（列出差异行）→ **说明 config.toml 被动了**，立刻执行下面这条只读核对，把输出贴回，先别继续：

    ```bash
    # 【服务器终端执行】只读核对，不做改动
    grep -n "thinking_tasks\|holidays" /opt/qq-companion/config.toml
    ```

- 附注：解包**不会删除**服务器上本地已不存在的旧文件（例如 `scripts/` 根目录下几个旧脚本、几张已下线的表情图），这些残留无害，不用管。

---

## 6. 阶段 D：装依赖【服务器终端执行】

```bash
# 【服务器终端执行】
cd /opt/qq-companion
./venv/bin/pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
./venv/bin/pip show edge-tts | head -2
./venv/bin/python -c "import aiohttp, aiosqlite, PIL, edge_tts; import companion.main, companion.arcs, companion.faces, companion.tts; print('依赖与新模块导入全部 OK')"
```

- 期望：
  - pip 出现 `Successfully installed ...`（若全是 `Requirement already satisfied` 也正常）；
  - `edge-tts` 版本显示 `7.2.8`；
  - 最后一行打印 `依赖与新模块导入全部 OK`。
- 为什么要装：本轮新增了 `edge-tts`（TTS 语音）。**即使 TTS 是关着的也必须装**——它是延迟加载 + 静默降级的设计，缺了不会报错，正好会陷入"看起来部署成功、实际少装一件"的盲区。
- 不对：pip 网络超时 → 原样重跑一次；仍失败把报错贴回（阿里云镜像偶尔抽风，不要自己换源）。

---

## 7. 阶段 E：核对服务器 config.toml（只读为主）【服务器终端执行】

### E1 用新代码把服务器那份配置真实读一遍

```bash
# 【服务器终端执行】只读，不改任何文件
cd /opt/qq-companion
./venv/bin/python - <<'PY'
import os
from companion.config import Config
p = "/opt/qq-companion/config.toml"
assert os.path.exists(p), "config.toml 不存在！停下来别继续"
c = Config.load(p)
print("后台总开关 thinking_tasks =", c.llm.thinking_tasks)
print("按用途思考白名单          =", c.llm.thinking_tasks_purposes)
print("life_arc 在白名单内        =", "life_arc" in c.llm.thinking_tasks_purposes)
print("节假日 holidays            =", c.get_holidays())
print("tts.enabled（应为 False）  =", c.tts.enabled)
print("首条延迟开关 timing        =", c.timing.timing_enabled)
print("正在输入表演 typing        =", c.timing.typing_indicator_enabled)
print("免打扰时段 / 唤醒间隔(分)  =", c.proactive.quiet_hours, "/", c.proactive.wake_interval_min, "~", c.proactive.wake_interval_max)
PY
```

逐行判读：

| 输出行 | 期望 | 不符合怎么办 |
|---|---|---|
| `life_arc 在白名单内` | `True` | 见 E2 |
| `tts.enabled` | `False` | 若是 `True` → 见 E3（本阶段下线 TTS） |
| `holidays` | 含 2026-10-01 ~ 10-08 共 8 天 | 若为空或不全 → 见 E4 |
| `thinking_tasks` | `False` | 若是 `True` → 属配置变更（全用途开思考，会涨成本），**先别动**，把整段输出贴回 |
| `timing` / `typing` | 都 `True` | 都是新功能默认开，符合预期 |
| `免打扰时段` | `[0, 8]`、`20 ~ 40` | 与现状一致，不用动 |

> 这一段的意义：它不是"看一眼配置文本"，而是**让新代码自己告诉你它会怎么理解这份配置**——"零改动上线"这句声明的真实证据就在这里。

### E2 若 `life_arc 在白名单内 = False`（在服务器上就地编辑）

```bash
# 【服务器终端执行】用 nano 就地编辑（就地！不是上传本机文件覆盖）
nano /opt/qq-companion/config.toml
```

nano 操作步骤（假设零经验）：

1. 用**方向键**移动到 `thinking_tasks_purposes = [...]` 那一行（找不到这一行 → 停下，把 E1 的输出贴回，不要自己加）；
2. 把整行改成下面这样，一个字符都不要漏：

   ```
   thinking_tasks_purposes = ["diary_archive", "vision_perception", "proactive_message", "life_arc"]
   ```

3. 按 `Ctrl` 和字母 `O`（保存），屏幕下方出现文件名 prompt，直接按 **回车** 确认；
4. 按 `Ctrl` 和字母 `X`（退出），回到命令行；
5. **重跑 E1 那段脚本**，确认 `life_arc 在白名单内 = True`。

### E3 若 `tts.enabled = True`

它是新功能，本轮必须**关着上线**（音色还没放行，且激活前有必修项）。

```bash
# 【服务器终端执行】
nano /opt/qq-companion/config.toml
```

找到 `[tts]` 段里的 `enabled = true`，改成 `enabled = false`；`Ctrl+O` 回车 → `Ctrl+X` 退出；重跑 E1 确认。

（若根本没有 `[tts]` 段：那就更好，代码默认就是关的，**不要**新增这一段。）

### E4 若 `holidays` 为空或不全

```bash
# 【服务器终端执行】
nano /opt/qq-companion/config.toml
```

找到 `[llm.pricing]` 段下的 `holidays = [...]`，按这个格式补齐 2026 国庆 8 天（用英文双引号、逗号分隔）：

```
holidays = ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"]
```

`Ctrl+O` 回车 → `Ctrl+X`，重跑 E1 确认。

填表规则（重要，别当"可选"）：

- 它不只是算钱用的，还是**行为侧的唯一数据源**：作息表、长假锚点（"回绍兴老家"）、计费峰谷都读它；
- 长假判定要求**连续 ≥ 4 天**，零散填几天等于长假功能没上；
- 年例：**每年 12 月底要把第二年的元旦 + 春节补进去**（2026 年内不用补，国庆是最后一段）。

> 红线再念一遍：本手册**从头到尾不提供任何"上传/覆盖服务器 config.toml"的命令**。要改配置就在服务器上用 nano 改。

---

## 8. 阶段 F：stickers 表 25 行清理【服务器终端执行】

> 背景（人话版）：这次表情包换代，本地索引从 58 键变成 43 键——13 张真删了、12 个改了名。数据库表还是旧的 58 行，它虽然不参与提示词，但参与"200 张上限计数"和"同名图片去重"，不清就会长期少算/多算。服务现在停着，正是清理的窗口。
> 顺序**必须是**：备份 → 白名单清理 → 重启补插。反了（先重启后清理）会把补进去的新键名又删掉。

### F1 先备份数据库（服务停着，这份备份干净）

```bash
# 【服务器终端执行】
mkdir -p /opt/qq-companion/data/backup
cp /opt/qq-companion/data/companion.db /opt/qq-companion/data/backup/companion-before-sticker-cleanup-20261005.db
ls -lh /opt/qq-companion/data/backup/companion-before-sticker-cleanup-20261005.db
```

### F2 白名单清理 + 当场回读

```bash
# 【服务器终端执行】
cd /opt/qq-companion && ./venv/bin/python - <<'PY'
import sqlite3, json
db = "/opt/qq-companion/data/companion.db"
keep = list(json.load(open("/opt/qq-companion/characters/example/stickers/index.json", encoding="utf-8")))
q = ",".join("?" * len(keep))
c = sqlite3.connect(db); cur = c.cursor()
cur.execute("SELECT COUNT(*) FROM stickers"); print("清理前:", cur.fetchone()[0])
cur.execute(f"SELECT name FROM stickers WHERE name NOT IN ({q}) ORDER BY name", keep)
stale = [r[0] for r in cur.fetchall()]; print("孤儿行:", len(stale), stale)
cur.execute(f"DELETE FROM stickers WHERE name NOT IN ({q})", keep)
c.commit()
cur.execute("SELECT COUNT(*) FROM stickers"); print("清理后:", cur.fetchone()[0]); c.close()
PY
```

期望的三段输出（**照这个对**）：

```
清理前: 58
孤儿行: 25 ['你不要过来呀', '偷笑', '吃东西', '吃惊', '听我说谢谢你', '咖波欢呼', '干饭', '惊讶猫猫', '打瞌睡', '收到', '敲桌子', '没生气哦', '派蒙加油', '派蒙无语', '示例大学菲比', '绫华优雅', '绫华微笑', '胡桃偷看', '菲比探头', '菲比欢呼', '蓝发厨娘', '让我康康', '赞', '震惊猫猫', '馋了']
清理后: 33
```

- 对账看**数字**（58 / 25 / 33）；括号里的名字排序可能与本例略有出入（按字节排序），**个数必须是 25**。
- **预期名单说明**：25 个名字 = 13 张真删除 + 12 个改名后的旧名。列表里出现好看的名字（如"示例大学菲比""干饭"）是因为它们对应的**键**被删了；**图片文件一张都没删**，随时可加回。
- 数字对不上时：脚本是"照新索引 43 键当白名单、只删不在名单里的名字"，逻辑上不可能多删——若三个数字与预期不同，把**完整输出原样贴回**（含命令本身），不要自己重试或手写 SQL。
- 这一步做对了才继续；**现在绝对不要重启服务**（下一阶段 G 做完再一起启）。

---

## 9. 阶段 G：NapCat 表情版本闸（DEPLOY 6.1，必做）【服务器终端执行】

> 为什么必须做：NapCat 发一条表情段时，如果这个表情 id 不在**它自己那份** `face_config.json` 里，会**静默丢弃**——不报错、不重试、消息照发，症状是"她明明要发表情，屏幕上什么都没有"（NapCat issue #1987）。本地清单是照 NapCat 2026-10-04 main 分支核过的，服务器镜像是 `latest`，**以服务器那份为准**。

### G1 找到容器里的表

```bash
# 【服务器终端执行】
cd /opt/qq-companion
docker exec napcat sh -c "ls /app/napcat/core/external/face_config.json 2>/dev/null || find / -name face_config.json -path '*napcat*' 2>/dev/null | head -1"
```

- 有输出路径 → 记下它（下面命令里的路径照抄，如果就是 `/app/napcat/core/external/face_config.json` 则不用改）；
- **没有任何输出** → 这个 NapCat 版本没有这份表：记一行"未找到表，跳过本阶段"，直接进阶段 H（不会崩，只是个别 id 会被 QQ 侧丢掉）。

### G2 与本地 26 项清单对账

```bash
# 【服务器终端执行】路径用 G1 打印出来的那个；若就是下面这个则不用改
docker exec napcat cat /app/napcat/core/external/face_config.json > /tmp/server_face_config.json
cd /opt/qq-companion && ./venv/bin/python - <<'PY'
import json, sys
sys.path.insert(0, "/opt/qq-companion")
from companion.prompts import PROMPT_FACE_LIST
raw = json.load(open("/tmp/server_face_config.json", encoding="utf-8"))
srv = {str(x.get("QDes", "")).lstrip("/") for x in raw.get("sysface", [])}
bad = [n for n, _ in PROMPT_FACE_LIST if n not in srv]
print("服务器表 sysface 条数:", len(raw.get("sysface", [])))
print("本地清单条数:", len(PROMPT_FACE_LIST))
print("这台机器发不出的标签:", bad or "无（全部可发）")
PY
```

- 输出 `这台机器发不出的标签: 无（全部可发）` → 直接进阶段 H，什么都不用改（这是最可能的结果）；
- 输出里**有**标签 → **先别改代码**：把这三行输出贴回给我。原因：`tests/test_fixes20.py` 把清单长度钉在 25~29 之间，逐个剔除有可能把清单剔到 24 导致本地测试变红，需要同时放宽阈值，这一步由我先判断"当场剔"还是"观察期再定"。
  （若我让你当场剔：改 `companion/prompts.py` 后必须在服务器上跑 `cd /opt/qq-companion && ./venv/bin/python -m unittest discover -s tests` 确认全绿，再重启服务。）

---

## 10. 阶段 H：启动服务 + 启动验证【服务器终端执行 + 本地电脑执行】

### H1 启动

```bash
# 【服务器终端执行】
sudo systemctl start qq-companion
sleep 5
sudo systemctl status qq-companion --no-pager | head -12
docker ps --format '{{.Names}}  {{.Status}}'
```

- 期望：`active (running)`；napcat 在 `Up`。
- 不对：状态是 `activating (auto-restart)` 循环或 `failed` → 执行 `journalctl -u qq-companion -n 50 --no-pager`，把输出贴回。

### H2 看启动日志

```bash
# 【服务器终端执行】
tail -n 40 /opt/qq-companion/data/logs/bot.log
```

- 期望看到两行关键日志：
  - `[OneBot] 正在连接 OneBot 服务: ws://127.0.0.1:3001`
  - `[OneBot] WebSocket 连接成功！`
- 不对：反复 `Connection refused 3001` → `docker restart napcat`，等 10 秒再看；仍然失败见第 16 节。

### H3 真人发一条（他在手机 QQ 上操作）

- 大号给小号发一句"在吗"。
- **第一条可能要等一会儿**：FIXES15 给她加了"回复时机人格化"——她正在忙（上课/合练/睡觉）会等 1~10 分钟，闲着时等 5~30 秒；对话激活后秒回。**这是要观察的新行为，不是卡住。**
- 顺带观察"正在输入"：她真正发出前，手机 QQ 上可能显示"对方正在输入"。
- 日志侧：看到她回话前后出现 `[Aggregator] ... 提交一轮`。

### H4 看板与状态 API

```powershell
# 【本地电脑执行】开隧道（这个窗口保持开着别关）
ssh -L 8080:127.0.0.1:8080 ubuntu@SERVER_IP
```

浏览器打开 `http://localhost:8080`，几个页面（总览/记忆/调试/计费/表情包/日志/管理）都应能打开。

```powershell
# 【本地电脑执行】另开一个 PowerShell 窗口：状态 API 必须返回 JSON
ssh ubuntu@SERVER_IP "curl -s http://127.0.0.1:8080/api/status"
```

- 期望：`{"bot_alive":true,"onebot_connected":true,"stage_name":"...","composite":...,"today_cost":...,"last_backup_time":...,"uptime_minutes":...}`
- 不对：`onebot_connected` 是 `false` → 回到 H2 看日志。

### H5 数据核对（sticker 回读 43 / 新表 / 计费用途）——**必须在 reset 之前做**

```bash
# 【服务器终端执行】
cd /opt/qq-companion
./venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect("data/companion.db")
print("stickers 行数（应为 43）:", c.execute("SELECT COUNT(*) FROM stickers").fetchone()[0])
print("life_arcs 行数（此刻 0 属正常）:", c.execute("SELECT COUNT(*) FROM life_arcs").fetchone()[0])
print("计费按用途:", c.execute("SELECT purpose, COUNT(*) FROM llm_calls GROUP BY purpose ORDER BY 2 DESC").fetchall())
c.close()
PY
```

- `stickers 行数` **必须是 43**（路径：清理前 58 → 清理后 33 → 重启时 `sync_initial_stickers` 把 10 个新键名补进库 → 43）。
  - 还是 33 → 服务没起来 / index.json 不是 43 键版；查 `systemctl status` 和 C5 的键数；
- `life_arcs` 此刻 0 属正常（还没到生成时点，见阶段 J）；若这句直接报 `no such table: life_arcs` → 服务没真正起来（新表是启动时自动建的），回 H1 看状态与日志。
- 计费列表里现在可能还没有 `life_arc`，正常——它要等生活主线真的生成一次。

> **这一段是 reset 之前的最后一次"老数据仍可读"证据**。reset 会把旧数据清掉，之后就验不了了，所以顺序不能颠倒。

---

## 11. 阶段 I：输入状态极性实测（DEPLOY 8.1，必做）【服务器 + 手机】

> 为什么必须做：「他打字她等」依赖 NapCat 上报的对方输入状态。它是**纯增强**——收不到时行为与改动前一致，不会瘫痪；但也正因为收不到就静默降级，**光看"用起来正常"证明不了事件真的到了**。
> `event_type` 的极性（1 还是 0 表示"正在输入"）**没有权威文档**，代码现在按"1=正在输入"实现；取错的后果被两层兜底夹住（15 秒自愈 + 30 秒上限），最坏只是"等他"的时机对调。

### I1 第一步：抓一条真实事件原文

```bash
# 【服务器终端执行】这个窗口先开着
docker logs -f --tail 20 napcat 2>&1 | grep -A2 -i "输入状态\|input_status"
```

他操作：在手机 QQ 输入框里**慢慢打字但先不发送**，停 5 秒以上（要像真人打字）。

- 日志出现 `[Notice] [输入状态] ...` → 事件到了，进 I2；
- **一条都没有** → 这个 NapCat 版本/配置不上报该事件 → 功能静默不生效，**属预期降级，不用改代码**；在《部署记录》里记一行"本环境无该事件"，跳到阶段 J。

### I2 第二步：核对极性（事件到了才做）

```bash
# 【服务器终端执行】另开一个窗口
tail -f /opt/qq-companion/data/logs/bot.log | grep -E "对方开始输入|对方停止输入|输入状态自愈|他还在打字|他已停手"
```

他操作：在手机 QQ 输入框里打字、停 5 秒以上（别发出去）。

- 看到 `[OneBot] 对方开始输入，通知聚合器等他` → **极性正确**，不用动代码，进 I3；
- 看到的是"对方停止输入"，或者干脆没反应（真正发出去之后才冒出"对方开始输入"）→ **极性反了**，做下面三步：

  1. 就地编辑（不要整文件重传）：

     ```bash
     # 【服务器终端执行】
     nano /opt/qq-companion/companion/onebot.py
     ```

     nano 里按 `Ctrl` + `W`（搜索），输入 `INPUT_STATUS_TYPE` 后回车，会跳到常量定义处；把 `INPUT_STATUS_TYPE_ON` 与 `INPUT_STATUS_TYPE_OFF` 的**值对调**（即 ON 变成 0、OFF 变成 1）；
  2. `Ctrl+O` 回车保存 → `Ctrl+X` 退出；
  3. 重启并复测：

     ```bash
     # 【服务器终端执行】
     sudo systemctl restart qq-companion
     ```

     等 10 秒，重复 I2 的观察，直到看到"对方开始输入"。
- 小提示：改完重启时，`tail` 那一段旧日志会被新日志顶下去——重启后再打字一次即可。

### I3 第三步（可选）：看聚合器有没有真的"等他"

```bash
# 【服务器终端执行】沿用 I2 的窗口，或换成这条
tail -f /opt/qq-companion/data/logs/bot.log | grep "Aggregator"
```

他操作：连发 3 条，每条之间停 7 秒以上。

- 看到 `[Aggregator] 他还在打字，暂不插话` 且 3 条合成一轮（`提交一轮: 3 条`）→ 功能真在生效；
- 3 条被切成 3 轮 → 先回 I1 看输入状态事件到底有没有来。**如果事件只在"开始打字"时报一次**，被切成 3 轮属于 NapCat 的上报粒度问题（只在状态变化时报），不是极性错，记一行即可。
- 观察期指标：以后日志里 `他还在打字，暂不插话` 的出现频率**接近零**说明 NapCat 事件没到，该去查，而不是该庆幸。

---

## 12. 阶段 J：生活主线观察（最多等 40 分钟）【服务器终端执行】

> 这一节是"要等、要查日志"的一条。生活主线（她提过的审查、考试）在**第一个主动消息周期（20~40 分钟）**或凌晨 04:17 维护钩子里生成；生成失败只会打一行 `[Arcs]` 警告、不会报错，所以**不看日志就分不清"还没到点"和"链路坏了"**。

```bash
# 【服务器终端执行】查主线日志
grep "\[Arcs\]" /opt/qq-companion/data/logs/bot.log | tail -20
```

- 前 40 分钟内**一行都没有 → 正常**，先去干别的，晚点再查；
- 出现 `[Arcs] 本轮补充生活主线 N 条（生成候选 M 条）` → 生成成功；
- 出现 `[Arcs] 生活主线生成失败` / `[Arcs] 生成结果不是 arcs 列表，丢弃` → 链路有问题：把这一行 + 前后 20 行贴回。

40 分钟后核对（这条是硬指标）：

```bash
# 【服务器终端执行】
cd /opt/qq-companion
./venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect("data/companion.db")
print("life_arcs 行数:", c.execute("SELECT COUNT(*) FROM life_arcs").fetchone()[0])
print("life_arc 计费调用次数:", c.execute("SELECT COUNT(*) FROM llm_calls WHERE purpose='life_arc'").fetchone()[0])
c.close()
PY
```

- 期望：`life_arcs 行数` ≥ 1，`life_arc 计费调用次数` ≥ 1。
- 两个都是 0：
  1. 回阶段 E1 确认 `life_arc 在白名单内 = True`；
  2. 把日志里所有 `[Arcs]` 行贴回。
- 区块确认：看板"调试"页能看到最近一轮完整提示词，有主线时里面应出现【她最近的生活】区块；没有主线时整块省略，**不是 bug**。

---

## 13. 阶段 K：第四次 reset【服务器终端执行】

> 做之前心里过一遍：这一下**她的记忆、好感度、日记、生活主线全部清零**，重新回到"初识"。数据库会自动先备份一份。

### K1 停服（保证没有进程在写库）

```bash
# 【服务器终端执行】
sudo systemctl stop qq-companion
sleep 3
sudo systemctl is-active qq-companion
```

- 期望：`inactive`。

### K2 清档

```bash
# 【服务器终端执行】原样照抄，绝对不要加 --purge-all
cd /opt/qq-companion
./venv/bin/python -m companion.reset --yes
```

> ⚠ **不要加 `--purge-all`**：那会连表情包库（刚清好的 43 张）和计费历史一起清掉。
> 需要人工输入 `YES` 的交互确认已经被 `--yes` 跳过，命令会直接执行并自动备份。

期望输出（大意）：

```
✨ 数据重置成功！✨
📦 备份文件: data/backup/daily/companion-20261005-HHMMSS.db
🧹 已清空表及数据行数:
  • turns     : N 行已清除
  • diary     : N 行已清除
  ...（diary_archive / facts / followups / suppressed_desires / observer_scores / milestones / state / life_arcs）
🔄 计数器归零: ...
💡 好感度与情绪状态已清空，下次交互时将从角色卡默认初始值重建。
```

当场核对两点：

1. 备份文件真的在 `data/backup/daily/` 下（命令已经打印路径）；
2. 清空列表里**没有** `stickers`、**没有** `llm_calls`。

- 不对：若列表里出现了 `stickers` 行 → 说明用了 `--purge-all`，立刻停下，用 B2 的数据库副本还原（见第 15 节档 2），然后把输出贴回。

### K3 起服

```bash
# 【服务器终端执行】
sudo systemctl start qq-companion
sleep 5
sudo systemctl status qq-companion --no-pager | head -12
docker ps --format '{{.Names}}  {{.Status}}'
```

- 期望：`active (running)`；napcat 在 `Up`。

---

## 14. 阶段 L：reset 后验证【服务器 + 本地】

### L1 服务与 NapCat 都活着

```bash
# 【服务器终端执行】
sudo systemctl status qq-companion --no-pager | head -6
docker ps --format '{{.Names}}  {{.Status}}'
tail -n 30 /opt/qq-companion/data/logs/bot.log
```

- 成功：`active (running)`；napcat `Up`；日志里再次出现 `[OneBot] WebSocket 连接成功！`。
- 失败：`journalctl -u qq-companion -n 50 --no-pager`，**原文贴回**。

### L2 状态 API（本地电脑执行，隧道窗口保持开着）

```powershell
# 【本地电脑执行】
ssh ubuntu@SERVER_IP "curl -s http://127.0.0.1:8080/api/status"
```

- 成功：JSON 里 `stage_name` 是**初识**、`composite` 约 23~26（角色卡初始值）、`uptime_minutes` 只有几分钟。
- 失败：隧道断了（重开 `ssh -L 8080:127.0.0.1:8080 ubuntu@SERVER_IP`）或服务没起。

### L3 数据核对

```bash
# 【服务器终端执行】
cd /opt/qq-companion
./venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect("data/companion.db")
for t in ("turns", "diary", "diary_archive", "facts", "observer_scores", "life_arcs"):
    print(t, c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
print("stickers（必须还是 43）:", c.execute("SELECT COUNT(*) FROM stickers").fetchone()[0])
c.close()
PY
```

- 成功：关系表全是 0；`stickers` 仍是 **43**（reset 不动表情包库，这是"没加 --purge-all"的硬证据）。
- 失败：stickers 不是 43 → 用了 `--purge-all`，按第 15 节档 2 用 B2 的副本还原。

### L4 真人对话

他发一条消息。

- 成功：她回话（第一条可能延迟 5 秒~10 分钟，属正常）。
- 口吻应是阶段 1「初识」：不黏、不熟络；手机 QQ 上可能看到"正在输入"。
- 失败：完全没反应 → 看 `tail -n 50 /opt/qq-companion/data/logs/bot.log` 有没有消息进来（`[OneBot]` 事件行），贴回。

### L5【她最近的生活】为空 = 正常（别误判）

reset 清空了 `life_arcs` 表，【她最近的生活】区块与事件通道要等**第一个主动消息周期（20~40 分钟）**或凌晨 04:17 钩子才会重新出现。

- **reset 后 40 分钟内为空，属正常，不要当成 bug、不要改代码。**
- 40 分钟后再跑一次阶段 J 的命令，应看到新主线的日志。

### L6 收尾留痕

把这三样贴回给我（这就是本次交付的验收证据）：

1. 阶段 F 的三个数字：`清理前 58 / 孤儿行 25 / 清理后 33`，以及阶段 H5 的 `stickers 行数 43`；
2. 阶段 H4 的 `/api/status` JSON 原文；
3. reset 输出最后几行 + 阶段 L3 的表计数。

之后她就是一个全新的"初识"状态：第一天话少、互动浅是正常的（好感度要从 23 慢慢涨），别按"她变冷淡了"理解。

---

## 15. 回滚预案（三档，按症状选）

### 什么时候该回滚

| 症状 | 用哪一档 |
|---|---|
| 服务起不来、反复自动重启、`journalctl` 里有异常堆栈 | 档 1 |
| 她完全不回话，且日志有异常 | 档 1 |
| 服务正常但她口吻明显不对（你自己读着别扭） | 档 1（只回代码与卡） |
| 数据库报错、数据错乱 | 档 2 |
| 已经把 reset 执行了，但想回到"没有 reset 的那一世" | 档 2 |
| 怀疑 `config.toml` 被误改 | 档 3 |

回滚的底气来自阶段 B 的三件套备份，以及一个已核对过的事实：**本轮库结构改动对老代码全部前向兼容**（新表 `life_arcs`、新 state key、卡索引新字段都不会让老代码报错），所以整段代码回退不会因为库而炸。

### 档 1：只回代码 + 角色卡（数据保留）

```bash
# 【服务器终端执行】
sudo systemctl stop qq-companion
sudo tar -xzf /home/ubuntu/qqc-backup-code-and-card-20261005.tar.gz -C /opt --overwrite
sudo chown -R ubuntu:ubuntu /opt/qq-companion
sudo systemctl start qq-companion
```

- 覆盖后，版本较新的独有文件（`arcs.py` / `faces.py` / `tts.py`）还留在目录里，但老代码不会 import 它们，无害。
- 备份包里**不含 `data/`**，所以数据库、日志、备份文件都不受影响（部署后产生的对话保留）。
- 回滚后做一次阶段 H 的启动验证（H1/H2/H4）。

### 档 2：代码 + 角色卡 + 数据库（丢掉部署后产生的对话）

```bash
# 【服务器终端执行】
sudo systemctl stop qq-companion
sudo tar -xzf /home/ubuntu/qqc-backup-code-and-card-20261005.tar.gz -C /opt --overwrite
sudo cp /home/ubuntu/qqc-backup-companion-db-20261005.db /opt/qq-companion/data/companion.db
sudo chown -R ubuntu:ubuntu /opt/qq-companion
sudo systemctl start qq-companion
```

- 这会把"部署前那一世"的全部数据原样带回来（对话、好感度、日记）；数据库用的是阶段 B2 的第二件备份。
- 这是"已经 reset 过又想撤销 reset"的正解；reset 自己也会在 `data/backup/daily/` 留一份清档前的备份，想只回数据库也可以用它。

### 档 3：`config.toml` 也要还原（只有确认被误改时才做）

本次流程**全程没有动过**服务器 `config.toml`，所以正常情况下这一步应该什么都不用做。先验证：

```bash
# 【服务器终端执行】只读核对
diff /home/ubuntu/qqc-backup-config-toml-20261005.toml /opt/qq-companion/config.toml && echo "无需还原（逐字节一致）"
```

- 打印"无需还原（逐字节一致）" → 什么都别做。
- 有差异输出，且你确认要还原时（⚠ 这是红线操作，只在这种情况下执行）：

```bash
# 【服务器终端执行】⚠ 仅在你确认 config.toml 被误改时执行
sudo cp /home/ubuntu/qqc-backup-config-toml-20261005.toml /opt/qq-companion/config.toml
sudo chown ubuntu:ubuntu /opt/qq-companion/config.toml
sudo systemctl restart qq-companion
```

### 本地侧回滚

```powershell
# 【本地电脑执行】回到部署前那一刻的本地代码（只在你要重新打包成旧版时才用）
cd "D:\qq-companion"
git checkout v2-deploy-20261005
```

---

## 16. 常见报错对照

| 现象 | 原因 | 怎么办 |
|---|---|---|
| `ModuleNotFoundError: No module named 'xxx'`，服务起不来 | 依赖没装 / 没装完 | 回阶段 D 重跑 `pip install -r requirements.txt` |
| 日志反复 `Connection refused 3001` | NapCat 容器没起、或 WS 服务端没配 | `docker ps` → `docker restart napcat`；确认端口 3001 与 token |
| `systemctl status` 显示 `activating (auto-restart)` 循环 | 启动即崩 | `journalctl -u qq-companion -n 80 --no-pager`，原文贴回 |
| 她 1~10 分钟才回第一条 | FIXES15 首条延迟（忙时） | **正常现象**，等等就好；对话激活后不再延迟 |
| 看不到"正在输入" | NapCat 版本可能不支持 `set_input_status` | 增强项，不影响文字；记一行即可 |
| 看板打不开（`localhost:8080`） | 隧道没开 / 断了 | 重开 `ssh -L 8080:127.0.0.1:8080 ubuntu@SERVER_IP` |
| 她要发表情但屏幕上什么都没有 | 该 id 不在服务器 NapCat 表里，被静默丢弃 | 回阶段 G 对账；把差集贴回 |
| `stickers 行数` 是 33 不是 43 | 服务没重启成功，或 index.json 不是 43 键版 | 查 `systemctl status` + 阶段 C5 的键数 |
| 40 分钟后 `life_arcs` 还是 0 | 白名单缺 `life_arc`，或生成失败 | 回阶段 E1 / 阶段 J，贴回 `[Arcs]` 日志 |
| reset 输出里出现 `stickers` 行 | 误加了 `--purge-all` | 立刻用阶段 B2 的 DB 副本还原（第 15 节档 2），贴回输出 |
| `diff` 显示 config.toml 有差异 | config 被改过 | 第 15 节档 3 |
| pip 装一半网络超时 | 镜像抽风 | 原样重跑一次；仍失败贴回 |
| 磁盘满 | `/tmp` 堆了旧包 | `df -h`；删 `/tmp/qqc-deploy-20261005/` |
| 上传包大小不对 / 传输中断 | 网络抖动 | 重跑 C3（先 `rm -f /tmp/qqc-deploy-20261005/*.tar.gz` 再传） |

**通用约定**：任何一步报错 → 把**命令 + 完整输出**原样贴回，不要自己猜、不要改代码、不要重复执行有破坏性的步骤（清理 / 清档 / 还原）。
其中：阶段 F 的清理脚本重跑是安全的（白名单式，不会越删）；reset 重跑会再清一次，也安全但没必要。

---

## 17. 附录 A：这些"怪现象"都是正常的，别误判

| 现象 | 为什么正常 |
|---|---|
| 第一条消息要等好几分钟才回 | FIXES15 首条延迟：她在忙（上课/练琴/睡觉）1~10 分钟，闲时 5~30 秒 |
| reset 后【她最近的生活】是空的 | 生活主线要等第一个主动消息周期（最多 40 分钟）或凌晨钩子 |
| reset 后她话少、不主动、不亲昵 | 阶段回到「初识」，好感度从 23 左右重新涨 |
| 表情包只偶尔出现 | 设计上就是低频；FIXES20 的采纳刻度本来定在"偶尔" |
| 提示词里【她最近的生活】整块消失（没主线时） | 只有 near/today 与 2 天内 resolved 的主线会注入，没有就整块省略 |
| 重启/部署后看板数字变了 | 部分指标按真实时间衰减，跨天比对必然有小数漂移 |
| 她偶尔发 `[沉默]`——她干脆没回 | 沉默权机制：他在道别场景只发语气词/表情时，她可以选择不说话 |

## 18. 附录 B：本次部署了什么（人话版）

- **她的话语细节更真了**：不再把 `[图片]` 这种占位符发到 QQ 上；看图时会点明"这张图在聊什么情绪"；不再把整句内心旁白当消息发出来；不再连环叮嘱、不再反问追逼。
- **她有了回复节奏**：不是秒回机器了——忙的时候会晚点回，发消息前会"正在输入"。
- **她敢沉默了**：该收尾的时候能选择不说话，而不是硬接。
- **她的生活有连续性**：提过的审查、考试会有下文（生活主线），到期会有"脱口而出"的一条主动消息。
- **表情包换代**：58 → 43 键，每张都有"画面/含义/场景"三栏语义，她能按"含义"挑图；QQ 系统小黄脸也能用了（26 项清单 + 同行混排）。
- **引用回复**：他能看到"引用他某一条"的回复（机制通了，采纳率待观察）。
- **装死上线**：TTS 语音回复默认**关**（`[tts].enabled=false`），服务器 config.toml 零改动；等音色最终放行再开。
- **旧账同步落地**：停机顺序修复（这次停机能看到完整的"正在关闭…关闭完成"序列）；一切数据落库前的滤网与纪律。

## 19. 附录 C：收尾与留痕

```powershell
# 【本地电脑执行】部署完成后，本地那个 19MB 的包可以删（服务器上还有一份）
Remove-Item "D:\qq-companion-v2-20261005.tar.gz"
```

- 服务器上 `/tmp/qqc-deploy-20261005/` 的包可以留着（占几十 MB），也可以删；
- **不要删**：`/home/ubuntu/qqc-backup-*` 三件套（回滚命脉）、`data/backup/companion-before-sticker-cleanup-20261005.db`、`data/backup/daily/` 里 reset 前的备份；
- 部署前夜打的 tag `v2-deploy-20261005` 保留（以后发布 GitHub 还能用）；
- **本次刻意不做的两件（记录在案，别在部署夜顺手做）**：
  1. `logrotate` 配 `bot.log` 轮转——服务器侧随时可加，但必须用 `copytruncate`（`bot.log` 是 systemd 用追加方式持有的，常规"轮转+新建"会让新日志永远空白）；
  2. `data/backup/` 里历史快照的清理——先只读数一下（`ls -1 /opt/qq-companion/data/backup/*.db | wc -l`），确认后另找时间处理，**千万别碰 `data/backups/`**（那是本地含前两世对话的快照目录）。
- 把第 14 节 L6 的三样证据贴回，本轮就算交付完成；之后进入观察期，凭生产数据裁决积压项（文字层"告别关照抖动"、A-2 阶段 9 可达性、A-3 脉冲阈值、D-1 mood 幂等、B-1 开 TTS 前必修、语音启用）。
