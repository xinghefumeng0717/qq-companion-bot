"""对话内核的上下文构建器 (context.py)

第一阶段离线纯文字内核的上下文构建组件。
只负责组装面向 LLM gateway.stream_chat 的标准 messages 格式，
不包含任何角色/世界观硬编码，不改动旧主链路或角色卡。
"""
from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

from companion.datefmt import relativize_dates
from companion.lorebook import BLOCK_HEADER
from companion.kernel.semantic import semantic_history_view
from companion.kernel.contracts import (
    IncomingMessage,
    MemoryPromptBlocks,
    PsychologicalSnapshot,
    StandardizedTurnInput,
    StyleExampleSelection,
)
from companion.prompts import holiday_prompt_note

# 动作、多模态及内部协议标记的只读过滤正则（不改动原始角色卡）
# 1. 协议方括号：[sticker:...]、[face:...]、[表情:...]、[quote:...]、[voice:...]...[/voice]、[沉默]、[吃糖]、[label:...] 等
_BRACKET_ACTION_RE = re.compile(
    r"\[(?:sticker|face|表情|quote|voice|label)[:：][^\]]+\]"
    r"|\[/voice\]"
    r"|\[(?:沉默|吃糖|图片|照片)\]",
    re.IGNORECASE,
)

# 固定通用聊天准则（与角色卡无关，所有角色共用）
# 说明：这里只放通用约束，禁止写入任何角色名、作品名或专属设定。
FIXED_GUIDELINES = (
    "只输出你真实发出的聊天纯文字内容，绝对不要输出任何小说式神态描写、动作描写、心理旁白或括号补充。",
    "只写自然聊天内容；语音、表情、引用和是否回复都由系统处理，不要输出任何控制标记。",
)

# 明确动作描写词表：涵盖常见神态表情、肢体动作与物体交互，避免误删普通解释或英文/数字
_ACTION_VERBS = (
    # 面部表情与声气神态
    "笑", "叹", "哭", "泣", "哼", "咳", "喘", "愣", "怔", "嗔", "抿", "撇", "瞪", "瞥", "望", "瞅", "瞧", "眯", "眨",
    # 肢体动作
    "摇头", "点头", "挥手", "招手", "摆手", "耸肩", "敬礼", "鞠躬", "欠身", "拱手", "作揖",
    "转身", "扭头", "回头", "低头", "抬头", "侧头", "歪头", "探头", "仰头",
    "捂脸", "捂嘴", "抚胸", "摸头", "挠头", "揉眼", "揉脸", "擦眼", "抹泪", "擦汗", "抹汗", "吐舌", "咬唇", "咬牙", "托腮", "皱眉", "挑眉",
    "跺脚", "握拳", "摊手", "拍手", "拍胸", "拍肩", "拍了拍", "捶胸", "鼓掌",
    "伸懒腰", "深呼吸", "倒吸", "打寒颤", "缩脖", "耸耸肩", "眨了眨眼", "眨眨眼", "叹了口气", "叹气", "叹息",
    "起身", "坐下", "站起", "躺下", "趴下", "蹲下",
    # 交互与物品动作
    "递", "拿", "掏", "摸", "揉", "抓", "捏", "抱", "扯", "拉", "推", "扶", "塞", "凑", "贴",
    "拿出", "掏出", "递给", "递上", "递过", "递来", "拿起", "端起", "放下", "倒水", "倒茶", "喝水",
    "移开视线", "移开目光", "别开视线", "翻白眼", "白了一眼", "闭眼", "睁眼",
    # 走动位移
    "走近", "走开", "离开", "小跑", "退后", "后退", "迈步", "踱步", "凑近", "靠过来", "退开", "蹦", "跳", "跑", "晃", "跃", "扑", "闪", "跨", "踏",
    # 语气耳语神态
    "小声", "轻声", "嘀库", "嘀咕", "嘟囔", "喃喃", "语塞",
)

# 显式解释与说明标记：遇到此类标记一律保留，不当作动作误删
_EXPLANATORY_MARKERS = ("例如", "比如", "说明", "解释", "注", "提示", "指", "即", "详见", "推荐", "可选", "默认", "单位", "开玩笑")


def _is_action_narration(inner: str) -> bool:
    """判断括号或星号内的文本是否为明确动作描写/旁白。"""
    inner = inner.strip()
    if not inner:
        return False
    # 纯数字、算术、版本号、符号（例如 1, 100, 2024, 2*3, v1.0, 3.14 等）
    if re.fullmatch(r"[\d\s+\-*/=.,:;%#@_~^()\[\]{}]+", inner):
        return False
    # 纯英文及常见标点符号（例如 v1.0, PDF, hello, OK, API 等）
    if re.fullmatch(r"[A-Za-z0-9\s._\-+=/:;!?,'\"()\[\]]+", inner):
        return False
    # 包含明确解释性标记
    if any(inner.startswith(m) for m in ("注", "例", "比如", "即", "指", "说明", "提示", "推荐", "可选", "默认", "单位")):
        return False
    if any(m in inner for m in _EXPLANATORY_MARKERS):
        return False
    # 命中动作动词
    return any(v in inner for v in _ACTION_VERBS)


# 2. 示例触发标签：【起】/【接】/【收】
_DIRECTION_TAG_RE = re.compile(r"^[ \t]*【[起收接]】[ \t]*", re.MULTILINE)

# 3. 连续多余空白与空行清理
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


def filter_action_tags(text: str) -> str:
    """在只读视图中过滤文本内的明确动作、协议标记及旁白描写，不修改原卡与源数据。

    保留算术表达式（如 2*3*4）、普通数字/英文括号以及普通解释说明括号。
    """
    if not text:
        return ""
    # 剥离触发标签
    cleaned = _DIRECTION_TAG_RE.sub("", text)
    # 剥离语音成对块内部的内容或标记 [voice:]...[/voice] -> 提取内层纯文字
    cleaned = re.sub(
        r"\[voice[:：]\]?\s*([^\[\]]*?)\s*\]?\s*\[/voice\]",
        r"\1",
        cleaned,
        flags=re.IGNORECASE,
    )
    # 剥离各类协议方括号标记
    cleaned = _BRACKET_ACTION_RE.sub("", cleaned)
    # 剥离明确动作描写（括号与星号），保留算术与普通解释
    def _repl(m: re.Match) -> str:
        inner = m.group(1) or m.group(2) or m.group(3) or ""
        if _is_action_narration(inner):
            return ""
        return m.group(0)

    cleaned = re.sub(
        r"（([^（）\n]{1,40})）|\(([^()\n]{1,40})\)|\*([^*\n]{1,40})\*",
        _repl,
        cleaned,
    )
    # 清理多余空行与两端空白
    cleaned = _MULTI_NEWLINE_RE.sub("\n\n", cleaned).strip()
    return cleaned

_STYLE_ACTION_WORDS = (
    "[voice", "[face", "[sticker", "[quote", "[沉默",
    "语音", "表情包", "小黄脸", "沉默",
)


def filter_style_rule(text: str) -> str:
    """只剔除动作协议分句，保留同条规则里的有效文风要求。"""
    cleaned = filter_action_tags(text)
    if not cleaned:
        return ""
    pieces = re.split(r"([。；;！!？?，,\n])", cleaned)
    kept: List[str] = []
    for index in range(0, len(pieces), 2):
        statement = pieces[index]
        punctuation = pieces[index + 1] if index + 1 < len(pieces) else ""
        if any(word in statement for word in _STYLE_ACTION_WORDS):
            continue
        if statement.strip():
            kept.append(statement + punctuation)
    return "".join(kept).strip()


# 星期口径（与旧 assembler.WEEKDAYS 同一套说法：星期一…星期日）
_WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")

# 聊天间隔门槛：与旧 assembler.gap_note 的 GAP_NOTE_HOURS 同一把尺子。
# 不足这个间隔就不提"距上次聊天"，免得模型被无意义的秒级间隔干扰。
GAP_NOTE_HOURS = 3.0


def format_chat_gap(hours: float) -> str:
    """把距上次聊天的小时数按旧 gap_note 的向下取整口径写成一句话。

    旧口径：days, rest = divmod(int(hours), 24)，
    "X 天 Y 小时" / "X 天" / "X 小时" 三种形态。
    """
    total = int(hours)  # 向下取整，与旧实现一致
    days, rest = divmod(total, 24)
    if days and rest:
        span = f"{days} 天 {rest} 小时"
    elif days:
        span = f"{days} 天"
    else:
        span = f"{rest} 小时"
    return f"距上次聊天已过 {span}"


def _safe_gap_hours(value: Any) -> Optional[float]:
    """把 action_context 里的间隔小时数洗成安全的有限浮点数。

    NaN / inf / 负数 / 非数值一律返回 None（当作没有记录），
    绝不把原始浮点值泄漏进提示词。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(hours):
        return None
    if hours < 0:
        return None
    return hours


class ContextBuilder:
    """负责纯文本离线模式下组装 messages 的构建器。"""

    def __init__(self, max_examples: Optional[int] = None, max_history_turns: int = 10):
        self.max_examples = max_examples
        self.max_history_turns = max_history_turns

    def _resolve_now(
        self,
        snapshot: PsychologicalSnapshot,
        turn_input: Optional[Union[StandardizedTurnInput, str]],
    ) -> datetime:
        """本轮时间基准：优先用输入层的 received_at，其次用快照冻结时间。"""
        received = getattr(turn_input, "received_at", None)
        if isinstance(received, datetime):
            return received
        frozen = getattr(snapshot, "frozen_at", None)
        if isinstance(frozen, datetime):
            return frozen
        return datetime.now()

    def _build_time_block(
        self,
        persona: Any,
        snapshot: PsychologicalSnapshot,
        now_dt: datetime,
    ) -> str:
        """组装独立的【本轮时间】事实块。

        只放客观事实（日期/时刻/星期、法定节假日、距上次聊天、活动状态），
        不写命令、不改用户正文、不塞进 user message。
        睡眠与当前聊天的冲突由快照里的 effective_activity / prompt_hint 仲裁，
        这里只原样展示仲裁结论，绝不出现"正在睡着"却又同时在聊天的自相矛盾写法。
        """
        context = getattr(snapshot, "action_context", None) or {}
        if not isinstance(context, Mapping):
            context = {}

        weekday = _WEEKDAYS[now_dt.weekday()]
        header = f"当前日期：{now_dt.strftime('%Y-%m-%d')} {now_dt.strftime('%H:%M')}（星期{weekday}）"

        # 法定节假日：口径与旧链路完全一致（prompts.holiday_prompt_note + 角色卡 world）
        try:
            span = int(context.get("holiday_span", 0) or 0)
        except (TypeError, ValueError):
            span = 0
        if span > 0:
            try:
                note = holiday_prompt_note(span, getattr(persona, "world", None)) or ""
            except Exception:
                note = ""
            if note:
                header += note

        lines = [header]

        # 距上次聊天：不足 3 小时（或没有记录）一律不提，绝不泄漏原始浮点值
        hours = _safe_gap_hours(context.get("hours_since_chat"))
        if hours is not None and hours >= GAP_NOTE_HOURS:
            lines.append(format_chat_gap(hours))

        # 活动状态：直接展示快照里已经仲裁好的 effective_activity（为空才回退作息表）。
        # 这里不再根据 is_chatting / is_sleeping_routine 自行写特判——
        # "聊天时到底是睡着还是醒着"的仲裁只留在 psychology，这里只负责展示。
        activity = str(context.get("effective_activity") or "").strip()
        if not activity:
            activity = str(context.get("routine_activity") or "").strip()
        if activity:
            lines.append(f"活动状态：{activity}")

        return "\n".join(lines)

    def _collect_lore_texts(
        self,
        turn_input: Optional[Union[StandardizedTurnInput, str]],
        history: Optional[Sequence[Union[Dict[str, Any], Mapping[str, Any]]]],
        scan_turns: int,
    ) -> List[str]:
        """收集当前输入与近期历史供世界书词条检索。"""
        texts: List[str] = []
        if turn_input:
            if isinstance(turn_input, str):
                if turn_input.strip():
                    texts.append(turn_input.strip())
            else:
                text = getattr(turn_input, "text", "")
                if text and str(text).strip():
                    texts.append(str(text).strip())
                quoted = getattr(turn_input, "quoted_text", "")
                if quoted and str(quoted).strip():
                    texts.append(str(quoted).strip())

        if history and scan_turns > 0:
            for item in history[-scan_turns:]:
                content = (
                    item.get("content")
                    or item.get("semantic_text")
                    or item.get("readable_text")
                    or item.get("text")
                    or ""
                )
                if content and str(content).strip():
                    texts.append(str(content).strip())
        return texts

    def _build_dynamic_lore_block(
        self,
        lorebook: Any,
        texts: Sequence[str],
    ) -> str:
        """根据输入与近期文本动态检索世界书 entries 并拼装提示词块。"""
        if not lorebook or not texts:
            return ""
        # 1. 优先调用现有 lorebook.build_block 方法
        if hasattr(lorebook, "build_block") and callable(lorebook.build_block):
            try:
                block = lorebook.build_block(texts)
                return block or ""
            except Exception:
                pass
        # 2. 备选调用 match 方法
        if hasattr(lorebook, "match") and callable(lorebook.match):
            try:
                picked = lorebook.match(texts)
                if picked:
                    return BLOCK_HEADER + "".join(f"\n  - {getattr(e, 'content', str(e))}" for e in picked)
            except Exception:
                pass
        # 3. 鸭子类型兼容 entries 列表（支持 dict 或数据类词条）
        entries = getattr(lorebook, "entries", None)
        if not entries:
            return ""
        hay = "\n".join(t for t in texts if t).lower()
        if not hay:
            return ""
        hits = []
        for idx, e in enumerate(entries):
            keys = getattr(e, "keys", None) or (e.get("keys") if isinstance(e, dict) else [])
            if keys and any(str(k).lower() in hay for k in keys):
                priority = getattr(e, "priority", 0) if hasattr(e, "priority") else (e.get("priority", 0) if isinstance(e, dict) else 0)
                hits.append((idx, e, priority))
        if not hits:
            return ""
        hits.sort(key=lambda x: (-x[2], x[0]))
        max_entries = getattr(lorebook, "max_entries", 4)
        max_chars = getattr(lorebook, "max_chars", 1200)
        out = []
        used = 0
        for _, e, _ in hits:
            content = getattr(e, "content", "") if hasattr(e, "content") else (e.get("content", "") if isinstance(e, dict) else "")
            if len(out) >= max_entries or used + len(content) > max_chars:
                continue
            out.append(content)
            used += len(content)
        if not out:
            return ""
        return BLOCK_HEADER + "".join(f"\n  - {c}" for c in out)

    def build_system_prompt(
        self,
        persona: Any,
        snapshot: PsychologicalSnapshot,
        turn_input: Optional[Union[StandardizedTurnInput, str]] = None,
        history: Optional[Sequence[Union[Dict[str, Any], Mapping[str, Any]]]] = None,
        memory_blocks: Optional[MemoryPromptBlocks] = None,
        style_examples: Optional[StyleExampleSelection] = None,
    ) -> str:
        """构建只包含角色核心、世界书、精简风格、心理提示和当前关系阶段的系统提示词。

        不得注入 voice/face/quote/sticker/沉默协议或完整动作字典。
        """
        sections: List[str] = []
        # 本轮时间基准：输入层 received_at 优先，其次快照冻结时间。
        now_dt = self._resolve_now(snapshot, turn_input)

        # 1. 角色核心设定与身份
        name = getattr(persona, "name", "角色")
        core_desc = getattr(persona, "core_description", "")
        role_header = f"你是{name}。" if name else ""
        if core_desc:
            role_header = f"{role_header}\n{core_desc}" if role_header else core_desc
        if role_header:
            sections.append(f"【角色核心】\n{role_header.strip()}")

        # 2. 重要记忆与习惯（若角色具备）
        personal_memories = getattr(persona, "personal_memories", None)
        if personal_memories:
            mems = "；".join(
                [f"{getattr(m, 'title', '')}（{getattr(m, 'content', '')}）" for m in personal_memories if getattr(m, 'title', '') or getattr(m, 'content', '')]
            )
            if mems:
                sections.append(f"【心中重要记忆】\n{mems}")

        habits = getattr(persona, "habits", None)
        if habits and isinstance(habits, (list, tuple)):
            habits_str = "；".join(str(h) for h in habits if h)
            if habits_str:
                sections.append(f"【生活习惯】\n{habits_str}")

        # 3. 世界书（Lorebook）与世界观
        lorebook = getattr(persona, "lorebook", None)
        always_lore = getattr(lorebook, "always", "") if lorebook else ""
        if isinstance(always_lore, str) and always_lore.strip():
            sections.append(f"【她的世界】\n{always_lore.strip()}")

        if lorebook:
            scan_turns = getattr(lorebook, "scan_turns", 4)
            try:
                scan_limit = max(0, int(scan_turns))
            except (TypeError, ValueError):
                scan_limit = 4
            texts = self._collect_lore_texts(turn_input, history, scan_limit)
            lore_block = self._build_dynamic_lore_block(lorebook, texts)
            if lore_block and lore_block.strip():
                sections.append(lore_block.strip())

        # 4. 聊天规范与纯文字交互准则（禁止动作描写与系统协议标记）
        guidelines = list(FIXED_GUIDELINES)
        chat_style = getattr(persona, "chat_style", None)
        raw_rules = getattr(chat_style, "rules", []) if chat_style else []
        filtered_rules = []
        for rule in raw_rules:
            if not isinstance(rule, str):
                continue
            cleaned_rule = filter_style_rule(rule)
            if cleaned_rule:
                filtered_rules.append(cleaned_rule)

        rules_text = "\n".join(f"- {g}" for g in guidelines)
        if filtered_rules:
            rules_text += "\n" + "\n".join(f"- {r}" for r in filtered_rules)
        sections.append(f"【聊天准则】\n{rules_text}")

        # 5. 角色卡的完整风格资产，分块保留且只读过滤动作协议。
        def cleaned_examples(name: str) -> List[str]:
            values = getattr(chat_style, name, []) if chat_style else []
            output: List[str] = []
            for value in values or []:
                if not isinstance(value, str):
                    continue
                cleaned = filter_action_tags(value)
                if cleaned and cleaned not in output:
                    output.append(cleaned)
            return output if self.max_examples is None else output[:self.max_examples]

        stage_idx = getattr(snapshot, "stage_index", 0)
        good_max_stage = getattr(chat_style, "good_examples_max_stage", None) if chat_style else None
        good_examples = cleaned_examples("good_examples")
        if good_examples:
            if good_max_stage is not None and stage_idx > good_max_stage:
                examples_block = "见下方【当前关系阶段】里这个阶段的语气示范"
            else:
                examples_block = "\n".join(f"  - {example}" for example in good_examples)
            sections.append(f"【正确示范】\n{examples_block}")

        plain_examples = cleaned_examples("plain_examples")
        if plain_examples:
            examples_block = "\n".join(f"  - {example}" for example in plain_examples)
            sections.append(
                "【日常废话流基线】\n她大部分消息就是这样平淡的一句，"
                f"不必每条都接话、提问或表达点什么：\n{examples_block}"
            )

        bad_examples = cleaned_examples("bad_examples")
        if bad_examples:
            examples_block = "\n".join(f"  - {example}" for example in bad_examples)
            sections.append(f"【错误示范（禁止）】\n{examples_block}")

        # 6. 当前关系阶段（当前态度与相处指引）
        stages = getattr(persona, "stages", None)
        if stages and isinstance(stages, (list, tuple)) and 0 <= stage_idx < len(stages):
            stage = stages[stage_idx]
            stage_name = getattr(stage, "name", f"阶段{stage_idx}")
            stage_tone = getattr(stage, "tone", "")
            stage_insts = getattr(stage, "instructions", []) or []
            safe_insts = [cleaned for i in stage_insts if (cleaned := filter_style_rule(str(i)))]
            insts_str = "；".join(str(i) for i in safe_insts if i)
            stage_info = f"当前阶段：{stage_name}。态度倾向：{stage_tone}。相处指引：{insts_str}"

            stage_examples = getattr(stage, "examples", []) or []
            filtered_stage_ex = []
            for se in stage_examples:
                if isinstance(se, str):
                    clean_se = filter_action_tags(se)
                    if clean_se:
                        filtered_stage_ex.append(clean_se)
            if filtered_stage_ex:
                stage_info += "\n阶段语气示范：\n" + "\n".join(f"  - {e}" for e in filtered_stage_ex[:3])
            sections.append(f"【当前关系阶段】\n{stage_info}")

        # 7. 心理状态提示（PsychologicalSnapshot 的精简 prompt_hint）
        prompt_hint = getattr(snapshot, "prompt_hint", "")
        if prompt_hint and isinstance(prompt_hint, str) and prompt_hint.strip():
            sections.append(f"【当前内心状态】\n{prompt_hint.strip()}")

        # 8. 本轮时间事实块（客观事实，放在心理提示附近；绝不写进用户正文）
        sections.append(f"【本轮时间】\n{self._build_time_block(persona, snapshot, now_dt)}")

        # 9. 本轮只读采集的记忆与安全块；缺省契约全部为空时不改变原结构。
        if memory_blocks is not None:
            for block in (
                memory_blocks.facts,
                memory_blocks.impressions,
                memory_blocks.self_facts,
                memory_blocks.diaries,
                memory_blocks.followups,
                memory_blocks.meetups,
                memory_blocks.desires,
                memory_blocks.arcs,
            ):
                if block and block.strip():
                    sections.append(block.strip())

        # 10. 动态示例已在公共准备阶段选定；这里只渲染冻结块，不检索或抽样。
        if style_examples is not None and style_examples.block.strip():
            sections.append(style_examples.block.strip())

        # 安全边界始终最后，不能被真人参考或其他可变块稀释。
        if memory_blocks is not None and memory_blocks.safety.strip():
            sections.append(memory_blocks.safety.strip())

        system_prompt = "\n\n".join(sections)

        # 游戏世界不说"10 月 22 号"：给她看的日期最后统一换成相对说法（与旧链路同一参照）
        world = getattr(persona, "world", None)
        if getattr(world, "relative_dates", False):
            system_prompt = relativize_dates(system_prompt, now_dt)
        return system_prompt

    def build_messages(
        self,
        persona: Any,
        snapshot: PsychologicalSnapshot,
        history: Sequence[Union[Dict[str, Any], Mapping[str, Any]]],
        turn_input: StandardizedTurnInput,
        memory_blocks: Optional[MemoryPromptBlocks] = None,
        style_examples: Optional[StyleExampleSelection] = None,
    ) -> List[Dict[str, Any]]:
        """组装适合 gateway.stream_chat 的标准 messages 列表。

        [
            {"role": "system", "content": ...},
            {"role": "user"|"assistant", "content": ...},
            ...
            {"role": "user", "content": ...}
        ]
        """
        system_content = self.build_system_prompt(
            persona,
            snapshot,
            turn_input=turn_input,
            history=history,
            memory_blocks=memory_blocks,
            style_examples=style_examples,
        )
        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_content}]

        # 处理近期历史（至多 max_history_turns 条）
        if history:
            sliced_history = history[-self.max_history_turns :]
            for item in sliced_history:
                role = item.get("role", "user")
                # 兼容不同历史数据结构：semantic_text > readable_text > text > content
                content = (
                    item.get("semantic_text")
                    or item.get("readable_text")
                    or item.get("content")
                    or item.get("text")
                    or ""
                )
                if isinstance(content, str):
                    content = semantic_history_view(content, role)
                if content:
                    messages.append({"role": role, "content": str(content)})

        # 当前轮统一使用输入层已经生成的自然语义，不再另走一条过滤路径。
        current_user_content = turn_input.text
        if turn_input.quoted_text:
            current_user_content = f"他引用之前的“{turn_input.quoted_text}”时说：\n{current_user_content}"

        messages.append({"role": "user", "content": current_user_content})

        return messages
