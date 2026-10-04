# 迭代任务书（FIXES22）：TTS 语音回复·阶段 A——她偶尔会开口说话（edge-tts 原型）

> 项目：QQ 伴侣机器人（D:/qq-companion）。前置阅读：`STATUS.md`、本文件；收侧语音实现 `companion/voice.py`（对称参考）；发送链路 `companion/main.py` 的 `_send_chunk_to_onebot`。
> 背景：她目前是"哑巴"——能听懂他的语音（SenseVoice 本地 ASR），自己却只会打字。真人聊天里语音是关系升温期的自然动作（躺着懒得打字时、撒娇时、睡前）。本迭代是**阶段 A**：用 edge-tts 预置音色零成本跑通"文本→语音→NapCat record 段"全链路；阶段 B（豆包声音复刻换更贴的音色）待所有者实测"个人账号能否开通"后另行立项，**不在本任务书**。
>
> 所有者已拍板的决策（不许翻案，直接执行）：
> 1. 语音是**低频动作**：每日上限（默认 3 条，config 可调）+ 单条 ≤20 秒 + **作息场景闸门**（她在上课/合练/图书馆/考试这类"不方便说话"的活动时，模型连 `[voice:]` 都不该看到可用）；
> 2. **config 开关默认关**：`[tts] enabled` 缺省 false，上线后所有者观察文字层稳定再手动开——新功能一律"先装死、后激活"；
> 3. 模型用 `[voice:]…[/voice]` 语法把**回复中的一小段**（一两句口语）说成语音，其余照常文字气泡；整段回复转语音是反模式，禁止；
> 4. **施工时机：FIXES20/21 交付后再开工**（同改 replier/main/prompts，排队）；阶段 A 音色用 `zh-CN-XiaoxiaoNeural`（调研结论：最贴 19 岁女大学生），Xiaoyi 备选写进 config 注释；
> 5. 执行模型**不动 `characters/`、`config.toml`**（新配置字段靠代码默认值兜底，服务器 config.toml 零改动；`config.example.toml` 同步更新）；
> 6. 合规红线：只用官方预置音色，不碰任何真人声音素材（阶段 B 复刻必须真人书面授权，协议见调研报告——本任务书阶段 A 天然无此风险）。
>
> 全局约束（高压线）：
> 1. 完成后 `./venv/Scripts/python.exe -m unittest discover -s tests` 全绿；
> 2. 新代码至少一次真实 API 冒烟（edge-tts 真实合成 + 发送管道验证）；
> 3. 交付：逐项 diff 摘要 + 测试输出 + 冒烟证据（含一段真实合成的 mp3 文件路径，所有者可以亲耳听）。

---

## 任务 1：合成模块（新文件 companion/tts.py，与 voice.py 对称）

1. `TTSManager`：输入一段文本 → 输出 mp3 临时文件路径（`data/voice_out/`，目录不存在则建）；
2. 调用 `edge_tts.Communicate(text, voice, rate=..., pitch=...)`，默认 `voice="zh-CN-XiaoxiaoNeural"`、`rate="-8%"`（压播音腔，注释注明可调）；**先落盘再发送**，发送完 `finally` 删临时文件（复用 voice.py 的清理模式）；
3. 失败纪律：合成超时（10s）/异常/返回空文件 → 返回 None，调用方按文字降级，**绝不因 TTS 失败丢消息**；
4. 依赖：`requirements.txt` 加 `edge-tts`（钉版本，调研报告提示接口有改版史）；ffmpeg 服务器已装、NapCat 内置转码，mp3 直接发 record 段即可；
5. 每日计数：state 键记当日已发语音条数，跨天自动清零（参照 proactive 的日计数模式）。

## 任务 2：`[voice:]` 语法与发送链路（replier.py + main.py + onebot.py）

1. **语法**：`[voice:]一小段口语[/voice]`，模型把它放在回复中想要"说出口"的位置；`parse_reply` 识别并转成 `{"type": "voice", "content": ...}` chunk；
2. **校验与降级**：`[tts].enabled=false` / 每日已达上限 / 作息闸门关闭 / TTS 合成失败 → voice chunk 的文本**按普通文字发出**（降级永远保消息）；每轮最多 1 条 voice chunk，多余的降级为文字；内容超过 ~60 字（≈20 秒）截断到最近句读（宁可短不可长）；
3. 发送：`onebot.py` 新增 `build_record_segment()`（镜像 `build_image_segment`，短语音用 base64://）；`main.py _send_chunk_to_onebot` 加 voice 分支：合成→发送→删临时文件；
4. **落库对称**：她发的语音在 turns 里以 `（语音消息）文本` 形态落库（与收侧他的语音转写同格式），observer/日记看到的是内容不是"一段语音"占位；
5. typing 联动：voice chunk 不触发 typing 表演（没人一边打字一边发语音）；语音发送前可以按合成出的时长短暂停顿（模拟"按住说话"的节拍，≤3s，做不进本迭代就留备注）。

## 任务 3：时机闸门（prompts.py + tts.py）

1. **作息场景闸门**：`persona.get_current_activity()` 返回的活动文本命中"上课/合排/合练/排练/图书馆/考试/讲座/熄灯"关键词 → 当轮 voice 语法不可用（提示词里直接不写 voice 能力说明，机制层同时兜底降级）——双保险；
2. **提示词**（仅 `enabled=true` 且闸门打开时注入 `SYSTEM_PROMPT_TEMPLATE`）：`你可以把回复中的一小段用 [voice:]…[/voice] 说成语音发给他——只在躺下休息、走路、睡前这类适合说话的时候，一天最多几条，说的是一两句口语，不是把整段回复念出来`；
3. 主动消息（proactive）通路同样可用（同闸门同上限）——"睡前收到一条她的语音"是这张卡的高光场景；
4. config 新增 `[tts]` 段（`enabled`/`voice`/`rate`/`daily_limit`/`max_chars`），全部代码默认值兜底；`config.example.toml` 同步。

## 任务 4：测试与冒烟

**单元测试**（tests/test_fixes22.py）：`[voice:]` 识别与边界（未闭合/多个/行内位置）；降级四路径（开关关/日上限/闸门关/合成失败）逐路径用例；60 字截断；落库 `（语音消息）` 形态；日计数跨天清零；与 face/sticker/quote 同轮共存；proactive 通路。
**真实 API 冒烟**（scripts/smoke/smoke_fixes22.py）：
- A 段：真实 edge-tts 合成一句话 → 打印 mp3 文件大小与时长（文件保留在 `data/voice_out/smoke_*.mp3` **供所有者亲耳试听**）；
- B 段：mock 她输出含 `[voice:]` 的回复走完整发送管道到 mock OneBot，贴出站报文（record 段结构）与落库记录；
- C 段：闸门验证——构造"她在上课"的活动状态，确认 voice 被降级成文字。

## 负面清单

1. 不动 `characters/`、`config.toml`；不做声音复刻（阶段 B 另立项）；不做"她说方言/唱歌"等花哨能力；
2. 不改收侧 ASR（voice.py）任何行为；语音不进看板新页面（留待以后）；
3. 单条语音 ≤20 秒硬顶（NapCat >35s 有失败报告、QQ 上限 60s，远离边界）；
4. 不部署、不碰服务器；与 FIXES20/21 串行施工。

## 验收 DoD

1. 测试全绿；2. 冒烟 A/B/C 三段证据（A 段 mp3 所有者试听认可音色是**放行必要条件**——音色不像她，功能宁可不开）；3. 逐项 diff 摘要；4. git 单 commit（前缀 `FIXES22:`）。

## 备注（规划模型留）

- **阶段 B 前置动作（所有者）**：拿个人实名账号去火山引擎控制台实测能否开通"豆包声音复刻 2.0"——新手手册已备好 `docs/VOLCENGINE_SETUP.md`（2026-10-04 调研：注册→扫脸实名→开通管理→API Key 全流程 + 按钮判据）；调研结论：预置音色路线个人 100% 可开，复刻 75~85% 概率可开；**阶段 B 出现第三条路——豆包官方预置女声（130+ 音色含"爽快思思/Vivi 温柔"等贴人设项，3 元/万字符、2 万字符免费、零授权风险），可能比克隆更省事**，阶段 B 立项时三选一（edge-tts 续用 / 豆包预置 / 豆包复刻）；价格口径已更正——当前 **3 元/万字符**（非旧记录 8 元），复刻音色槽位 **138 元/音色一次性**（预付费下单扣/后付费首次合成扣），免费额度 2 万字符/半年够原型；
- 阶段 B 若做，素材必须真人书面授权（火山《声音复刻协议》4.1.1 + 民法典声音权），她是 19 岁女大学生、所有者是男性，素材需找一位女性真人授权录制（14~30s 干净 wav）；
- DEEP_AUDIT 面 B 补交互对：voice × typing（不触发）、voice × 沉默权（[沉默] 优先，沉默时不合成）、voice × 每日上限计数与 proactive 共用账目。
