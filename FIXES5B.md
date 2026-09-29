# 迭代任务书（FIXES5B）：看板"去胶水感"深化 + 删夜主题 + 删重启预告 + 日记人称修正

> 项目：QQ 伴侣机器人（D:/qq-companion）。FIXES5 的纸主题已上线，本任务是在其基础上的**质感深化**，不是推翻重做。
>
> 全局约束（同 FIXES5，逐条有效）：
> 1. 零新增依赖、零 CDN、零外部字体、零 JS 框架；JS 只允许既有主题脚本（本次将删除）与 CSS 动画；
> 2. 业务逻辑、数据获取、API 字段一律不动；
> 3. 完成后 `./venv/Scripts/python.exe -m unittest discover -s tests -v` 全绿（DOM 断言可随结构调整更新，覆盖点不减少）；
> 4. 禁止改动 `characters/`、`config.toml`。
> 5. 设计规格写死，禁止自由发挥；做完交"纸主题下各页面截图或整页 HTML 供审查"。

---

## 任务 1：删除夜间主题（admin.py）

1. 删除 `theme-night` 色板、主题切换按钮、localStorage 主题脚本、URL `?theme=` 参数处理；
2. body 固定 `theme-paper`（CSS 变量块保留 paper 一套即可，可去掉 body class 直接写 `:root`）；
3. 导航栏右侧只保留"30s 自动刷新"提示。

## 任务 2：去盒子化（核心：消灭"卡片贴墙"感）

**原则：手账是一页纸，不是一板便利贴。** 全站最多只允许三种"容器"：首屏卡（带胶带）、`/admin` 操作卡、`pre` 代码/日志窗。其余内容**全部直接躺在纸面上**。

1. 总览页：
   - 删除"系统状态总览"标题栏与 h2（首屏卡就是页面开头）；
   - 雷达图区、阶段台阶区、关系档案区、PAD 情绪区**去掉 card 边框底色**，改为`段落区块`：区块标题用 `13px / font-weight:600 / letter-spacing:2px / color:var(--ink-soft)`，标题后方跟一条弹性细横线（`flex:1; height:1px; background:var(--border)`），标题与内容间距 12px，区块之间距 36px；
   - 雷达图与阶段台阶仍左右并排（grid 1:1），但无盒子；
2. 记忆页：时间轴裸躺在纸面（现已是），但删除其外层 card；
3. 计费/调试/表情包页：表格与 pre 保留（表格是数据不是"卡片"），卡片标题规范同上；
4. 保留 `.card` 类但仅用于上述三种容器。

## 任务 3：纸的肌理（纯 CSS，禁止图片）

body 背景叠加**极淡点阵**（手账点格纸）：

```css
background-color: var(--bg);
background-image: radial-gradient(circle, #e5dcc9 1px, transparent 1.2px);
background-size: 26px 26px;
```

点色在两主题已删除后固定为 `#e5dcc9`；效果必须是"凑近才看得见"，远看仍是纯色。

## 任务 4：和纸胶带与手账细节

1. **首屏卡**：顶部居中贴一条胶带——伪元素 `width:88px; height:20px; top:-10px; left:50%; transform:translateX(-50%) rotate(-2deg); background:rgba(79,138,109,0.28);` 两端用 `clip-path` 或渐隐做出撕边感（做不到撕边就用半透明矩形，不许引入图片）；卡本身保留细边框与左侧 3px 青瓷竖线；
2. **日记条目**：每条日记改为"小便签"——无框，顶部一条 48px 宽 12px 高的小胶带（`nth-child(odd)` 旋转 -1.5deg、`nth-child(even)` 旋转 1.2deg，朱砂与青瓷交替），时间轴节点圆点颜色=该条情感色调色（不要全金）；
3. **强度条去下载条化**：改为 3px 高细条，轨道色 `var(--border)` 透明度 0.4，填充 var(--celadon)，圆角 2px，宽度 120px；数值文字 11px；
4. **欲言又止**：保留朱砂左边框 3px，顶部贴一条朱砂小胶带（同任务 4.2 规格）。

## 任务 5：微动效（全部纯 CSS，入场动画 ≤1s 且只播一次）

1. **状态灯呼吸**：OneBot 状态 ● 加 `@keyframes pulse-dot { 0%,100%{opacity:1} 50%{opacity:0.35} }`，2.6s 无限循环（这是唯一允许的无限动画）；
2. **雷达图生长**：数据多边形描边用 `pathLength="100"` + `stroke-dasharray:100` + `stroke-dashoffset:100→0`（0.8s ease-out 一次），填充 `opacity 0→1`（0.6s，delay 0.5s）；网格与轴不动画；
3. **首屏卡入场**：`opacity 0→1 + translateY(4px→0)`，0.5s 一次；
4. **导航与按钮**：hover 颜色/边框过渡 0.2s；
5. 注意 30s meta refresh 会重播入场动画，属可接受行为，不要为此加 JS 抑制。

## 任务 6：删除重启预告功能（main.py / config.py）

1. 删除优雅停机告别消息（"我去喝口水，马上回来"）、启动恢复消息（"回来了"）、`_has_announced_connected` 标记及相关全部代码路径；
2. `config.py` 移除 `restart_notice` 字段（load 时容忍旧配置里存在该键，不报错）；
3. 同步修复停机时的 `Task was destroyed but it is pending`（停止流程中不得遗留 pending task：所有后台任务在 `stop_gracefully` 里统一 cancel 并 `await asyncio.gather(..., return_exceptions=True)`）；
4. 对应测试（test_fixes4 中 4 个重启预告用例）改为验证"停机不再发送任何消息、无 pending task 警告"。

## 任务 7：日记人称修正（prompts.py）

`DIARY_SYSTEM_PROMPT` 追加一条硬规则：`日记里称呼机主必须用"他"（第三人称），是写给自己的私密日记，不是写给他的信；机主是男生。`

## 验收

1. 页面无夜主题残留（按钮、CSS、JS 均无）；
2. 总览页除首屏卡外无任何带边框盒子，区块用细线分区；
3. 背景点阵可见但不显眼；胶带元素出现在首屏卡、日记、欲言又止三处；
4. 状态灯呼吸、雷达生长动画生效且无 JS 报错；
5. `systemctl restart` 后 bot.log 无 pending task 错误，QQ 端收不到任何系统消息；
6. 全量测试绿。

## 交付

逐项结论 + 截图/HTML 样本 + 测试输出；预期改动：`companion/admin.py`、`companion/main.py`、`companion/config.py`、`companion/prompts.py`、`tests/`。
