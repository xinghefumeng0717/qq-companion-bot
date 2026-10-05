# 把它变成你自己的伴侣：定制指南

> 这个仓库默认是一套"能跑的骨架 + 一张虚构示例卡"。本指南带你把它变成**你的**伴侣。
> 写卡的方法论（怎么写得不像 AI）另见长文 [CARD_CRAFT.md](CARD_CRAFT.md)，本文只管"怎么换"。

## 总览：五步

```
clone → 装依赖 → 改 config.toml → 写你的角色卡 → 沙箱试聊 → 上线
```

## 第 1 步：config.toml 里必须改的字段

复制 `config.example.toml` 为 `config.toml`，真正**必须**改的只有这些：

| 字段 | 含义 | 不改的后果 |
|---|---|---|
| `[account] allowed_user_id` | 你的 QQ 大号（机器人只响应这个人） | 它不认识你，不回话 |
| `[account] bot_qq` | 机器人登录的小号 QQ | 仅影响自我识别过滤 |
| `[onebot] access_token` | 与 NapCat WebUI 里设的密钥一致 | 连不上 NapCat |
| `[models.*] api_key` | 你的 LLM API key（默认档案是 DeepSeek） | 401，全程降级 |
| `[character] path` | 你的角色卡目录（第 2 步建） | 用示例卡跑 |

其余全部有默认值兜底，能跑再慢慢调。计费价目（`[llm.pricing]`）换成你用的模型官网价即可，不换也能跑，只是费用统计不准。

## 第 2 步：写你的角色卡

```bash
cp -r characters/example characters/my_character
```

然后编辑 `characters/my_character/character.json`，字段的心法（详表见 README 第 5 节）：

- **`core_description`**：她是谁。别写"温柔可爱"这种单薄片语——人是由**矛盾**组成的（我们的真卡是三个互相打架的侧面拼出来的）；
- **`chat_style.rules`**：硬纪律。有几条是血泪换来的，建议原样抄：禁括号旁白、心里活动不上屏、绝不承认是 AI、告别不拖尾；
- **`good_examples` / `plain_examples` / `bad_examples`**：**最重要、最花时间的部分**。bad_examples 请收集你真实聊天里 AI 腔的翻车原句钉进去——抽象规则没用，具体错例有用；
- **`stages`**：恰好 10 个关系阶段，每个阶段给语气示范和禁区（比如早期阶段禁亲昵词）；
- **`daily_routine`**：覆盖全天 24 小时的作息表。它驱动"她此刻在干嘛"、回复延迟、语音闸门；
- **`initial_dims`**：开局六维好感度（决定开局在哪个阶段）；
- **`long_holiday_activity` / `calendar_anchors` / `life_arc_seed_pool`**（可选）：长假文案、日历锚点、生活主线取材范围。不填分别是"通用默认 / 无锚点 / 不做生活主线"；想让"她的生活有连续性"，至少填 `life_arc_seed_pool`，写法见 README 第 5.1.1 节与示例卡。

**改卡后必须做的事**：跑 `python -m companion.chat` 进沙箱试聊（不碰 QQ、不碰生产数据），觉得不对就改卡再来。别直接上线试错。

## 第 3 步：表情包（可选）

往 `characters/my_character/stickers/` 放图，`index.json` 里登记名称与含义。运行后她还会自己收藏新图（视觉模型自动打标）。没有表情包也能跑。

## 第 4 步：值得知道的旋钮

| 想要什么 | 动哪里 |
|---|---|
| 她主动找你的频率 | `[proactive]` 段的唤醒间隔与闸门 |
| 语音回复 | `[tts]` 段（默认关；开之前读 config 注释） |
| 好感度涨速/阶段刻度 | `companion/affection.py` 的阈值表（动之前先读 FIXES8 文档，这是有仿真实测校准过的） |
| 作息/长假行为 | 卡里的 `daily_routine`（日常）+ `long_holiday_activity`（长假那句）+ `[llm.pricing].holidays` 填法定节假日 |
| 她的生活主线取材 | 卡里的 `life_arc_seed_pool`（必填才会生成）+ `calendar_anchors` |

## 第 5 步：上线前自检清单

- [ ] `python -m unittest discover -s tests` 全绿（ outsiders 的 clone 会 skip 27 条依赖私有画像的用例，属正常）；
- [ ] 沙箱里聊过至少 20 轮，告别、沉默、表情包场景都试过；
- [ ] `config.toml` 不在 git 跟踪里（`.gitignore` 已拦，别手动 `git add -f`）；
- [ ] 服务器部署照 README 第 3、7 节。

## 常见问题

- **"测试报角色卡找不到"**：不会了。测试自动回落到 `characters/example`，也可用 `QQC_TEST_CARD=<路径>` 指定；
- **"改卡要重启吗"**：角色卡每次组装提示词时现读，改完下轮对话即生效，不用重启；
- **"她会一直这样吗"**：不会。好感度、情绪、记忆都在随你们的相处演化——这正是这个项目的意思。
