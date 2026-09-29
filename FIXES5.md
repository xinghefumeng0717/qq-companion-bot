# 迭代任务书（FIXES5）：监控看板"手账"主题重设计 + 青梓小窝质感优化

> 项目：QQ 伴侣机器人（D:/qq-companion，蓝图 PLAN.md，前序 FIXES~FIXES4 完成，71 测试全绿）。
> 本次为**纯前端重设计**，禁止改动任何业务逻辑、路由行为、数据内容、API 字段。
>
> 全局约束（违反即返工）：
> 1. **零新增依赖**：禁止任何 CDN、外部字体、JS 框架、CSS 框架。只用系统字体栈 + 内联 CSS + 服务端渲染；
> 2. 允许的唯一 JS：主题切换按钮的 ≤15 行内联脚本（读写 localStorage、切换 body class）；其余交互一律不做；
> 3. 保留 30 秒自动刷新；刷新后主题选择不丢失（localStorage）；
> 4. `companion/admin.py` 是唯一改动对象（小窝除外）；业务数据获取代码（SQL、引擎调用）不许动；
> 5. 完成后 `./venv/Scripts/python.exe -m unittest discover -s tests -v` 全绿（test_m6 的 DOM 断言可按新结构更新，但覆盖点不许减少）；
> 6. 禁止改动 `characters/`、`config.toml`。

---

## 任务 1：设计系统（两个主题，一套 CSS 变量）

所有页面共用一个 `html_shell(title, path, content)` 渲染（重构现有同名函数），CSS 全部内联在其 `<style>` 中。

### 1.1 主题机制

- `<body>` 携带 class：`theme-paper` 或 `theme-night`；
- 导航栏最右侧放切换按钮"纸 / 夜"，点击切换 body class 并写 `localStorage.setItem('qingzi-theme', ...)`；
- 页面加载时内联脚本读 localStorage 应用主题；**无存储时默认 `theme-paper`**（纸是主打主题）；
- 30 秒 meta refresh 不重置主题（主题由 localStorage 恢复）。

### 1.2 色板（写死，不许改）

```
/* theme-paper 手账纸（默认） */
--bg:        #f6f1e7   /* 宣纸底 */
--card:      #fbf8f1   /* 纸页卡片 */
--border:    #e0d8c8   /* 纸页裁切线 */
--ink:       #2b2b26   /* 正文墨色 */
--ink-soft:  #6b675c   /* 次级墨 */
--ink-faint: #9a9484   /* 弱化墨 */
--celadon:   #4f8a6d   /* 青瓷绿（数据、在线状态、强调） */
--celadon-bg:#e3eee7   /* 青瓷浅底（进度条、色笺） */
--cinnabar:  #c04851   /* 朱砂（警示、私密内容、离线状态） */
--gold:      #b8862f   /* 暖金（温暖类情感、里程碑） */

/* theme-night 夜（沿用现有深色基调微调） */
--bg:        #14151a
--card:      #1e2027
--border:    #2e3138
--ink:       #d8d5cc
--ink-soft:  #9b978c
--ink-faint: #6b6a63
--celadon:   #6faa8d
--celadon-bg:#22312b
--cinnabar:  #d0676f
--gold:      #c9a05a
```

### 1.3 字体栈（系统字体，禁止外链）

- 正文/表格/UI：`"Microsoft YaHei UI", "PingFang SC", sans-serif`
- **情感文字**（首屏一句、日记正文、欲言又止、阶段语气描述）：`"STKaiti", "KaiTi", "SimSun", serif`，斜体
- 数字：`"Consolas", "Courier New", monospace`

### 1.4 基础版式

- 容器 `max-width: 1080px` 居中，左右 padding 24px；
- 卡片：`background: var(--card); border: 1px solid var(--border); border-radius: 6px; padding: 20px 24px;` 卡片间距 20px；**禁用盒阴影**（纸感靠边框不靠阴影）；
- 标题层级：页面主标题 20px/600、卡片标题 15px/600（颜色 var(--ink)），卡片标题下方 8px 处一条 1px var(--border) 细线；
- 导航栏：顶部通栏，底部 1px var(--border)；链接 14px，当前页下划线 2px var(--celadon)；最右为主题切换按钮（文字按钮"纸/夜"）。

---

## 任务 2：总览页（/）重设计

自上而下四个区块：

### 2.1 首屏"她此刻"（全新，最核心）

- 一张通栏卡片，内文**只有一句话**，情感字体（楷体系）、斜体、17px、行高 1.9：
  > *九月廿九，星期二。心情不错，正在临湖餐厅二楼吃饭。他已有 2 小时没说话了。*
- 生成规则（Python 拼句，数据已有）：中文日期（"九月廿九"格式，需实现 1~31 的中文数字转换）、星期、心情描述、当前作息活动、距上次聊天小时数（<1 小时则省略末句）；
- 卡片左侧 3px 竖线，颜色 var(--celadon)。

### 2.2 好感度雷达图 + 阶段台阶（并排两卡，grid 1:1）

- **雷达图**：重绘现有 SVG——网格与轴线用 var(--ink-faint) 细线，数据多边形填充 `var(--celadon)` 透明度 0.25、描边 2px var(--celadon)，六个顶点 3px 实心圆点；每轴端点外侧标注维度名（12px var(--ink-soft)）和数值（Consolas 13px var(--ink)）；
- **阶段台阶**：把 10 个阶段画成 10 级横向台阶（div 横排，每级 `flex:1; height:8px; border-radius:2px`），已过阶段填 var(--celadon)，当前阶段填 var(--gold) 并在下方标注阶段名（情感字体 14px），未到阶段填 var(--border)；台阶下方一行小字："复合分 23.4 · 距「熟络」还需 7.6"。

### 2.3 关系档案 + 连接状态（并排两卡）

- 关系档案：认识天数（大号数字 28px + "天"）、累计对话轮数、里程碑竖向时间线（每条：左侧 6px 圆点 var(--gold)，阶段名 + 日期）；
- 连接状态：OneBot 灯（在线 var(--celadon) ● / 离线 var(--cinnabar) ●）、今日费用、上次备份时间。

### 2.4 PAD 情绪卡

- 三个数值条（愉悦/唤醒/安心），每条：标签 13px + 横向细条（高度 6px，填充 var(--celadon)，负值从中心向左右延伸的偏离式）+ 数值；
- 条下一句情感字体小字：当前的心情描述与信任描述（来自现有翻译层数据）。

---

## 任务 3：记忆页（/memory）重设计为"手账时间轴"

- 日记列表改为**竖向时间轴**：左侧竖线 2px var(--border)，每条日记一个节点；
- 节点内容：日期（Consolas 12px var(--ink-faint)）+ 情感色调色笺（小圆角矩形 4px 高 24px 宽，温暖/幸福/欢喜=var(--gold)，思念=var(--celadon)，不安/伤感=var(--ink-soft)，平静=var(--ink-faint)）+ 正文（情感字体 15px 斜体）+ 强度衰减条（6px 高，宽 = strength% ）；
- 语义事实、待跟进、欲言又止保持卡片式，欲言又止卡片左边框 3px var(--cinnabar)（红笔批注感），正文用情感字体。

---

## 任务 4：其余页面统一设计系统

- **/costs**：汇总数字用 24px Consolas；表格隔行底色 var(--bg)；缓存命中率数字用 var(--celadon)；
- **/debug**：system prompt 的 `<pre>` 用纸页卡片样式，等宽 12px，行高 1.7；观察者打分均值数字加粗；
- **/logs**：深色日志窗不受主题影响（固定 #1a1b20 底 #a9d6c3 字），等宽 12px；
- **/stickers**：图片网格卡片边框 var(--border)，描述文字 12px var(--ink-soft)；
- **/admin**：三个操作按钮颜色统一为——备份 var(--celadon)、重启 var(--gold)、重置 var(--cinnabar)；版式同卡片规范。

---

## 任务 5：青梓小窝质感优化（launcher/qingzi_home.py）

不加头像、不加功能，只做质感：

1. **状态灯改三张小卡片**：横向三等分，每张含——状态点（● 8px）+ 标题（"隧道"/"看板"/"伴侣"）+ 一行小字描述（隧道："已连接" / "未连接"；看板："可访问"/"不可达"；伴侣："在线"/"离线"/"未知"），离线红在线绿未知黄（色值沿用现有常量）；
2. **按钮**：统一高度 38px，hover 变色已有——加深按下态（activebackground 再深一档）；主按钮（连接/断开）与其余按钮拉开 12px 间距；
3. **底部状态栏**：改为左对齐"消息样式"（前缀"› "，颜色 #6c7086），不再居中；
4. **字体**：标题"青梓小窝" 16px bold，信息行 9px，按钮 10px，链接 8.5px；全局字体 "Microsoft YaHei UI"；
5. 窗口固定尺寸逻辑（DPI 缩放）保持不变。

---

## 验收标准（逐项核对，不许打折）

1. 所有页面在两种主题下切换正常、刷新后主题保持；
2. 默认主题（清空 localStorage）为纸主题；
3. 首屏句子中文日期正确（如"九月廿九"）、四项数据真实；
4. 雷达图/阶段台阶/时间轴在两种主题下对比度可读（朱砂、青瓷在两主题下都不糊）；
5. 全页无 CDN 请求（开发者工具 Network 面板验证）、无新增依赖；
6. 全量测试通过；
7. 交付每个页面在"纸"主题下的整页截图（可用无头浏览器或手动）。

## 交付

逐项结论 + 截图 + 测试输出。改动文件预期仅：`companion/admin.py`、`launcher/qingzi_home.py`、`tests/test_m6_admin.py`（如需更新断言）、`tests/`（如新增主题相关测试）。
