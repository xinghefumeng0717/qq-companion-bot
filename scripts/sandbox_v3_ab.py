"""青梓角色卡 V2/V3 多轮脚本化对照仿真 (scripts/sandbox_v3_ab.py)

用途：对生产暴露的文风场景，在「原卡 characters/qingzi（下称 V2）」与
「候选卡 characters/qingzi-v3（下称 V3）」上各跑 --runs 次（默认 3），
用户消息按脚本逐条喂入，她的回复经 ChatSession 落入沙箱库后作为下一轮历史，
完整复刻生产多轮链路（含 observer 结算）。

关键实现：
  - 继承 companion.chat.ChatSession，只覆写 initialize()：用全新空库建表，
    不再执行 ChatSession 默认的「复制生产库到沙箱」那一步。
  - 组装期把 companion 各模块的 datetime 属性 patch 成固定时刻（时间伪装）。
  - 跑完即删临时库（ChatSession.close 会删，脚本再兜底删 -wal/-shm）。
  - 轮次留档：--round 1 写 data/v3_ab_raw.json，--round 2 写 data/v3_ab_raw_round2.json；
    报告 data/v3_ab_transcripts.md 由磁盘上已存在的各轮留档**合成**（第一轮 + 第二轮）。
  - --from-raw：不调 API，仅用留档重新合成报告（便于改统计口径）。

用法：
  ./venv/Scripts/python.exe scripts/sandbox_v3_ab.py --round 2 --scenarios A,B,D,F --runs 3
  ./venv/Scripts/python.exe scripts/sandbox_v3_ab.py --from-raw

产物：
  data/v3_ab_transcripts.md      完整 transcript + 客观统计（合成各轮）
  data/v3_ab_raw.json            第一轮留档
  data/v3_ab_raw_round2.json     第二轮留档

纪律：不改 characters/qingzi（原卡）、不改 config.toml；API key 只从 config.toml 读取，
任何产物里都不得出现 key。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import sys
from datetime import datetime
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from companion.affection import AffectionEngine
from companion.assembler import PromptAssembler
from companion.chat import ChatSession
from companion.config import Config
from companion.db import Database
from companion.gateway import LLMGateway
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.observer import Observer
from companion.persona import Persona
from companion.replier import SILENCE_TOKEN, Replier
from companion.stickers import StickerManager

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT_MD = "data/v3_ab_transcripts.md"
ROUND_RAW = {1: "data/v3_ab_raw.json", 2: "data/v3_ab_raw_round2.json"}
ROUND_LABEL = {1: "第一轮", 2: "第二轮"}

CARDS: List[Tuple[str, str]] = [
    ("V2", "characters/qingzi"),
    ("V3", "characters/qingzi-v3"),
]

SCENARIOS: Dict[str, Dict[str, Any]] = {
    "A": {
        "title": "告别拖尾",
        "turns": ["今天聊得开心，我先去洗澡啦", "嗯嗯"],
    },
    "B": {
        "title": "连环叮嘱",
        "turns": ["我待会要坐动车回家了，先收拾去了"],
    },
    "C": {
        "title": "话图一体",
        "turns": [
            "今天红烧肉真好吃[发来一张照片：一盘色泽红亮的红烧肉，撒着葱花，摆在白色瓷盘里。]"
        ],
    },
    "D": {
        "title": "表情包收尾",
        "turns": [
            "那我先去自习了，你忙你的",
            "[发来一张照片：白色卡通小动物，紫底，头顶有'嗯嗯'抖动符号，带腮红，表情乖巧。]",
        ],
    },
    "E": {
        "title": "梗寿命",
        "turns": [
            "哈哈哈你还记得上次那个麻辣烫阿姨的梗吗",
            "对对，红油那位",
            "所以阿姨到底记住我没",
        ],
    },
    "F": {
        "title": "防滥用探针（非告别语境的「嗯嗯」）",
        "turns": ["你们乐团最近排练紧吗", "嗯嗯"],
    },
}

# ── 时间伪装（与 card_ab_test2/3 同一手法：只在本进程内替换模块的 datetime 属性）──
FAKE_NOW = datetime(2026, 10, 1, 20, 30)  # 2026-10-01 是星期四，20:30 命中周四作息正常清醒时段
FAKE_NOW_STR = FAKE_NOW.strftime("%Y-%m-%d %H:%M")
TIME_PATCH_MODULES = (
    "companion.db",
    "companion.assembler",
    "companion.memory",
    "companion.mood",
    "companion.affection",
)
_ORIG_DATETIME: Dict[str, Any] = {}


class FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FAKE_NOW if tz is None else FAKE_NOW.replace(tzinfo=tz)

    @classmethod
    def utcnow(cls):
        return FAKE_NOW

    @classmethod
    def today(cls):
        return FAKE_NOW


def patch_time() -> None:
    for name in TIME_PATCH_MODULES:
        mod = importlib.import_module(name)
        if name not in _ORIG_DATETIME:
            _ORIG_DATETIME[name] = getattr(mod, "datetime")
        setattr(mod, "datetime", FakeDatetime)


def restore_time() -> None:
    for name, orig in _ORIG_DATETIME.items():
        setattr(importlib.import_module(name), "datetime", orig)


class FreshChatSession(ChatSession):
    """与生产 ChatSession 完全同构，只把「复制生产库」换成「全新空库建表」。"""

    async def initialize(self) -> None:
        db_dir = os.path.dirname(self.sandbox_db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            p = self.sandbox_db_path + suffix
            if os.path.exists(p):
                os.remove(p)

        self.db = Database(self.sandbox_db_path)
        await self.db.init_tables()

        self.persona = Persona.load(self.config.character.path)
        stickers_dir = os.path.join(self.persona.base_dir, self.persona.stickers_dir)
        self.stickers = StickerManager(stickers_dir, self.db)
        await self.stickers.sync_initial_stickers()

        self.affection = AffectionEngine(self.db, self.persona.initial_dims)
        self.mood = MoodEngine(self.db)
        self.gateway = LLMGateway(self.config.llm, self.db)
        self.memory = MemoryManager(self.db, self.gateway, self.affection, self.persona)
        self.assembler = PromptAssembler(
            self.persona,
            self.affection,
            self.mood,
            self.memory,
            self.stickers,
            self.db,
            holidays_provider=self.config.get_holidays,
        )
        self.replier = Replier(self.config.reply, self.stickers)
        self.observer = Observer(
            self.gateway, self.affection, self.mood, self.memory, self.stickers, self.db
        )
        self._initialized = True


async def run_one(
    card_name: str,
    card_dir: str,
    scenario_key: str,
    run_idx: int,
    sem: asyncio.Semaphore,
    results: Dict[Tuple[str, str, int], List[Dict[str, str]]],
    log: List[str],
) -> None:
    async with sem:
        db_path = f"data/sandbox_v3test_{scenario_key}_{card_name}_{run_idx}.db"
        cfg = Config.load()
        cfg.character.path = card_dir
        session = FreshChatSession(config=cfg, sandbox_db_path=db_path)
        turns: List[Dict[str, str]] = []
        try:
            await session.initialize()
            for msg in SCENARIOS[scenario_key]["turns"]:
                raw_parts: List[str] = []
                clean = await session.handle_input(msg, on_piece=raw_parts.append)
                turns.append(
                    {"user": msg, "raw": "".join(raw_parts).strip(), "reply": clean}
                )
            # save_turn_pair 会派生一个后台日记归档任务（<8 轮时只读库后早退）；
            # 给它一点时间跑完，否则它会撞上紧接着关闭的沙箱库并抛 ProgrammingError
            await asyncio.sleep(0.2)
        except Exception as e:
            turns.append(
                {
                    "user": "[ERROR]",
                    "raw": f"{type(e).__name__}: {e}",
                    "reply": f"[失败] {type(e).__name__}: {e}",
                }
            )
        finally:
            try:
                await session.close()
            except Exception:
                pass
            for suffix in ("", "-wal", "-shm"):
                p = db_path + suffix
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
        results[(scenario_key, card_name, run_idx)] = turns
        n_ok = sum(1 for t in turns if not t["reply"].startswith("[失败]"))
        log.append(f"  完成 场景{scenario_key}/{card_name}/第{run_idx}次：{n_ok}/{len(turns)} 轮成功")


# ==========================================
# 客观统计口径（统一规则，两卡同规则套用）
# ==========================================

KEY_TURN = {"A": 1, "B": 0, "C": 0, "D": 1, "E": 2, "F": 1}

CLAUSE_SPLIT_RE = re.compile(r"[。！？!?~～，、；;\n]+")
Q_RE = re.compile(r"[？?]")
QUESTION_RE = re.compile(r"[？?]|哪|什么|怎么|为啥|为什么|吗$|呢$|么$")
# 语气词（短气泡温度）：带语气词 = 不是干巴巴的短收
TONE_RE = re.compile(r"呀|啦|吧|呢|嘛|哈|哦|噢|嗯|诶|咯|哟|呗|～|~")

NEW_SCENE_RE = re.compile(
    r"回来了|刚回|刚到|洗完|收拾完|散完步|散步回来|湖面|晚风|风特别|灯光|宿舍|琴房|食堂|练完|楼下"
)
CAUTION_RE = re.compile(
    r"记得|别忘|别睡|别熬|别光顾|路上|慢点|注意|小心|到家|到了|说一声|发消息|报平安|"
    r"早点|提前|多穿|多喝|带好|收好|眯会儿|眯一会|照顾好|安全"
)
C_VISUAL_RE = re.compile(
    r"看着|看起来|颜色|油亮|亮|葱花|瓷盘|白瓷|摆盘|这盘|盘里|盘子|照片|图里|这图|这张图"
)
C_TASTE_RE = re.compile(r"好吃|香|馋|味道|辣|下饭|想尝|看饿|会吃|爱吃|喜欢|想吃|来一口")
D_IMG_RE = re.compile(
    r"卡通|小动物|紫色|紫底|腮红|抖动|乖|软乎乎|这只|画风|表情包|图里|这图|这张|"
    r"可爱|小家伙|毛茸茸|白团|软软"
)
E_MEME_RE = re.compile(r"麻辣烫|红油|阿姨|记住|记得|认脸|脸熟")


def clauses(text: str) -> List[str]:
    return [s.strip() for s in CLAUSE_SPLIT_RE.split(text) if s.strip()]


def bubbles(text: str) -> List[str]:
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def chars(text: str) -> int:
    return len(re.sub(r"\s", "", text))


def fmt_num_list(vals: List[Any]) -> str:
    return "/".join(str(v) for v in vals)


def is_silent(text: str) -> bool:
    return text.strip() == SILENCE_TOKEN


def _cautions(text: str) -> List[str]:
    """叮嘱小句 = 命中叮嘱词、且不是疑问句的小句（问句属正常关切，不计入连环祈使）"""
    return [c for c in clauses(text) if CAUTION_RE.search(c) and not QUESTION_RE.search(c)]


def _pure_visual_and_taste(text: str) -> Tuple[List[str], List[str], List[str]]:
    pure_vis, taste, vis_all = [], [], []
    for b in bubbles(text):
        has_vis, has_taste = bool(C_VISUAL_RE.search(b)), bool(C_TASTE_RE.search(b))
        if has_vis:
            vis_all.append(b)
            if not has_taste:
                pure_vis.append(b)
        if has_taste:
            taste.append(b)
    return pure_vis, taste, vis_all


def key_reply(turns: List[Dict[str, str]], key: str) -> str:
    idx = KEY_TURN.get(key, 0)
    return turns[idx]["reply"] if len(turns) > idx else ""


def silence_positions(turns: List[Dict[str, str]]) -> List[int]:
    return [i + 1 for i, t in enumerate(turns) if is_silent(t.get("reply", ""))]


# ==========================================
# 报告
# ==========================================

LEGEND = """## 统计口径（两卡同规则套用，只数事实）

- 字数：去掉所有空白字符后的字符数；段数/气泡数：换行分隔的非空段数
- 小句：按 `。！？!?~～，、；;` 与换行切分
- 沉默：某轮回复 strip 后恰为 `[沉默]`（与 `companion/replier.py` 的判定一致）
- 语气词命中：`呀|啦|吧|呢|嘛|哈|哦|噢|嗯|诶|咯|哟|呗|～|~` 的去重命中
- 关键轮 = 每场景的收尾承接轮：A/D/F 为第 2 条（他「嗯嗯」/表情包）之后，B 为第 1 条，C 为第 1 条，E 为第 3 条
- 编造新场景词（A/F）：`回来了|刚回|刚到|洗完|收拾完|散完步|散步回来|湖面|晚风|风特别|灯光|宿舍|琴房|食堂|练完|楼下`
- 叮嘱小句（B）：小句命中 `记得|别忘|别睡|别熬|别光顾|路上|慢点|注意|小心|到家|到了|说一声|发消息|报平安|早点|提前|多穿|多喝|带好|收好|眯会儿|眯一会|照顾好|安全` 且不含疑问标记（`？?哪什么怎么为啥为什么吗$呢$么$`）
- 两段式（C，机械判定）：存在「只点评图片、不接话」的气泡（命中视觉词且不含味觉词）且同时存在味觉/接话气泡
- 视觉词（C）：`看着|看起来|颜色|油亮|亮|葱花|瓷盘|白瓷|摆盘|这盘|盘里|盘子|照片|图里|这图|这张图`；味觉/接话词（C）：`好吃|香|馋|味道|辣|下饭|想尝|看饿|会吃|爱吃|喜欢|想吃|来一口`
- 图内元素词（D）：`卡通|小动物|紫色|紫底|腮红|抖动|乖|软乎乎|这只|画风|表情包|图里|这图|这张|可爱|小家伙|毛茸茸|白团|软软`
- 梗词（E）：`麻辣烫|红油|阿姨|记住|记得|认脸|脸熟`
"""


def _transcript_section(
    label: str,
    results: Dict[Tuple[str, str, int], List[Dict[str, str]]],
    key: str,
    runs: int,
    render_raw: bool,
) -> List[str]:
    sc = SCENARIOS[key]
    L: List[str] = []
    L.append(f"### 场景{key} · {sc['title']}")
    L.append("")
    L.append("机主脚本：")
    for i, msg in enumerate(sc["turns"], start=1):
        L.append(f"{i}. {msg}")
    L.append("")
    for card_name, _ in CARDS:
        for run in range(1, runs + 1):
            turns = results.get((key, card_name, run), [])
            L.append(f"#### {label} / 场景{key} / {card_name} / 第{run}次")
            L.append("")
            for i, t in enumerate(turns, start=1):
                L.append(f"**机主 第{i}条：** {t['user']}")
                L.append("")
                if render_raw:
                    L.append("**青梓（模型裸输出）：**")
                    L.append("")
                    L.append("> " + t["raw"].replace("\n", "\n> "))
                    if re.sub(r"\s", "", t["raw"]) != re.sub(r"\s", "", t["reply"]):
                        L.append("")
                        L.append("**生产实发记录：**")
                        L.append("")
                        L.append("> " + t["reply"].replace("\n", "\n> "))
                else:
                    L.append("**青梓：**")
                    L.append("")
                    L.append("> " + t["reply"].replace("\n", "\n> "))
                L.append("")
            L.append("")
    return L


def _stats_section(
    results: Dict[Tuple[str, str, int], List[Dict[str, str]]],
    keys: List[str],
    runs: int,
) -> List[str]:
    L: List[str] = []
    L.append("### 客观统计（只数事实）")
    L.append("")
    L.append("**沉默总览（整场任意轮的 [沉默]）**")
    L.append("")
    for key in keys:
        for card_name, _ in CARDS:
            pos = [
                silence_positions(results.get((key, card_name, r), []))
                for r in range(1, runs + 1)
            ]
            hit = sum(1 for p in pos if p)
            L.append(
                f"- 场景{key} {card_name}：三轮沉默轮次位置 {pos}；出现沉默的采样数 {hit}/{runs}"
            )
    L.append("")
    L.append("**关键轮逐项**")
    L.append("")
    for key in keys:
        for card_name, _ in CARDS:
            turns_list = [results.get((key, card_name, r), []) for r in range(1, runs + 1)]
            replies = [key_reply(t, key) for t in turns_list]
            sil = ["沉默" if is_silent(r) else "回复" for r in replies]
            lens = [chars(r) for r in replies]
            segs = [len(bubbles(r)) for r in replies]
            qs = [len(Q_RE.findall(r)) for r in replies]
            tones = [sorted(set(TONE_RE.findall(r))) if not is_silent(r) else [] for r in replies]
            L.append(
                f"- 场景{key} {card_name}：节奏 {sil}；字数 {fmt_num_list(lens)}；"
                f"段数 {fmt_num_list(segs)}；问号 {fmt_num_list(qs)}；语气词命中 {tones}"
            )
            if key in ("A", "F"):
                scene = [sorted(set(NEW_SCENE_RE.findall(r))) for r in replies]
                L.append(f"  - 新场景词命中 {scene}")
            elif key == "B":
                for i, r in enumerate(replies, start=1):
                    L.append(f"  - 第{i}次叮嘱小句：{_cautions(r)}")
            elif key == "C":
                flag, vn, tn = [], [], []
                for r in replies:
                    pure_vis, taste, vis_all = _pure_visual_and_taste(r)
                    flag.append("是" if (pure_vis and taste) else "否")
                    vn.append(len(vis_all))
                    tn.append(len(taste))
                L.append(
                    f"  - 两段式(机械判定) {fmt_num_list(flag)}；视觉词气泡数 {fmt_num_list(vn)}；"
                    f"味觉/接话气泡数 {fmt_num_list(tn)}"
                )
            elif key == "D":
                img = [sorted(set(D_IMG_RE.findall(r))) for r in replies]
                L.append(f"  - 图内元素词命中 {img}")
            elif key == "E":
                meme = [sorted(set(E_MEME_RE.findall(r))) for r in replies]
                L.append(f"  - 梗词命中 {meme}")
    L.append("")
    return L


def build_round_doc(
    label: str,
    results: Dict[Tuple[str, str, int], List[Dict[str, str]]],
    meta: Dict[str, Any],
    render_raw: bool,
) -> List[str]:
    runs = meta["runs_per_cell"]
    keys = list(meta["scenarios"].keys())
    L: List[str] = []
    L.append("---")
    L.append("")
    L.append(f"## {label}")
    L.append("")
    L.append(f"- 生成时间（真实系统时间）：{meta['generated_at']}")
    L.append(f"- 时间伪装：组装期注入的「现在」固定为 **{meta.get('fake_now', FAKE_NOW_STR)} 星期四**；observer 结算逐轮开启（与生产一致）")
    L.append(f"- 模型：`deepseek-v4-pro`（config.toml 默认主聊），thinking enabled / reasoning_effort low（与生产一致）")
    L.append(
        "- 数据库策略：每个 (场景×卡×采样) 使用**全新空库** `data/sandbox_v3test_*.db`，跑完即删；"
        "不复用 `data/companion.db` 或任何含历史的库"
    )
    L.append(f"- 采样：每场景每卡 {runs} 次；失败调用 {meta.get('failures', 0)} 条（标 `[失败]`）")
    L.append("- 卡片：V2 = `characters/qingzi/character.json`（原卡）；V3 = `characters/qingzi-v3/character.json`（候选卡）")
    L.append(f"- 调用耗时：{meta.get('elapsed_sec', 0)} 秒")
    L.append("")
    for key in keys:
        L.extend(_transcript_section(label, results, key, runs, render_raw))
    L.extend(_stats_section(results, keys, runs))
    return L


def compose_md() -> str:
    L: List[str] = []
    L.append("# 青梓角色卡 V2/V3 多轮脚本化对照仿真 · 原始 transcript")
    L.append("")
    L.append(LEGEND)
    for rnd in sorted(ROUND_RAW):
        path = ROUND_RAW[rnd]
        if not os.path.exists(path):
            continue
        results, meta = load_raw(path)
        L.extend(build_round_doc(ROUND_LABEL[rnd], results, meta, render_raw=(rnd == 2)))
    return "\n".join(L)


def arg_value(name: str, default: str) -> str:
    for i, a in enumerate(sys.argv):
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
        if a == name and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def load_raw(path: str) -> Tuple[Dict[Tuple[str, str, int], List[Dict[str, str]]], Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    results: Dict[Tuple[str, str, int], List[Dict[str, str]]] = {}
    for k, v in data["results"].items():
        sk, card, run = k.split("|")
        results[(sk, card, int(run))] = v
    return results, data["meta"]


async def main() -> None:
    os.chdir(ROOT)
    round_no = int(arg_value("--round", "1"))
    runs = int(arg_value("--runs", "3"))
    raw_keys = arg_value("--scenarios", ",".join(SCENARIOS.keys()))
    keys = [k.strip() for k in raw_keys.split(",") if k.strip() in SCENARIOS]
    concurrency = int(arg_value("--concurrency", "4"))
    out_raw = ROUND_RAW.get(round_no, ROUND_RAW[1])

    if "--from-raw" in sys.argv:
        with open(OUT_MD, "w", encoding="utf-8") as f:
            f.write(compose_md())
        print(f"已用磁盘留档重新合成 {OUT_MD}")
        return

    print(f"[仿真] 第{round_no}轮 场景 {keys} × 卡 {[c for c, _ in CARDS]} × 每格 {runs} 次；并发 {concurrency}", flush=True)
    print(f"[时间伪装] 组装期固定为 {FAKE_NOW_STR}（星期四）", flush=True)

    patch_time()
    started = datetime.now()
    results: Dict[Tuple[str, str, int], List[Dict[str, str]]] = {}
    log: List[str] = []
    sem = asyncio.Semaphore(concurrency)
    try:
        jobs = []
        for key in keys:
            for card_name, card_dir in CARDS:
                for run in range(1, runs + 1):
                    jobs.append(run_one(card_name, card_dir, key, run, sem, results, log))
        await asyncio.gather(*jobs)
    finally:
        restore_time()

    failures = sum(
        1 for turns in results.values() for t in turns if t["reply"].startswith("[失败]")
    )
    elapsed = int((datetime.now() - started).total_seconds())
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(out_raw, "w", encoding="utf-8") as f:
        json.dump(
            {
                "meta": {
                    "round": round_no,
                    "generated_at": generated_at,
                    "fake_now": FAKE_NOW_STR,
                    "runs_per_cell": runs,
                    "cards": CARDS,
                    "scenarios": {k: SCENARIOS[k] for k in keys},
                    "elapsed_sec": elapsed,
                    "failures": failures,
                },
                "results": {f"{k}|{c}|{r}": v for (k, c, r), v in results.items()},
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write(compose_md())

    print("\n".join(log))
    print(f"\n完成：{len(results)} 格，失败 {failures} 条，耗时 {elapsed} 秒")
    print(f"留档: {out_raw}")
    print(f"报告: {OUT_MD}")


if __name__ == "__main__":
    asyncio.run(main())
