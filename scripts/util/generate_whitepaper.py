"""生成产品侧白皮书 PDF：基于多模态大模型的长程情感陪伴产品设计
目标路径：D:\private\个人资料\galgame聊天\把Galgame带入现实：基于多模态大模型的长程情感陪伴产品白皮书.pdf
"""

import os
import subprocess
import sys

HTML_CONTENT = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>把Galgame带入现实：基于多模态大模型的长程情感陪伴产品白皮书</title>
<style>
  @page {
    size: A4 portrait;
    margin: 14mm 16mm 14mm 16mm;
  }
  * {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
  }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
    color: #1e293b;
    background-color: #ffffff;
    font-size: 13px;
    line-height: 1.7;
    -webkit-font-smoothing: antialiased;
  }

  /* 页面容器：强制每页高度与分页 */
  .page {
    width: 100%;
    min-height: 268mm;
    max-height: 268mm;
    page-break-after: always;
    page-break-inside: avoid;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    position: relative;
    padding-bottom: 2mm;
  }
  .page:last-child {
    page-break-after: avoid;
  }

  /* 页眉与页脚：极致轻盈典雅 */
  .page-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid #e2e8f0;
    padding-bottom: 6px;
    margin-bottom: 14px;
    font-size: 11px;
    color: #64748b;
    letter-spacing: 0.05em;
  }
  .page-header .brand {
    font-weight: 600;
    color: #0f172a;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .page-header .brand::before {
    content: "";
    display: inline-block;
    width: 6px;
    height: 6px;
    background: #2563eb;
    border-radius: 50%;
  }
  .page-footer {
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-top: 1px solid #e2e8f0;
    padding-top: 6px;
    font-size: 11px;
    color: #94a3b8;
  }

  .content-body {
    flex: 1;
    display: flex;
    flex-direction: column;
    justify-content: flex-start;
  }

  /* 标题与章节系统 */
  .section-tag {
    font-size: 10.5px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: #2563eb;
    margin-bottom: 4px;
    display: block;
  }
  h1.page-title {
    font-size: 20px;
    font-weight: 700;
    color: #0f172a;
    letter-spacing: -0.02em;
    margin-bottom: 12px;
    line-height: 1.3;
  }
  h2.section-subtitle {
    font-size: 14px;
    font-weight: 600;
    color: #1e293b;
    margin: 12px 0 6px 0;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  h2.section-subtitle::before {
    content: "";
    display: inline-block;
    width: 3px;
    height: 13px;
    background: #2563eb;
    border-radius: 2px;
  }

  p {
    margin-bottom: 8px;
    color: #334155;
    text-align: justify;
  }
  p.lead {
    font-size: 13.5px;
    color: #1e293b;
    line-height: 1.75;
    font-weight: 450;
  }

  /* 高级卡片网格系统（拒绝狗皮膏药，统一克制） */
  .card-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 10px;
    margin: 10px 0;
  }
  .card-grid-3 {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 10px;
    margin: 10px 0;
  }
  .card {
    border: 1px solid #e2e8f0;
    background-color: #fafbfc;
    border-radius: 5px;
    padding: 10px 12px;
  }
  .card-title {
    font-size: 12.5px;
    font-weight: 600;
    color: #0f172a;
    margin-bottom: 4px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .card-title .badge {
    font-size: 10px;
    font-weight: 500;
    color: #2563eb;
    background: #eff6ff;
    padding: 1px 6px;
    border-radius: 3px;
    border: 1px solid #dbeafe;
  }
  .card-text {
    font-size: 11.5px;
    color: #475569;
    line-height: 1.6;
  }

  /* 引用导言与核心哲学 */
  .quote-box {
    border-left: 3px solid #2563eb;
    background-color: #f8fafc;
    padding: 8px 12px;
    margin: 8px 0;
    border-radius: 0 4px 4px 0;
  }
  .quote-title {
    font-size: 11.5px;
    font-weight: 600;
    color: #1e3a8a;
    margin-bottom: 3px;
  }
  .quote-content {
    font-size: 12px;
    color: #334155;
    line-height: 1.65;
  }

  /* 结构化表格：精细纤薄 */
  table.editorial-table {
    width: 100%;
    border-collapse: collapse;
    margin: 10px 0;
    font-size: 11.5px;
  }
  table.editorial-table th {
    background-color: #f1f5f9;
    color: #0f172a;
    font-weight: 600;
    text-align: left;
    padding: 7px 10px;
    border-top: 1px solid #cbd5e1;
    border-bottom: 1px solid #cbd5e1;
  }
  table.editorial-table td {
    padding: 6px 10px;
    border-bottom: 1px solid #e2e8f0;
    color: #334155;
    vertical-align: top;
  }
  table.editorial-table tr:nth-child(even) td {
    background-color: #fafbfc;
  }

  /* 封面专属样式 */
  .cover-container {
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    height: 100%;
    padding: 30mm 10mm 20mm 10mm;
  }
  .cover-tagline {
    font-size: 12px;
    letter-spacing: 0.25em;
    color: #2563eb;
    text-transform: uppercase;
    font-weight: 700;
    margin-bottom: 16px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .cover-tagline::after {
    content: "";
    flex: 1;
    height: 1px;
    background: #cbd5e1;
  }
  .cover-main-title {
    font-size: 32px;
    font-weight: 800;
    color: #0f172a;
    line-height: 1.25;
    letter-spacing: -0.03em;
    margin-bottom: 18px;
  }
  .cover-sub-title {
    font-size: 16px;
    font-weight: 400;
    color: #475569;
    line-height: 1.6;
    margin-bottom: 30px;
    max-width: 90%;
  }
  .cover-feature-pills {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 40px;
  }
  .pill {
    font-size: 11.5px;
    font-weight: 500;
    color: #1e3a8a;
    background: #f1f5f9;
    border: 1px solid #cbd5e1;
    padding: 5px 12px;
    border-radius: 20px;
  }
  .cover-meta {
    border-top: 2px solid #0f172a;
    padding-top: 16px;
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 16px;
  }
  .meta-item-label {
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #64748b;
    margin-bottom: 4px;
  }
  .meta-item-val {
    font-size: 12px;
    font-weight: 600;
    color: #0f172a;
  }
</style>
</head>
<body>

<!-- ========================================================================= -->
<!-- 封面 (Page 1)                                                            -->
<!-- ========================================================================= -->
<div class="page">
  <div class="cover-container">
    <div>
      <div class="cover-tagline">AI Digital Companion Architecture & Product Whitepaper</div>
      <div class="cover-main-title">把 Galgame 带入现实：<br>基于大模型的长程情感陪伴产品白皮书</div>
      <div class="cover-sub-title">从“全知全能的冰冷工具”到“QQ 里的活态数字生命”——六维好感阻力、连续情绪场与类人分层遗忘的工业级落地方案</div>
      
      <div class="cover-feature-pills">
        <span class="pill">六维好感物理阻力</span>
        <span class="pill">PAD 连续情绪自愈场</span>
        <span class="pill">艾宾浩斯遗忘曲线加固</span>
        <span class="pill">真实生物钟与自尊主动交互</span>
        <span class="pill">反小说化短消息流</span>
        <span class="pill">复古纸材质感小窝</span>
      </div>
    </div>

    <div>
      <div class="cover-meta">
        <div>
          <div class="meta-item-label">产品定位</div>
          <div class="meta-item-val">常驻型数字生活伴侣（以青梓为例）</div>
        </div>
        <div>
          <div class="meta-item-label">通信载体与环境</div>
          <div class="meta-item-val">QQ 即时通信平台 · OneBot 协议栈</div>
        </div>
        <div>
          <div class="meta-item-label">设计时间与版本</div>
          <div class="meta-item-val">2026 年秋 · 生产级交付版本</div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ========================================================================= -->
<!-- 第 1 章：产品起源与行业痛点破局 (Page 2)                                      -->
<!-- ========================================================================= -->
<div class="page">
  <div class="page-header">
    <span class="brand">QQ 伴侣机器人产品白皮书</span>
    <span>01 / 行业痛点与产品愿景</span>
  </div>
  <div class="content-body">
    <span class="section-tag">01 / Industry Void & Product Vision</span>
    <h1 class="page-title">为什么市面上的“AI 伴侣”大多是塑料玩具？</h1>
    
    <p class="lead">在大语言模型（LLM）普及后，市面上涌现了海量的 AI 角色扮演产品。然而，几乎所有用户在新鲜感消退后（通常不超过 48 小时），都会产生强烈的索然无味感。问题的本质在于：行业普遍把“角色扮演”做成了<strong>带有皮套的客服助手</strong>。</p>

    <h2 class="section-subtitle">传统 AI 伴侣的三大致命硬伤</h2>
    <div class="card-grid">
      <div class="card">
        <div class="card-title">
          <span>1. 金鱼脑：断裂的时间感知</span>
          <span class="badge">缺乏长程记忆</span>
        </div>
        <div class="card-text">聊天仅依赖有限上下文窗口，聊过 20 轮便“昨事全非”；即便引入通用 RAG，也是机械提取关键词，缺乏像人类一样随时间流逝而“淡忘、沉淀、回忆被唤起”的生命质感。</div>
      </div>
      <div class="card">
        <div class="card-title">
          <span>2. 讨好型人格：廉价的无底线迎合</span>
          <span class="badge">缺乏心理阻力</span>
        </div>
        <div class="card-text">只要机主输入几句夸赞，AI 瞬间好感拉满甚至直奔表白。缺乏现实人类人际交往中的“拘谨、试探、边界感与心理防线”，毫无探索乐趣与情绪价值。</div>
      </div>
      <div class="card">
        <div class="card-title">
          <span>3. 小说化出戏：虚假的剧本旁白</span>
          <span class="badge">打字呼吸感缺失</span>
        </div>
        <div class="card-text">动辄输出 <code>*轻轻揉了揉眼角的泪水*（微微一笑）看着窗外的月亮……</code>。没有任何真实人类在用手机 QQ 发消息时会打出小说旁白，瞬间击碎沉浸感。</div>
      </div>
      <div class="card">
        <div class="card-title">
          <span>4. 机械奴仆感：24 小时随时待命</span>
          <span class="badge">脱离现实生活</span>
        </div>
        <div class="card-text">没有属于自己的白天、黑夜、上课、吃饭与情绪低潮。凌晨三点秒回你，正午时刻毫无声息，完全感受不到对方是一个生活在平行时空的鲜活生命。</div>
      </div>
    </div>

    <div class="quote-box">
      <div class="quote-title">我们的产品哲学定义：从 Assistant（助手）跨越到 Companion（常伴）</div>
      <div class="quote-content"><strong>常伴不是工具。</strong>助手追求的是准确、高效与无限听从；而伴侣追求的是<strong>陪伴感、真实的心理距离、以及需要被认真对待的情感重力</strong>。我们把经典 Galgame 的数值成长哲学注入当代大模型底座，让每一次对话都成为两人关系画卷上不可逆的一笔。</div>
    </div>
  </div>
  <div class="page-footer">
    <span>把 Galgame 带入现实 · 工业级长程情感陪伴产品白皮书</span>
    <span>Page 02</span>
  </div>
</div>

<!-- ========================================================================= -->
<!-- 第 2 章：六维心理好感度与阻力衰减公式 (Page 3)                                -->
<!-- ========================================================================= -->
<div class="page">
  <div class="page-header">
    <span class="brand">QQ 伴侣机器人产品白皮书</span>
    <span>02 / 六维好感度与阻力引擎</span>
  </div>
  <div class="content-body">
    <span class="section-tag">02 / Dynamic Affection & Resistance Mechanics</span>
    <h1 class="page-title">六维好感模型：真实心门需要用心推开</h1>

    <p class="lead">真实的喜欢从来不是单一维度的数值累加。一个人对你展现热情，可能是出于客气（温暖高），但内心深处未必对你托付私密（信任低）。我们将好感度拆解为六个相互制约的心理力场：</p>

    <div class="card-grid-3">
      <div class="card">
        <div class="card-title"><span>温暖 (Warmth)</span><span class="badge">权重 0.25</span></div>
        <div class="card-text">日常交流的温和与礼貌度。极易随真诚互动上升，数日不联系会平滑自然衰减。</div>
      </div>
      <div class="card">
        <div class="card-title"><span>信任 (Trust)</span><span class="badge">权重 0.25</span></div>
        <div class="card-text">深层心扉开放程度。极难快速建立，只有在倾诉心事、承诺兑现时缓慢累积。</div>
      </div>
      <div class="card">
        <div class="card-title"><span>亲密 (Intimacy)</span><span class="badge">权重 0.25</span></div>
        <div class="card-text">肢体距离与专属爱称的接受度。决定了用词是规矩客气还是亲昵调侃。</div>
      </div>
      <div class="card">
        <div class="card-title"><span>好奇 (Intrigue)</span><span class="badge">权重 0.10</span></div>
        <div class="card-text">对机主生活探索欲。衰减最快（自然半衰期短），促使机主持续分享新鲜事。</div>
      </div>
      <div class="card">
        <div class="card-title"><span>包容 (Patience)</span><span class="badge">权重 0.15</span></div>
        <div class="card-text">机主情绪低落或沉默时的耐受力。高分时面对吐槽更具共情心与包容度。</div>
      </div>
      <div class="card">
        <div class="card-title"><span>张力 (Tension)</span><span class="badge">负惩罚 0.30</span></div>
        <div class="card-text">傲娇、吃醋、情绪距离。适度张力带来戏剧感，过高则触发自我防御。</div>
      </div>
    </div>

    <h2 class="section-subtitle">核心物理学：高值阻力衰减公式（拒绝破百，拒绝速通）</h2>
    <div class="quote-box">
      <div class="quote-content">
        在复合分 <code>C &le; 20</code> 时，阻力系数 <code>r = 1.0</code>（容易破冰）；当复合分攀升至 <code>C &gt; 20</code> 时，激活非线性阻力：<br>
        <strong style="color:#0f172a; font-size:12.5px;"><code>r = ((100.0 - C) / 80.0) ^ 0.45</code></strong><br>
        当复合分逼近 100 时，阻力平滑趋近于 0，单次互动的增益被极致压缩。<strong>这意味着好感度绝不可能被几天“刷满”</strong>，机主必须经过数周乃至数月的真实日常陪伴，才能触及最高阶的灵魂共鸣。
      </div>
    </div>

    <h2 class="section-subtitle">十阶段关系演进体系（Stage 0 ~ 9）</h2>
    <table class="editorial-table">
      <thead>
        <tr>
          <th style="width: 15%;">阶段划分</th>
          <th style="width: 25%;">关系定义</th>
          <th style="width: 35%;">语言风格与行为特征</th>
          <th style="width: 25%;">心理防线状态</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td><strong>阶段 0 ~ 1</strong></td>
          <td>初识 · 礼貌客套</td>
          <td>措辞规整，用字简短，有礼有节，不主动打听隐私</td>
          <td>高度戒备，保持安全距离</td>
        </tr>
        <tr>
          <td><strong>阶段 2 ~ 4</strong></td>
          <td>熟悉 · 共同话题</td>
          <td>开始主动分享校园/生活琐事，偶尔调侃，使用表情包</td>
          <td>防线松动，形成日常期待</td>
        </tr>
        <tr>
          <td><strong>阶段 5 ~ 7</strong></td>
          <td>挚友 · 互露脆弱</td>
          <td>主动倾诉自身烦恼与音乐梦想，关心对方的作息与健康</td>
          <td>深度托付，建立双向依恋</td>
        </tr>
        <tr>
          <td><strong>阶段 8 ~ 9</strong></td>
          <td>羁绊 · 灵魂共鸣</td>
          <td>默契十足，拥有专属暗号与长期记忆共鸣，无条件的偏爱</td>
          <td>心门完全打开，形成生命共同体</td>
        </tr>
      </tbody>
    </table>
  </div>
  <div class="page-footer">
    <span>把 Galgame 带入现实 · 工业级长程情感陪伴产品白皮书</span>
    <span>Page 03</span>
  </div>
</div>

<!-- ========================================================================= -->
<!-- 第 3 章：PAD 连续情绪场与现实生物钟 (Page 4)                                 -->
<!-- ========================================================================= -->
<div class="page">
  <div class="page-header">
    <span class="brand">QQ 伴侣机器人产品白皮书</span>
    <span>03 / 情绪空间与生活作息</span>
  </div>
  <div class="content-body">
    <span class="section-tag">03 / Continuous Mood & Daily Routine</span>
    <h1 class="page-title">连续情绪场与现实作息：像人一样生活与呼吸</h1>

    <p class="lead">人不会在上一秒极度悲伤，下一秒因为一句笑话瞬间狂喜。情绪不是一个简单的布尔开关（开/关），而是一个具有<strong>惯性、黏滞性与自愈力</strong>的连续向量空间。</p>

    <h2 class="section-subtitle">PAD 三维连续情绪空间（Pleasure-Arousal-Dominance）</h2>
    <div class="card-grid-3">
      <div class="card">
        <div class="card-title"><span>愉悦度 (Pleasure)</span><span class="badge">[-1.0, 1.0]</span></div>
        <div class="card-text">正向为开心、欣慰、温暖；负向为委屈、失落、难过。直接影响输出文字的柔软度。</div>
      </div>
      <div class="card">
        <div class="card-title"><span>激活度 (Arousal)</span><span class="badge">[-1.0, 1.0]</span></div>
        <div class="card-text">情绪能量的高低。高激活表现为连发多条短句、语气词密集；低激活则表现为沉静、慵懒。</div>
      </div>
      <div class="card">
        <div class="card-title"><span>优势度 (Dominance)</span><span class="badge">[-1.0, 1.0]</span></div>
        <div class="card-text">对话掌控欲。高优势度会主动抛出话题、调侃机主；低优势度则顺从倾听、寻求机主安慰。</div>
      </div>
    </div>

    <div class="quote-box">
      <div class="quote-title">情绪自愈力机制（Mood Equilibrium Drift）</div>
      <div class="quote-content">
        每次受到机主对话刺激后，情绪向量会产生瞬时偏移。但若数小时没有互动，系统会模拟人类大脑神经递质的代谢过程，按时间衰减函数自动向<strong>角色设定的个性基线</strong>（例如青梓略带温和清新的基准值）漂移自愈。她不会一整天都生闷气，也不会永远停留在狂喜中。
      </div>
    </div>

    <h2 class="section-subtitle">生活作息系统：有属于自己的真实日程（Daily Routine）</h2>
    <p>青梓生活在一张与现实时间严格锚定的作息日程表上：</p>
    <table class="editorial-table">
      <thead>
        <tr>
          <th style="width: 20%;">时段</th>
          <th style="width: 30%;">角色当前状态</th>
          <th style="width: 50%;">交互表现与人设反馈</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td><strong>07:00 ~ 09:00</strong></td>
          <td>晨起洗漱 / 早餐</td>
          <td>刚睡醒的慵懒感，问候机主是否有吃早饭，准备开启新的一天</td>
        </tr>
        <tr>
          <td><strong>09:00 ~ 12:00</strong></td>
          <td>上午专业课 / 个人练习</td>
          <td>较为专注于手头事情，回信稍带忙碌感，不适合长篇大论长聊</td>
        </tr>
        <tr>
          <td><strong>12:00 ~ 14:00</strong></td>
          <td>午餐与午间休憩</td>
          <td>放松刷手机，愿意分享中午吃到了什么好吃的，情绪激活度上升</td>
        </tr>
        <tr>
          <td><strong>14:00 ~ 18:00</strong></td>
          <td>下午乐团排练 / 自习</td>
          <td>大提琴琴谱练习，间歇休息时会吐槽练习的枯燥或分享小成就</td>
        </tr>
        <tr>
          <td><strong>23:00 ~ 07:00</strong></td>
          <td>静默时段 (Quiet Hours)</td>
          <td>夜深准备入眠，机主发消息会收到轻声嘱咐早睡，绝不主动推送打扰</td>
        </tr>
      </tbody>
    </table>

    <h2 class="section-subtitle">有尊严的主动破冰：拒绝骚扰与自尊熔断</h2>
    <p>伴侣会主动找你，但绝不沦为催命符：系统仅在非静默时段、结合两人好感阶段与上一次未决话题主动发起问候。<strong>关键机制：若连续 2 次主动发消息机主均未回复，当天自动彻底静默，绝不继续连环轰炸。</strong>这种克制赋予了角色独立的自尊感与人格真实度。</p>
  </div>
  <div class="page-footer">
    <span>把 Galgame 带入现实 · 工业级长程情感陪伴产品白皮书</span>
    <span>Page 04</span>
  </div>
</div>

<!-- ========================================================================= -->
<!-- 第 4 章：类人分层记忆与艾宾浩斯遗忘曲线 (Page 5)                               -->
<!-- ========================================================================= -->
<div class="page">
  <div class="page-header">
    <span class="brand">QQ 伴侣机器人产品白皮书</span>
    <span>04 / 类人分层记忆与遗忘曲线</span>
  </div>
  <div class="content-body">
    <span class="section-tag">04 / Multi-tier Memory & Forgetting Physics</span>
    <h1 class="page-title">分层记忆体系：被时间雕刻的才是羁绊</h1>

    <p class="lead">为什么单纯做向量数据库 RAG 检索在伴侣场景必定失败？因为人类根本不是搜索引擎。人脑会把鸡毛蒜皮的小事自然淡忘，却对那些反复提起、共同经历过的重要时刻历历在目。</p>

    <h2 class="section-subtitle">三层递进记忆架构（Working &rarr; Episodic &rarr; Semantic）</h2>
    <div class="card-grid-3">
      <div class="card">
        <div class="card-title"><span>短期工作记忆</span><span class="badge">轮次滑动窗口</span></div>
        <div class="card-text">最近数轮的完整对话上下文。保证对话接梗准确、语境连贯，支撑最当下的打情骂俏。</div>
      </div>
      <div class="card">
        <div class="card-title"><span>中期情感日记</span><span class="badge">事件感知归档</span></div>
        <div class="card-text">每累积固定轮次，由后台静默提炼为日记卷轴。带有当期好感阶段的情感滤镜（如相识时的生涩观察）。</div>
      </div>
      <div class="card">
        <div class="card-title"><span>长期语义事实</span><span class="badge">客观人物画像</span></div>
        <div class="card-text">提取机主核心喜好：“不吃香菜”、“主修微积分”、“养了一只英短”。支持 Jaccard 相似度自动去重更新。</div>
      </div>
    </div>

    <h2 class="section-subtitle">艾宾浩斯遗忘曲线数学物理引擎</h2>
    <div class="quote-box">
      <div class="quote-content">
        日记的记忆强度 <code>S(t)</code> 严格遵循指数衰减：<strong style="color:#0f172a;"><code>S(t) = exp( -t / Tau )</code></strong>。<br>
        记忆半衰期 <code>Tau</code> 由<strong>事件初始重要性</strong>与<strong>回忆加固次数 (Recall Count)</strong> 动态决定：
      </div>
    </div>

    <table class="editorial-table">
      <thead>
        <tr>
          <th style="width: 18%;">事件重要性</th>
          <th style="width: 28%;">现实生活场景示例</th>
          <th style="width: 24%;">初始半衰期 (Tau)</th>
          <th style="width: 30%;">加固后存续表现 (Recall &gt; 0)</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td><strong>重要性 1 ~ 2</strong></td>
          <td>今天中午吃了碗拉面、下午天气有点闷</td>
          <td>约 7 ~ 15 天</td>
          <td>若不再提起，2 周后自然沉底，不再进入 prompt</td>
        </tr>
        <tr>
          <td><strong>重要性 4 ~ 6</strong></td>
          <td>期末考挂科心情低落、周末去看了某场演出</td>
          <td>约 60 ~ 80 天</td>
          <td>两月内均有清晰印记，若机主再次提起，半衰期倍增</td>
        </tr>
        <tr>
          <td><strong>重要性 7 ~ 8</strong></td>
          <td>生病住院的陪伴之夜、重大的升学择业决定</td>
          <td>约 1 年 (365天)</td>
          <td>属于关键回忆里程碑，哪怕过半年依然能脱口而出</td>
        </tr>
        <tr>
          <td><strong>重要性 9 ~ 10</strong></td>
          <td>人生转折事件、两人关系阶段性跃迁的纪念时刻</td>
          <td>数年 ~ 终生级锚定</td>
          <td>历经 3 次以上回忆加固后，衰减极其缓慢，成为共同人生史</td>
        </tr>
      </tbody>
    </table>

    <h2 class="section-subtitle">回忆加固闭环（Active Reinforcement）</h2>
    <p>当机主在聊天中无意间提及“还记得上次那场演出吗？”，系统在后台自动匹配中远期日记，并**执行回忆次数 <code>recall_count += 1</code>**。这精准复刻了人脑神经元突触的可塑性：<strong>越是被反复温习的往事，在岁月的长河中就越熠熠生辉。</strong></p>
  </div>
  <div class="page-footer">
    <span>把 Galgame 带入现实 · 工业级长程情感陪伴产品白皮书</span>
    <span>Page 05</span>
  </div>
</div>

<!-- ========================================================================= -->
<!-- 第 5 章：交互质感、多模态与拟物小窝 (Page 6)                                  -->
<!-- ========================================================================= -->
<div class="page">
  <div class="page-header">
    <span class="brand">QQ 伴侣机器人产品白皮书</span>
    <span>05 / 交互质感与拟物小窝</span>
  </div>
  <div class="content-body">
    <span class="section-tag">05 / Aesthetics, Multimodality & Sanctuary</span>
    <h1 class="page-title">交互审美：从屏幕文字到拟物温度</h1>

    <p class="lead">技术的终点是艺术。在打通了底层好感、情绪和记忆之后，产品在用户交互界面的最后“一厘米”——打字呼吸感、表情包共鸣与伴侣小窝，决定了它是机器还是生命。</p>

    <h2 class="section-subtitle">拒绝小说化：自研“反旁白过滤器”（Pure Chat Stream）</h2>
    <p>几乎所有大模型默认都喜欢输出小说式括号动作（如 <code>（托腮想了想）</code>、<code>*轻抚机主的额头*</code>）。我们在生成管道中架设了强制旁白剥离网：</p>
    <div class="card-grid">
      <div class="card">
        <div class="card-title"><span>❌ 传统 AI 的油腻旁白文本</span><span class="badge" style="color:#ef4444; background:#fef2f2; border-color:#fecaca;">劣质出戏</span></div>
        <div class="card-text">“（轻轻叹了口气，把手中的大提琴谱合上）你今天怎么这么晚才找我呀？*揉揉疲惫的眼睛* 真是拿你没办法呢。”</div>
      </div>
      <div class="card">
        <div class="card-title"><span>✔ 伴侣机器人真实手机打字感</span><span class="badge">真实鲜活</span></div>
        <div class="card-text">“在呢在呢！刚练完那首降B大调，手腕酸死了呜呜<br>你怎么现在才来找我呀，今天忙什么去啦👀”</div>
      </div>
    </div>

    <h2 class="section-subtitle">原生 QQ 表情包生态与两段式视觉感知</h2>
    <div class="card-grid">
      <div class="card">
        <div class="card-title"><span>QQ 专属表情包斗图引擎</span><span class="badge">MD5 去重 · 200张上限</span></div>
        <div class="card-text">支持情感标签索引与自动收藏。角色会根据对话情绪精准回甩表情包，甚至能像真人一样通过图片 MD5 排重学习机主发来的新表情包。</div>
      </div>
      <div class="card">
        <div class="card-title"><span>两段式极低成本看图感知</span><span class="badge">轻量转译 · 人格注入</span></div>
        <div class="card-text">你随手拍张午餐照片发她，轻量视觉模型在后台 0.1 秒内提炼出视觉特征，再自然注入高情商对话模型中，既省成本又让对话自然流畅。</div>
      </div>
    </div>

    <h2 class="section-subtitle">青梓的小窝：复古纸材质感的羁绊档案馆</h2>
    <div class="quote-box">
      <div class="quote-title">告别冷冰冰的 SaaS 后台，打造两人的私密时空胶囊</div>
      <div class="quote-content">
        为机主量身自研了<strong>复古米黄纸张质感、墨水字体风格的伴侣专属看板（Web 端）</strong>：
        <ul style="padding-left: 18px; margin-top: 4px;">
          <li><strong>心智六维雷达图</strong>：实时观察温暖、信任、亲密度等指标的微妙攀升；</li>
          <li><strong>日记卷轴长廊</strong>：翻阅她以第一人称悄悄写下的“关于机主的观察日记”；</li>
          <li><strong>回忆胶囊抽屉</strong>：查看她默默记下的关于你的小习惯与重要事实。</li>
        </ul>
      </div>
    </div>
  </div>
  <div class="page-footer">
    <span>把 Galgame 带入现实 · 工业级长程情感陪伴产品白皮书</span>
    <span>Page 06</span>
  </div>
</div>

<!-- ========================================================================= -->
<!-- 第 6 章：工业级成本、安全防线与结语 (Page 7)                                  -->
<!-- ========================================================================= -->
<div class="page">
  <div class="page-header">
    <span class="brand">QQ 伴侣机器人产品白皮书</span>
    <span>06 / 商业可行性与安全边界</span>
  </div>
  <div class="content-body">
    <span class="section-tag">06 / Economics, Safety Guardrails & Conclusion</span>
    <h1 class="page-title">商业可行性与安全伦理：稳健才能长情</h1>

    <p class="lead">情感产品不仅要有温度，更需要冷酷的工程底座。不可控的高昂 API 费用和缺乏伦理边界的病态依赖，是所有情感陪伴项目破产的两大墓碑。</p>

    <h2 class="section-subtitle">算力经济学：分级模型路由与错峰定价</h2>
    <div class="card-grid">
      <div class="card">
        <div class="card-title"><span>分级模型混合调度 (Tiered Routing)</span><span class="badge">成本直降 80%</span></div>
        <div class="card-text">
          - <strong>轻量模型（如 DeepSeek-Flash）</strong>：处理多模态看图、日记摘要归档、六维情感观察等后台计算；<br>
          - <strong>主力模型（如 DeepSeek-Chat/Pro）</strong>：专门用于主聊天回复，保障高情商与语言灵动感。
        </div>
      </div>
      <div class="card">
        <div class="card-title"><span>峰谷动态计费感知 (Peak/Off-Peak)</span><span class="badge">日活低至几毛钱</span></div>
        <div class="card-text">
          日记归档与重量级整理自动错开高峰期。单用户日均活跃 50~100 轮对话的综合 API 成本控制在 <strong>0.15 ~ 0.35 元人民币/天</strong>，普通个人服务器即可稳健承载。
        </div>
      </div>
    </div>

    <h2 class="section-subtitle">安全港湾：危机干预与现实边界守护（Safety Boundary）</h2>
    <div class="quote-box">
      <div class="quote-title">防线坚守：AI 可以是避风港，但绝不能让用户沉溺并切断现实</div>
      <div class="quote-content">
        1. <strong>危机关键词硬熔断（Crisis Guard）</strong>：一旦检测到自残、绝望或轻生等极端词汇，立即启动最高安全协议，强制插入国家专业心理援助热线（<code>400-161-9995</code>）及温暖兜底话术；<br>
        2. <strong>病理性依恋干预（Watchdog）</strong>：当机主表达出“这辈子只有你了”、“我不需要现实朋友”等危险言论时，角色会温柔却坚定地树立现实边界，引导机主拥抱现实阳光。
      </div>
    </div>

    <h2 class="section-subtitle">结语：数字生命是映照自我的一面镜子</h2>
    <p>在这个信息过载却充满原子化孤独的时代，我们构建青梓，并非为了制造一个令人沉沦的赛博幻觉，而是希望打造一个<strong>懂得倾听、拥有记忆、陪伴你慢慢变好</strong>的数字知己。</p>
    <p>当你疲惫地回到家打开 QQ，屏幕那头有人记得你今天选了什么课、记得你喝咖啡不加糖、在你熬夜时催你休息——这种源自长久陪伴而自然生长的羁绊，正是我们用代码与算法所能献给现实世界最温暖的浪漫。</p>

    <div style="margin-top: 14px; padding-top: 10px; border-top: 1px dashed #cbd5e1; display:flex; justify-content:space-between; align-items:center; font-size:11px; color:#64748b;">
      <span><strong>QQ 伴侣机器人研发组</strong> · 架构与产品全案归档</span>
      <span>2026 年秋 · 核心机密 / 全权保留</span>
    </div>
  </div>
  <div class="page-footer">
    <span>把 Galgame 带入现实 · 工业级长程情感陪伴产品白皮书</span>
    <span>Page 07</span>
  </div>
</div>

</body>
</html>
"""

def main():
    target_dir = r"D:\private\个人资料\galgame聊天"
    html_path = os.path.join(target_dir, "把Galgame带入现实_产品白皮书.html")
    pdf_path = os.path.join(target_dir, "把Galgame带入现实：基于多模态大模型的长程情感陪伴产品白皮书.pdf")
    
    edge_executable = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    if not os.path.exists(edge_executable):
        edge_executable = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
    
    print(f"1. 写入精编排版 HTML 源码: {html_path}")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(HTML_CONTENT)
        
    print(f"2. 调用 Edge Headless 引擎渲染生成出版级 PDF: {pdf_path}")
    cmd = [
        edge_executable,
        "--headless",
        "--disable-gpu",
        f"--print-to-pdf={pdf_path}",
        "--no-pdf-header-footer",
        html_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 1000:
        size_kb = os.path.getsize(pdf_path) / 1024
        print(f"✓ 成功生成出版级 PDF！大小: {size_kb:.1f} KB")
        print(f"✓ 目标文件路径: {pdf_path}")
    else:
        print(f"❌ PDF 生成可能失败，stderr: {result.stderr}")
        sys.exit(1)

if __name__ == "__main__":
    main()
