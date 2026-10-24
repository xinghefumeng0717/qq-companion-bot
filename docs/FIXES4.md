# 迭代任务书（FIXES4）：重启预告 + 应用内备份 + 管理网页 + 青梓小窝桌面控制台

> 项目：QQ 伴侣机器人（D:/qq-companion，蓝图 PLAN.md，前序 FIXES/FIXES2/FIXES3 已完成）。
> 主题：降低日常运维门槛。
>
> 全局约束：
> 1. 不引入新第三方依赖（桌面 GUI 用标准库 tkinter；SSH/SCP 调系统命令）；
> 2. **服务器 config.toml 已被冻结**：本次所有新增配置项必须在代码里给出默认值，旧 config.toml 缺省运行时行为正常（restart_notice 默认 true）；
> 3. 禁止改动 `characters/example/character.json`；提示词集中 prompts.py；LLM 调用集中 gateway.py；
> 4. 完成后 `./venv/Scripts/python.exe -m unittest discover -s tests -v` 全绿；
> 5. 与 PLAN/FIXES 矛盾时停下报告。

---

## 任务 1：重启预告（companion/main.py）

1. 优雅停机（SIGTERM/SIGINT，现有"接收到退出信号"路径）时，在关闭 OneBot 连接前向机主私聊发送一条告别消息：`我去喝口水，马上回来`；
2. 启动并完成 OneBot 首次连接成功后，发送：`回来了`；
3. 发送失败静默忽略（记 debug 日志即可，绝不影响启停流程）；配置开关 `[reply] restart_notice = true`（缺省 true）；
4. 注意：systemd 杀死的是无响应进程时不保证能发出，属正常；只有优雅路径才发。

## 任务 2：应用内每日备份（companion/backup.py）

不依赖系统 cron，在机器人进程内实现：

1. 新增 asyncio 定时任务：每天 04:17 执行一次备份（启动时若当天尚未备份则补一次）；
2. 备份动作：SQLite 在线备份（`VACUUM INTO` 或文件复制）`data/companion.db` → `data/backup/daily/companion-YYYYMMDD-HHMM.db`，并同步复制一份为 `data/backup/daily/latest.db`（供外部固定路径拉取）；
3. 保留最近 14 份，更旧的自动删除；
4. 每次备份写一行 INFO 日志；失败 WARNING 不影响主流程；
5. 备份逻辑写成独立函数 `run_daily_backup(db_path) -> str`（返回备份路径），供任务 3 的"立即备份"按钮复用。

## 任务 3：状态 API 与 /admin 管理页（companion/admin.py）

### 3.1 `GET /api/status`（JSON）

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

`onebot_connected` 需要 OneBotClient 暴露一个连接状态属性（admin 构造时注入 onebot 客户端引用）。

### 3.2 总览页加"连接状态"灯

总览页顶部加一行状态灯：绿色●OneBot 在线 / 红色●OneBot 掉线（读同一状态属性）。

### 3.3 `GET /admin` 管理页 + 三个操作（POST）

页面含三个按钮，每个按钮点击前页面内二次确认：
- **立即备份**：POST /admin/backup → 调 `run_daily_backup()`，返回备份路径；
- **重启服务**：POST /admin/restart → 先渲染"已收到，5 秒后重启"，然后 `asyncio` 延迟调用 `os._exit(0)`，由 systemd `Restart=always` 自动拉起（这是无 sudo 权限下重启自身的标准做法，注释说明）；
- **重置数据**：POST /admin/reset，请求体必须含 `confirm=YES`（页面输入框要求手敲 YES），复用 `companion/reset.py` 的核心重置函数（先自动备份再清档，保留 stickers/llm_calls 的默认行为），返回各表清除行数。
管理页导航栏加入口；所有操作写 WARNING 日志（操作类型 + 结果）。

## 任务 4：青梓小窝 · 桌面控制台（launcher/，本地 Windows 应用）

### 4.1 技术形态

- `launcher/qingzi_home.py`：tkinter 深色小窗（约 340×460），标题"青梓小窝"，无第三方依赖；
- `launcher/创建桌面快捷方式.ps1`：用 WScript.Shell 生成桌面快捷方式，目标为项目 venv 的 `pythonw.exe`（**无控制台黑窗**）+ 脚本路径，图标用 `launcher/assets/app.ico`（若 `D:\chatgpt-on-wechat\companion.ico` 存在则复制之，否则用 tkinter 默认）；
- SSH 命令假设免密（用户已配置密钥），连接失败时在界面上显示错误而不是崩溃。

### 4.2 功能（全部为界面按钮或自动行为）

1. **一键连接/断开**：后台隐藏进程 `ssh -N -L 8080:127.0.0.1:8080 ubuntu@<你的服务器IP>`（CREATE_NO_WINDOW）；窗口关闭时自动终止该进程；
2. **三盏状态灯**（每 10 秒轮询）：隧道（本地 8080 端口可连接）/ 看板（http://localhost:8080 可访问）/ 机器人（`/api/status` 返回 bot_alive 且 onebot_connected）；
3. **打开看板**：默认浏览器打开 `http://localhost:8080`；
4. **一键拉备份**：`scp ubuntu@<你的服务器IP>:/opt/qq-companion/data/backup/daily/latest.db "D:\qq-companion\data\backups\"`，完成后显示"上次同步：HH:MM"（持久化到 `data/backups/sync_state.json`）；
5. **仿真对话**：新开一个 cmd 窗口执行 `ssh -t ubuntu@<你的服务器IP> "cd /opt/qq-companion && ./venv/bin/python -m companion.chat"`（给用户调试人设用）；
6. **掉线提醒**：轮询发现 `onebot_connected=false` 时，界面红灯 + 弹出提示"青梓掉线了，该去 NapCat 扫码了"（每 10 分钟最多提醒一次）；
7. **头部信息栏**：从 `/api/status` 读取显示"当前阶段：相识 · 今日费用：¥0.42"；
8. **快捷链接**：打开项目文件夹、打开 /logs 页。

### 4.3 样式

深色背景（#1e1e2e 系）、圆角按钮、无动画；按钮排布竖向列表，状态灯在顶部横排。保持简洁，不堆砌控件。

---

## 任务 5：main.py 圈养式重构（纯搬运，零行为变化）

**目标**：拆解上帝模块。只许成功不许变味。

1. 新建 `companion/turn_handler.py`：把 main.py 中"一轮对话的完整流水线"（聚合回调 → 图片两段式/语音 → 组装 → 流式回复 → replier 分段发送 → 落库 → 观察者结算触发）整体迁入，封装为 `TurnHandler` 类；
2. `main.py` 只保留：组件装配、启停信号处理、OneBot 事件入口转发；
3. **硬性红线**：
   - 纯代码移动与必要的引用调整，禁止"顺手优化"任何逻辑、提示词、参数；
   - 现有 55 个测试**不许修改**，必须全部原样通过；
   - 完成后必须用真实 API 做一次冒烟验证（仿真 CLI 聊 1 轮 + 观察日志无异常），并在报告中附上输出；
4. 若拆分过程中发现逻辑纠缠到无法"纯搬运"，停下来报告，宁可不拆。

## 测试要求

- 任务 2：备份函数产出文件、latest.db 生成、14 份轮转删除最旧（用临时目录）；
- 任务 3：`/api/status` 返回 JSON 字段齐全；/admin/reset 缺少 confirm=YES 时拒绝执行且数据库无变化；/admin/backup 返回备份路径；
- 任务 4：界面不测，但状态解析（JSON → 灯状态）、sync_state.json 读写、备份拉取路径拼接写成纯函数并测试；
- 任务 1：优雅停机路径的单测（mock onebot 发送，断言发了告别消息且发送失败不阻塞关闭）。

## 交付

逐项结论 + 全量测试结果 + README 增补（青梓小窝使用方法、/admin 页说明、每日备份机制）。
