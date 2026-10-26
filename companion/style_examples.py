"""动态示例 (style_examples.py)
按他这一句话，从真人聊天库里挑几条"类似情境下别人怎么接"的例子塞进提示词。

只学结构，不学口吻：库里是另一个人的回复，提示词里明说不能照抄她的句子、
用词和口头禅。库在整理时已去掉对方的表情和招牌口头禅，这里不再处理。

块放在【事实】之后：那里的时间每轮都变，前面的长前缀照样能命中缓存。

检索是汉字二元组 TF-IDF 余弦，不依赖任何外部服务；先取最像的 TOP_K 条，
再随机抽 PICK 条，避免同一句话每次都看到一模一样的示例。
"""

from __future__ import annotations

import json
import logging
import math
import random
import re
from collections import Counter
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

TOP_K = 5
PICK = 3
MIN_SCORE = 0.15
TAG_RE = re.compile(r"\[[^\]]*\]")
HANZI_RE = re.compile(r"[一-鿿]")

BLOCK_HEADER = (
    "\n【别人的聊天参考】不是你说过的话，也不是你的口吻：下面是另一个女生在类似情境下怎么回的。"
    "只看她怎么接——回多长、先接情绪还是先接事、会不会顺手说一句自己相关的小事；"
    "不要照抄她的句子、用词和口头禅，用你自己的说法："
)


def _bigrams(text: str) -> Counter:
    hz = "".join(HANZI_RE.findall(TAG_RE.sub("", text or "")))
    if len(hz) < 2:
        return Counter(hz)
    return Counter(hz[i:i + 2] for i in range(len(hz) - 1))


def _one_line(text: str) -> str:
    return " / ".join(p.strip() for p in (text or "").split("\n") if p.strip())


class StyleExampleBank:
    def __init__(
        self,
        items: List[Dict[str, str]],
        rng: Optional[random.Random] = None,
        header: Optional[str] = None,
    ):
        self.items = [x for x in items if x.get("his") and x.get("her")]
        self.rng = rng or random.Random()
        # 库文件可自带表头（字典格式的 "header" 键）：官方语料库是"她自己说过的话"，
        # 与真人库的"另一个女生"口径不同；没带就用默认表头，旧库行为不变。
        self.header = header or BLOCK_HEADER
        self._vecs = [_bigrams(x["his"]) for x in self.items]
        df: Counter = Counter()
        for v in self._vecs:
            df.update(v.keys())
        n = max(len(self._vecs), 1)
        self._idf = {k: math.log((n + 1) / (c + 1)) + 1.0 for k, c in df.items()}
        self._norms = [self._norm(v) for v in self._vecs]

    @classmethod
    def load(cls, path: str) -> Optional["StyleExampleBank"]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            # 两种库格式：纯列表（旧），或 {"header": 表头, "items": [...]}（新，可选表头）
            if isinstance(raw, dict):
                items = raw.get("items", [])
                header = raw.get("header") if isinstance(raw.get("header"), str) else None
            else:
                items, header = raw, None
            bank = cls(items, header=header)
            logger.info(f"[StyleExamples] 载入 {len(bank.items)} 条示例: {path}")
            return bank if bank.items else None
        except Exception as e:
            logger.error(f"[StyleExamples] 载入失败，动态示例不启用: {e}")
            return None

    def _norm(self, v: Counter) -> float:
        return math.sqrt(sum((c * self._idf.get(k, 1.0)) ** 2 for k, c in v.items())) or 1.0

    def search_entries(self, text: str, top_k: int = TOP_K) -> List[Tuple[int, Dict[str, str]]]:
        """返回候选的原始序号与内容，供可回放抽样和 trace 使用。"""
        q = _bigrams(text)
        if not q:
            return []
        qn = self._norm(q)
        scored = []
        for i, v in enumerate(self._vecs):
            dot = sum(c * v[k] * self._idf.get(k, 1.0) ** 2 for k, c in q.items() if k in v)
            if dot:
                s = dot / (qn * self._norms[i])
                if s >= MIN_SCORE:
                    scored.append((s, i))
        scored.sort(reverse=True)
        return [(i, self.items[i]) for _, i in scored[:top_k]]

    def search(self, text: str, top_k: int = TOP_K) -> List[Dict[str, str]]:
        return [item for _index, item in self.search_entries(text, top_k=top_k)]

    def pick(self, text: str, n: int = PICK) -> List[Dict[str, str]]:
        cands = self.search(text)
        if len(cands) > n:
            cands = self.rng.sample(cands, n)
        return cands

    def render(self, picked: Sequence[Dict[str, str]]) -> str:
        """只渲染已经选定的条目；空序列不留下标题或空白块。"""
        if not picked:
            return ""
        lines = [
            f"\n  - 他：{_one_line(x['his'])} → 她：{_one_line(x['her'])}" for x in picked
        ]
        return self.header + "".join(lines)

    def build_block(self, text: str) -> str:
        """旧链路入口：保留共享 RNG 抽样行为，不在本阶段改生产基线。"""
        try:
            return self.render(self.pick(text))
        except Exception as e:
            logger.error(f"[StyleExamples] 检索异常: {e}")
            return ""
