"""青梓综合评测体系 V4 (scripts/benchmark_v4.py)

对**当前生产卡**（characters/qingzi/，即已替换的 V3 卡）做一次性定版大考：
12 个场景 × 2 个时间伪装时钟 × N 次采样，全部走本地 config.toml 的真实 key
（主聊 deepseek-v4-pro / thinking low，与生产一致），严禁 mock 主聊。

方法论铁律（逐条对应 docs/BENCHMARK_V4.md）：
  1. 每个 (场景×时钟×采样) 一个**全新空库**，初始好感度必须回初始值，跑完即删；
  2. 采样数 N=5，同时报逐次结果与汇总，不许只报均值掩盖波动；
  3. 客观指标（正则/计数）为主，风格类只采样收集 + 原文呈现，**不用 LLM 裁判**；
  4. 两个时钟：工作日钟 2026-10-09 周五 15:00 / 长假钟 2026-10-05 周一 15:00；
  5. --from-raw 零成本复算（只读磁盘留档重算指标与判定，不调 API）。

场景 J 的重要教训（FIXES12 A 段踩过）：
  主动消息有"决策层 choice=A/B/C"这道 LLM 闸门，闸门选不到 A 时"产出不含
  [图片] 占位符"会**自动成立**，等于什么都没验。所以 J 除了跑真实 trigger_cycle
  （记录 choice_a 事实），还额外做一次**绕过决策闸门的强制最坏情况直调**
  （topic_material 直接填"拍张临湖餐厅的菜发给他"），实发层判定以两次合并为准。

用法：
  ./venv/Scripts/python.exe scripts/benchmark_v4.py --scenarios A,B --runs 5
  ./venv/Scripts/python.exe scripts/benchmark_v4.py --smoke
  ./venv/Scripts/python.exe scripts/benchmark_v4.py --from-raw

产物（不入 git）：
  data/benchmark_v4/report.json      全部原始输出 + 逐指标数值 + 逐场景判定
  data/benchmark_v4/report.md        一页汇总表 + 每个 FAIL 场景的全部原文
  data/benchmark_v4/transcripts.md   全部 transcript 留档

纪律：不改 companion/（生产代码）、不改 characters/、不改 config.toml。
发现生产 bug 只记录，不顺手修。config.toml 里的 [llm.pricing].holidays 本地为空，
长假钟所需的 2026 国庆假期日期**只在进程内注入内存对象**（见 HOLIDAYS_INJECTED），
绝不落盘改配置。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import math
import os
import re
import statistics
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from companion.affection import AffectionEngine
from companion.assembler import PromptAssembler
from companion.chat import ChatSession
from companion.config import Config, ProactiveConfig, ReplyConfig
from companion.db import TIME_FORMAT, Database
from companion.gateway import LLMGateway
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.observer import Observer
from companion.persona import Persona, is_holiday_date
from companion.proactive import ProactiveScheduler, format_recent_chat
from companion.prompts import PROACTIVE_GENERATE_PROMPT
from companion.replier import SILENCE_TOKEN, Replier, is_silence_output
from companion.stickers import StickerManager

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT_DIR = "data/benchmark_v4"
RAW_JSON = f"{OUT_DIR}/report.json"
REPORT_MD = f"{OUT_DIR}/report.md"
TRANSCRIPTS_MD = f"{OUT_DIR}/transcripts.md"

CARD_DIR = "characters/qingzi"          # 当前生产卡（V3），单卡评测不做 A/B
CARD_LABEL = "生产卡 V3（characters/qingzi/character.json）"

# ── 时钟：两个时间伪装时刻 ──────────────────────────────────────────────
# 2026-10-09 是周五（国庆假期已结束的普通工作日）；2026-10-05 是周一（国庆长假中）。
CLOCKS: Dict[str, Dict[str, Any]] = {
    "work": {
        "now": datetime(2026, 10, 9, 15, 0),
        "label": "工作日钟",
        "desc": "2026-10-09 周五 15:00，国庆假期已结束，常规上课日",
    },
    "holiday": {
        "now": datetime(2026, 10, 5, 15, 0),
        "label": "长假钟",
        "desc": "2026-10-05 周一 15:00，国庆长假中，她按卡应在绍兴老家",
    },
}
CLOCK_ORDER = ["work", "holiday"]

# 2026 年国庆法定假期（10-01 周四 ~ 10-07 周三）。本地 config.toml 的 holidays 为空，
# 这里**只在内存里注入**给 Config.llm.pricing.holidays，让长假钟真正命中
# persona.get_current_activity(is_holiday=True) 与 assembler 的「放假不上课」提示。
# 绝不用它去改磁盘上的 config.toml（红线文件）。服务器侧按 DEPLOY.md 手工填。
HOLIDAYS_INJECTED = [f"2026-10-{d:02d}" for d in range(1, 8)]

# ── v3 基准（scenario L 的对照基线，取自 data/archive/benchmark_v3/）────────
V3_BASELINE = {
    "称呼率": 0.0,          # 干净库 v2 卡实测 0.0%
    "句尾问号率": 5.6,       # 干净库 v2 卡实测 5.6%
    "单气泡字数中位": 13,    # 干净库 v2 卡实测 13 字
    "回复总字数中位": 23.5,
    "平均气泡数": 1.7,
    "来源": "data/archive/benchmark_v3/card_benchmark_v3_clean_vs_polluted.md（干净库 90 次采样）",
}
# L 的通过线（任务书原文：对照 v3 基准无明显漂移）
L_PASS = {"称呼率": 10.0, "单气泡字数中位": (10, 16), "句尾问号率": 10.0}

# ── 场景库（12 个；脚本与指标口径逐字对齐 docs/BENCHMARK_V4.md）──────────
SCENARIOS: Dict[str, Dict[str, Any]] = {
    "A": {
        "title": "告别拖尾（回归 FIXES13）",
        "kind": "chat",
        "turns": ["今天聊得开心，我先去洗澡啦", "嗯嗯"],
        "key_turn": 1,
        "metrics": ["②轮 单格达标率（无问号/新场景词/叮嘱，且 沉默 或 ≤10 字 或 纯表情包）", "②轮 沉默率", "②轮 字数", "②轮 纯表情包率", "新话题/新场景词出现率"],
        "pass_line": "单格达标 ≥4/5（无问号/新场景/叮嘱，且 沉默 或 ≤10 字 或 纯表情包）；新场景词 0/5",
        "clocks": CLOCK_ORDER,
    },
    "B": {
        "title": "连环叮嘱（回归 FIXES13）",
        "kind": "chat",
        "turns": ["我待会要坐动车回家了，先收拾去了"],
        "key_turn": 0,
        "metrics": ["叮嘱小句数（祈使句且指向他未来动作；疑问句不计入）"],
        "pass_line": "叮嘱 ≤2 句的比例 ≥4/5（所有者裁决 2026-10-04：两句短关照属真人行为）",
        "clocks": CLOCK_ORDER,
    },
    "C": {
        "title": "话图一体（回归 FIXES12 文字层）",
        "kind": "chat",
        "turns": ["今天红烧肉真好吃[发来一张照片：一盘色泽红亮的红烧肉，撒着葱花，摆在白色瓷盘里。]"],
        "key_turn": 0,
        "metrics": ["两段式判定（存在『只评图不接话』气泡且存在『只接话』气泡）"],
        "pass_line": "两段式 0/5",
        "clocks": CLOCK_ORDER,
    },
    "D": {
        "title": "表情包收尾（回归 FIXES12/13）",
        "kind": "chat",
        "turns": [
            "那我先去自习了，你忙你的",
            "[发来一张照片：白色卡通小动物，紫底，头顶有'嗯嗯'抖动符号，带腮红，表情乖巧。]",
        ],
        "key_turn": 1,
        "metrics": ["②轮 单格达标率（无图内元素词/问号/新话题，叮嘱 ≤1 句）", "记录指标：②轮 沉默率、②轮 字数"],
        "pass_line": "单格达标 ≥4/5（无图内元素词、无问号/新话题、叮嘱 ≤1 句）",
        "clocks": CLOCK_ORDER,
    },
    "E": {
        "title": "梗寿命——他不续梗版（修正 FIXES13 的作废设计）",
        "kind": "chat",
        "turns": [
            "哈哈哈你还记得上次那个麻辣烫阿姨的梗吗",
            "对对，红油那位",
            "不说这个了，你下午干嘛了",
        ],
        "key_turn": 2,
        "metrics": ["③轮及之后 梗词命中率"],
        "pass_line": "③轮梗词 0/5",
        "clocks": CLOCK_ORDER,
    },
    "F": {
        "title": "沉默防滥用探针（回归 FIXES13）",
        "kind": "chat",
        "turns": ["你们乐团最近排练紧吗", "嗯嗯"],
        "key_turn": 1,
        "metrics": ["②轮 [沉默] 出现率"],
        "pass_line": "沉默 ≤10%（10 格最多 1 格；非告别语境沉默仍属异常）",
        "clocks": CLOCK_ORDER,
    },
    "G": {
        "title": "长假位置一致性（新卡锚点验证）",
        "kind": "chat",
        "turns": ["我国庆在家躺了三天了，你呢", "今天干嘛了"],
        "key_turn": None,
        "metrics": [
            "校园场景词（宿舍/琴房/银泉/临湖/启真湖/自习室）出现情况",
            "老家场景词（家/绍兴/爸妈）出现情况",
            "前后矛盾判定（一轮说在校一轮说在家）",
        ],
        "pass_line": "校园场景词 0/5；矛盾 0/5",
        # G 的判据本身就是长假锚点验证，两个钟都跑（工作日钟下说老家反而是异常，
        # 但通过线只对长假钟有断言意义，故主判定取长假钟，工作日钟作为对照呈现）
        "primary_clock": "holiday",
        "clocks": CLOCK_ORDER,
    },
    "H": {
        "title": "短假留校一致性（工作日钟为主）",
        "kind": "chat",
        "turns": ["下午有课吗"],
        "key_turn": 0,
        "metrics": ["回复与周五作息（工作日钟）一致性", "长假场景词（老家/绍兴）出现率"],
        "pass_line": "长假词 0/5",
        "primary_clock": "work",
        "clocks": CLOCK_ORDER,
    },
    "I": {
        "title": "阶段红线：相识期邀约",
        "kind": "chat",
        "turns": ["感觉咱俩挺聊得来的", "哈哈那当然"],
        "key_turn": None,
        "metrics": ["线下邀约词命中率（见面/一起吃/带你去/来我们学校/约）"],
        "pass_line": "0/5",
        "clocks": CLOCK_ORDER,
    },
    "J": {
        "title": "[图片] 占位符泄漏回归（FIXES12）",
        "kind": "proactive",
        "desire": "想拍张临湖餐厅的菜发给他",   # 沙箱预先写入的欲言又止念头
        "seed_user": "临湖那边今天人少吗",
        "seed_bot": "我刚到的时候还挺空的",
        "metrics": ["产出含 [图片]/[照片] 占位符的比例（模型裸输出层与最终实发层分别统计）"],
        "pass_line": "实发层 0/5",
        "clocks": CLOCK_ORDER,
    },
    "K": {
        "title": "日记事实归属（FIXES11 任务6 验证）",
        "kind": "diary",
        "turns": [
            "我上周去玉泉老校区听了个弦乐讲座，还挺值的",
            "你那把大提琴最近在练什么曲子呀",
            "我室友最近在学尤克里里，晚上弹得我脑壳疼",
            "对了，我下个月好像要去参加一个科创比赛",
            "你最近学校那边有什么好玩的事没",
            "我今天晚饭又是食堂随便对付的",
            "你昨天说练琴到挺晚的，是不是最近乐团挺忙",
            "行，那你早点休息吧，我也准备睡了",
        ],
        # 两个"他说的"事实点（固定在他自己的台词里，出处可由构造保证）
        "his_facts": [
            {"id": "F1", "label": "他说的·玉泉弦乐讲座", "re": r"玉泉|弦乐讲座"},
            {"id": "F2", "label": "他说的·室友学尤克里里", "re": r"尤克里里|室友"},
        ],
        # "她说的"事实点从她自己的回复里现捞（正则见 SELF_FACT_RE），
        # 捞不到就报 self_fact_found=false，绝不自动算通过
        "metrics": ["日记中事实归属正确率（对话原文 vs 日记原文并排呈现）"],
        "pass_line": "归属颠倒 0 处（以报告呈现，所有者人工裁决）",
        "clocks": ["work"],
    },
    "L": {
        "title": "经典风格基线（沿用 benchmark v3 口径，全场景汇总统计）",
        "kind": "aggregate",
        "metrics": [
            "称呼率（阿俊/小W同学出现率）",
            "气泡中位长度",
            "句尾问号率",
            "语气词密度",
            "emoji/感叹号率",
            "[沉默] 总使用率",
        ],
        "pass_line": "对照 v3 基准（称呼≈0%、气泡中位 10~16 字、句尾问号 <10%）无明显漂移",
        "clocks": CLOCK_ORDER,
    },
}
# L 汇总统计纳入的场景（K 是归属测试脚手架，8 轮固定台词不代表自然风格，剔除并留档说明）
L_AGG_SCENARIOS = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]

# ==========================================
# 客观指标口径（正则全部在此，两钟两卡同规则套用）
# ==========================================
CLAUSE_SPLIT_RE = re.compile(r"[。！？!?~～，、；;\n]+")
QUESTION_RE = re.compile(r"[？?]|哪|什么|怎么|为啥|为什么|吗$|呢$|么$")
# 字面问号（A/D 单格达标口径：整条回复不得带问号；与 QUESTION_RE 的宽疑问口径分开）
QUESTION_MARK_RE = re.compile(r"[？?]")
TONE_RE = re.compile(r"呀|啦|吧|呢|嘛|哈|哦|噢|嗯|诶|咯|哟|呗|～|~")
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿️]")

# A：编造新场景词（承自 benchmark v3 口径）
NEW_SCENE_RE = re.compile(
    r"回来了|刚回|刚到|洗完|收拾完|散完步|散步回来|湖面|晚风|风特别|灯光|宿舍|琴房|食堂|练完|楼下"
)
# B：叮嘱词（祈使/关照，指向他未来的动作）
CAUTION_RE = re.compile(
    r"记得|别忘|别睡|别熬|别光顾|路上|慢点|注意|小心|到家|到了|说一声|发消息|报平安|"
    r"早点|提前|多穿|多喝|带好|收好|眯会儿|眯一会|照顾好|安全"
)
# C：视觉词 / 味觉接话词（两段式机械判定，沿用 v3 口径）
C_VISUAL_RE = re.compile(
    r"看着|看起来|颜色|油亮|亮|葱花|瓷盘|白瓷|摆盘|这盘|盘里|盘子|照片|图里|这图|这张图"
)
C_TASTE_RE = re.compile(r"好吃|香|馋|味道|辣|下饭|想尝|看饿|会吃|爱吃|喜欢|想吃|来一口")
# D：图内元素词（任务书 V4 口径：猫/狗/卡通/画风/可爱/乖/这图/表情包）
D_IMG_RE = re.compile(r"猫|狗|卡通|画风|可爱|乖|这图|表情包")
# A：纯表情包回复（replier 落记录格式 [表情:xxx]；兼容未匹配时的模型裸标记 [sticker:xxx]）
STICKER_ONLY_RE = re.compile(r"\[(?:表情|sticker)[:：][^\]]+\]", re.IGNORECASE)
# E：梗词（任务书 V4 口径：麻辣烫/阿姨/红油/辣椒）
E_MEME_RE = re.compile(r"麻辣烫|阿姨|红油|辣椒")
# G：校园场景词 / 老家场景词（任务书 V4 口径）
CAMPUS_RE = re.compile(r"宿舍|琴房|银泉|临湖|启真湖|自习室")
HOME_RE = re.compile(r"家|绍兴|爸妈")
# H：长假词（任务书 V4 口径）
LONGHOLIDAY_RE = re.compile(r"老家|绍兴")
HOLIDAY_VIBE_RE = re.compile(r"放假|假期|长假|在家躺|休息|不上课|没课")
# I：线下邀约词（任务书 V4 口径）+ 已知的正则噪声词（仅用于标注，不改判定口径）
INVITE_RE = re.compile(r"见面|一起吃|带你去|来我们学校|约")
INVITE_NOISE_RE = re.compile(r"大约|约定|约个?时候说|约吗")
# L：称呼
NAME_RE = re.compile(r"阿俊|小W同学")
# J：占位符黑名单（含各种写法）
PLACEHOLDER_TOKENS = ["[图片]", "【图片】", "[照片]", "【照片】", "[image]", "[IMAGE]"]
# K：「她说的」事实点现捞正则（第一版，见下方 K_TOKEN_RE 的说明）
SELF_FACT_RE = re.compile(
    r"(?:我这|我在|我今天|我昨天|我刚|我最近|我这周|我们)\s*[^。！？\n]{0,12}?"
    r"(大提琴|乐团|排练|琴房|曲子|演出|文琴|临湖|论文|作业|室友|奶茶|图书馆|湖边)"
)
# K：「她说的那 1 件」的**唯一可靠捞法**——具体名词词表（地点/曲目/食物/场所）。
# 第一版用 SELF_FACT_RE（要求"我"在前 12 字内）实测 5/5 全部捞空：她实际说了
# "我今晚在东二随便拌了个面""最近在磕德沃夏克的b小调协奏曲"这类自述，
# 但"东二""德沃夏克"不在那张活动类词表里，导致 F3 整条判据悬空——
# 捞不到时报告只显示"颠倒嫌疑 0 处"，看上去像通过，实则**根本没验**。
# 改为：取她回复里出现、而他台词里**从不出现**的具体名词。
# 「只在她嘴里出现」= 这个事实的所有权无歧义，归属判定才成立。
K_TOKEN_RE = re.compile(
    r"德沃夏克|巴赫|圣桑|莫扎特|贝多芬|协奏曲|四重奏|无伴奏组曲|组曲|东二|银泉|风味|"
    r"蒙民伟楼|启真湖|临湖|琴房|芝士年糕|三明治|麻辣烫|黑天鹅|空弦|谱子|专场|老校区|"
    r"梧桐|湖边|食堂|大提琴|排练"
)

# ==========================================
# 判定阈值（所有者裁决 2026-10-04：真人标准重校准）
# ==========================================
# 首轮大考 105 格原始数据按旧口径判出 7 PASS / 4 FAIL / 1 人工裁决。所有者复核后
# 只对 A/B/D/F 四个场景下裁决，把通过线从"机械严格"校准到"真人标准"（其余场景
# 一律不动）。所有阈值集中在此，禁止散落到各判定分支里。
OWNER_RULING = "所有者裁决 2026-10-04：真人标准重校准"
PASS_RATIO = 0.80              # A/B/D 共用的比例通过线（"≥4/5"），按实际格数缩放
SHORT_CLOSE_MAX_CHARS = 6      # 记录指标口径：短收（≤6 字无问号，含 D/F 展示）
A_CLOSE_MAX_CHARS = 10         # A：收尾字数上限 6 → 10（"嗯，晚点再聊"也算正常收尾）
B_CAUTION_MAX = 2              # B：叮嘱小句上限 1 → 2（两句短关照是真人行为）
D_CAUTION_MAX = 1              # D：叮嘱小句上限（D 新达标的一个条件）
F_SILENCE_MAX_RATIO = 0.10     # F：沉默上限 0 → ≤10%（10 格最多 1 格）


# ==========================================
# 时间伪装（与 sandbox_v3_ab.py 同一手法：只在本进程内替换模块的 datetime 属性）
# ==========================================
_FAKE_NOW: datetime = CLOCKS["work"]["now"]
TIME_PATCH_MODULES = (
    "companion.db",
    "companion.assembler",
    "companion.memory",
    "companion.mood",
    "companion.affection",
    "companion.proactive",   # 场景 J 走主动消息，决策层/生成层各自读 now()
)
_ORIG_DATETIME: Dict[str, Any] = {}


class FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _FAKE_NOW if tz is None else _FAKE_NOW.replace(tzinfo=tz)

    @classmethod
    def utcnow(cls):
        return _FAKE_NOW

    @classmethod
    def today(cls):
        return _FAKE_NOW


def set_clock(clock_key: str) -> None:
    global _FAKE_NOW
    _FAKE_NOW = CLOCKS[clock_key]["now"]


def patch_time() -> None:
    for name in TIME_PATCH_MODULES:
        mod = importlib.import_module(name)
        if name not in _ORIG_DATETIME:
            _ORIG_DATETIME[name] = getattr(mod, "datetime")
        setattr(mod, "datetime", FakeDatetime)


def restore_time() -> None:
    for name, orig in _ORIG_DATETIME.items():
        setattr(importlib.import_module(name), "datetime", orig)


# ==========================================
# 沙箱：与生产 ChatSession 完全同构，只把「复制生产库」换成「全新空库建表」
# ==========================================
class FreshChatSession(ChatSession):
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


def make_config() -> Config:
    """加载真实 config.toml（真实 key），并只在内存里注入 2026 国庆假期日期。"""
    cfg = Config.load()
    cfg.character.path = CARD_DIR
    cfg.llm.pricing.holidays = list(HOLIDAYS_INJECTED)
    return cfg


async def collect_cost(db) -> Dict[str, Any]:
    """从沙箱库读出本次跑分的真实调用量与估算费用（删库前取）。"""
    rows = await db.fetchall(
        "SELECT purpose, COUNT(*) n, SUM(cost_estimate) c, "
        "SUM(prompt_tokens) p, SUM(completion_tokens) o FROM llm_calls GROUP BY purpose"
    )
    by_purpose = {
        r["purpose"]: {
            "calls": r["n"],
            "cost": round(float(r["c"] or 0.0), 6),
            "prompt_tokens": r["p"] or 0,
            "completion_tokens": r["o"] or 0,
        }
        for r in rows
    }
    total = await db.fetchone("SELECT COUNT(*) n, SUM(cost_estimate) c FROM llm_calls")
    return {
        "by_purpose": by_purpose,
        "calls": total["n"] or 0,
        "cost_cny": round(float(total["c"] or 0.0), 4),
    }


async def assert_clean_state(session) -> Dict[str, Any]:
    """干净库铁律自证：turns=0、阶段 1、复合分回初始值。取不到就如实记 False。"""
    aff = await session.affection.get_state()
    row = await session.db.fetchone("SELECT COUNT(*) n FROM turns")
    return {
        "stage": int(aff.get("stage", -1)),
        "composite": round(float(aff.get("composite", -1.0)), 2),
        "turns": row["n"] if row else -1,
        "clean": (row["n"] == 0) and int(aff.get("stage", -1)) == 1,
    }


def _kill_db(db_path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass


# ==========================================
# 场景执行器
# ==========================================
async def run_chat_scenario(session, key: str) -> List[Dict[str, str]]:
    turns: List[Dict[str, str]] = []
    for msg in SCENARIOS[key]["turns"]:
        raw_parts: List[str] = []
        clean = await session.handle_input(msg, on_piece=raw_parts.append)
        turns.append({"user": msg, "raw": "".join(raw_parts).strip(), "reply": clean})
    return turns


async def run_diary_scenario(session, key: str) -> Dict[str, Any]:
    """K 场景：喂 8 轮固定对话 → 触发日记归档 → 读 diary 表新行。"""
    turns = await run_chat_scenario(session, key)
    # 归档是 save_turn_pair 里 create_task 的后台任务，给它时间跑完；
    # 再显式调一次（内部有 _archive_lock + 游标，幂等）保证一定归档成功。
    await asyncio.sleep(0.3)
    try:
        await session.memory.check_and_trigger_diary_archive()
    except Exception as e:
        turns.append(
            {"user": "[归档异常]", "raw": f"{type(e).__name__}: {e}", "reply": f"[失败] {e}"}
        )
    await asyncio.sleep(0.2)
    rows = await session.db.fetchall(
        "SELECT id, content, importance, sentiment, created_at FROM diary ORDER BY id ASC"
    )
    arch = await session.db.fetchall(
        "SELECT id, content FROM diary_archive ORDER BY id ASC"
    )
    return {
        "turns": turns,
        "diary": [dict(r) for r in rows],
        "diary_archive": [dict(r) for r in arch],
    }


def _sent_text(chunks) -> str:
    return "\n".join(
        c["content"] if c["type"] == "text" else f"[表情:{c.get('desc', '')}]" for c in chunks
    )


def _find_placeholders(text: str) -> List[str]:
    return [t for t in PLACEHOLDER_TOKENS if t in (text or "")]


async def run_proactive_scenario(session, key: str, clock_key: str) -> Dict[str, Any]:
    """J 场景：欲言又止池写入"拍照片"念头 → 真实主动消息 → 强制最坏情况直调。"""
    sc = SCENARIOS[key]
    fake_now = CLOCKS[clock_key]["now"]

    sent: List[Dict[str, Any]] = []
    llm_calls: List[Dict[str, str]] = []

    async def collect(chunk):
        sent.append(chunk)

    _real_chat = session.gateway.chat

    async def spy_chat(*args, **kwargs):
        resp = await _real_chat(*args, **kwargs)
        llm_calls.append({"purpose": kwargs.get("purpose"), "response": str(resp)[:1200]})
        return resp

    session.gateway.chat = spy_chat
    # 只放开免打扰时段闸门（生产的 [0,8] 会拦掉本场景）；其余配置与生产一致
    replier = Replier(ReplyConfig(chunk_delay_min=0.0, chunk_delay_max=0.0), session.stickers)
    scheduler = ProactiveScheduler(
        config=ProactiveConfig(enabled=True, quiet_hours=[]),
        persona=session.persona,
        affection=session.affection,
        mood=session.mood,
        memory=session.memory,
        stickers=session.stickers,
        replier=replier,
        gateway=session.gateway,
        db=session.db,
        send_msg_fn=collect,
        assembler=session.assembler,
        holidays_provider=session.config.get_holidays,
    )

    out: Dict[str, Any] = {
        "turns": [],
        "attempts": [],
        "choice_a": False,
        "trigger_sent": "",
        "forced": {},
    }
    try:
        # 1) 预置欲言又止念头 + 种子历史（复用 smoke_fixes12 的手法）
        #    种子历史必须落在 2 小时前：规则闸门第 2 条是「距机器人上次发言 < 60 分钟即拦截」，
        #    若把种子时间戳打在伪装钟的同一刻，闸门会零成本地把整条链拦掉，
        #    决策层/生成层压根不会被调用——那正是"断言自动成立"的假通过。
        seed_at = (fake_now - timedelta(hours=2)).strftime(TIME_FORMAT)
        await session.db.execute(
            "INSERT INTO suppressed_desires (content, created_at) VALUES (?, ?)",
            (sc["desire"], fake_now.strftime(TIME_FORMAT)),
        )
        for role, content in (("user", sc["seed_user"]), ("assistant", sc["seed_bot"])):
            await session.db.execute(
                "INSERT INTO turns (role, content, proactive, has_image, created_at) "
                "VALUES (?, ?, 0, 0, ?)",
                (role, content, seed_at),
            )
        out["turns"].append(
            {
                "user": f"[预置欲言又止] {sc['desire']}（+ 种子历史：{sc['seed_user']} / {sc['seed_bot']}）",
                "raw": "",
                "reply": "",
            }
        )

        # 2) 真实主动消息：最多 3 次，直到决策层选 A 且真的发出内容
        for attempt in range(1, 4):
            llm_calls.clear()
            # _select_topic_material 用掉欲言又止池后即删除，后续尝试需重新写入，
            # 否则第 2/3 次尝试其实在测"作息活动"素材而不是"拍照片"最坏情况
            await session.db.execute(
                "INSERT INTO suppressed_desires (content, created_at) VALUES (?, ?)",
                (sc["desire"], fake_now.strftime(TIME_FORMAT)),
            )
            await scheduler.reset_unanswered_count()
            sent.clear()
            # 记录规则闸门判定：让报告能证明这条链是真跑到了决策层还是被零成本拦下
            gate_blocked, gate_reason = await scheduler._check_rules_gate()
            await scheduler.trigger_cycle()

            attempt_sent = _sent_text(sent)
            decision = next((c for c in llm_calls if c["purpose"] == "proactive_decision"), None)
            generation = next((c for c in llm_calls if c["purpose"] == "proactive_message"), None)
            choice = "?"
            if decision:
                try:
                    choice = json.loads(decision["response"]).get("choice", "?")
                except Exception:
                    choice = "?"
            out["attempts"].append(
                {
                    "attempt": attempt,
                    "gate_blocked": gate_blocked,
                    "gate_reason": gate_reason,
                    "choice": choice,
                    "decision_raw": decision["response"] if decision else None,
                    "generation_raw": generation["response"] if generation else None,
                    "sent_text": attempt_sent,
                    "raw_placeholders": _find_placeholders(
                        generation["response"] if generation else ""
                    ),
                    "sent_placeholders": _find_placeholders(attempt_sent),
                }
            )
            if str(choice).upper() == "A" and attempt_sent:
                out["choice_a"] = True
                break

        out["trigger_sent"] = next(
            (a["sent_text"] for a in reversed(out["attempts"]) if a["sent_text"]), ""
        )

        # 3) 强制最坏情况直调：绕过决策闸门，把 topic_material 直接填成"拍照片"计划。
        #    决策层选什么是模型的自由意志，不能只靠它配合来验收——这一步才是真正的判据。
        gen_turns = await session.memory.get_recent_turns(limit=8)
        gen_user_prompt = PROACTIVE_GENERATE_PROMPT.format(
            user_address=session.persona.user_address,
            current_time=fake_now.strftime(TIME_FORMAT),
            topic_material=sc["desire"],
            recent_chat=format_recent_chat(gen_turns, max_chars=60),
            stickers_list="、".join(session.stickers.get_prompt_sticker_list()),
        )
        forced_system = await session.assembler.assemble_system_prompt("")
        forced_raw = await session.gateway.chat(
            messages=[
                {"role": "system", "content": forced_system},
                {"role": "user", "content": gen_user_prompt},
            ],
            model=session.gateway.config.text_model,
            temperature=0.8,
            purpose="proactive_message",
        )
        forced_chunks, forced_record = replier.parse_reply(str(forced_raw), source="proactive")
        out["forced"] = {
            "note": "绕过决策闸门的生成层直调，topic_material 强制为『拍照片』计划（最坏情况）",
            "generation_raw": str(forced_raw),
            "raw_placeholders": _find_placeholders(str(forced_raw)),
            "sent_chunks": [dict(c) for c in forced_chunks],
            "sent_text": _sent_text(forced_chunks),
            "record": forced_record,
            "sent_placeholders": _find_placeholders(_sent_text(forced_chunks)),
            "record_placeholders": _find_placeholders(forced_record),
        }
    finally:
        session.gateway.chat = _real_chat
    return out


async def run_cell_inner(key: str, clock_key: str, run_idx: int) -> Dict[str, Any]:
    db_path = f"data/bench_v4_{key}_{clock_key}_{run_idx}.db"
    cfg = make_config()
    session = FreshChatSession(config=cfg, sandbox_db_path=db_path)
    cell: Dict[str, Any] = {
        "scenario": key,
        "clock": clock_key,
        "run": run_idx,
        "card": CARD_DIR,
        "turns": [],
        "clean_state": {},
        "cost": {},
        "error": None,
        "attempt_no": 1,
    }
    try:
        await session.initialize()
        cell["clean_state"] = await assert_clean_state(session)
        sc = SCENARIOS[key]
        if sc["kind"] == "chat":
            cell["turns"] = await run_chat_scenario(session, key)
        elif sc["kind"] == "diary":
            res = await run_diary_scenario(session, key)
            cell["turns"] = res["turns"]
            cell["diary"] = res["diary"]
            cell["diary_archive"] = res["diary_archive"]
        elif sc["kind"] == "proactive":
            res = await run_proactive_scenario(session, key, clock_key)
            cell.update(res)
        cell["cost"] = await collect_cost(session.db)
    except Exception as e:
        cell["error"] = f"{type(e).__name__}: {e}"
        cell["turns"].append({"user": "[ERROR]", "raw": cell["error"], "reply": f"[失败] {e}"})
    finally:
        try:
            await session.close()
        except Exception:
            pass
        _kill_db(db_path)
    return cell


async def run_cell(key: str, clock_key: str, run_idx: int, sem, results, log) -> None:
    async with sem:
        for attempt in (1, 2):
            cell = await run_cell_inner(key, clock_key, run_idx)
            # 注意：字段名是 attempt_no 而不是 attempts——后者是 J 场景主动消息
            # 每次决策尝试的日志列表（list），同名会把日志覆盖成整数导致复算崩溃。
            cell["attempt_no"] = attempt
            if cell["error"] is None:
                break
            log.append(f"  ! 场景{key}/{clock_key}/第{run_idx}次 第{attempt}轮异常：{cell['error']}")
        results[(key, clock_key, run_idx)] = cell
        n_turn = len(cell.get("turns", []))
        log.append(
            f"  完成 场景{key}/{clock_key}/第{run_idx}次："
            f"{n_turn} 轮 | 调用 {cell.get('cost', {}).get('calls', 0)}"
            f" | 费用 ¥{cell.get('cost', {}).get('cost_cny', 0.0)}"
        )


# ==========================================
# 指标计算（纯函数，--from-raw 复算走同一套代码）
# ==========================================
def chars(text: str) -> int:
    return len(re.sub(r"\s", "", text or ""))


def bubbles(text: str) -> List[str]:
    return [ln.strip() for ln in (text or "").split("\n") if ln.strip()]


def clauses(text: str) -> List[str]:
    return [s.strip() for s in CLAUSE_SPLIT_RE.split(text or "") if s.strip()]


def is_silent(text: str) -> bool:
    return (text or "").strip() == SILENCE_TOKEN


def is_short_close(text: str, max_chars: int = SHORT_CLOSE_MAX_CHARS) -> bool:
    """短收：≤max_chars 字且无问号。沉默单独统计，不重复计入。

    默认 6 字是记录指标口径（D/F 展示）；A 的单格达标按所有者裁决放宽到 10 字。
    """
    if is_silent(text):
        return False
    return chars(text) <= max_chars and "？" not in (text or "")


def stop_ok(text: str) -> bool:
    """沉默 + 短收（"该断就断"，作为 A/D 的记录指标保留输出）。"""
    return is_silent(text) or is_short_close(text)


def has_question_mark(text: str) -> bool:
    """字面问号（A/D 单格达标的"无问号"口径）。"""
    return bool(QUESTION_MARK_RE.search(text or ""))


def caution_clauses(text: str) -> List[str]:
    """叮嘱小句：命中叮嘱词表且非疑问句（疑问式关切不计入；B/D 共用口径）。"""
    return [c for c in clauses(text) if CAUTION_RE.search(c) and not QUESTION_RE.search(c)]


def is_pure_sticker(text: str) -> bool:
    """纯表情包回复：整条实发只有一个表情包段（replier 落记录为 [表情:xxx]）。"""
    b = bubbles(text)
    return len(b) == 1 and bool(STICKER_ONLY_RE.fullmatch(b[0]))


def a_close_ok(text: str) -> bool:
    """A 单格达标（所有者裁决 2026-10-04：真人标准重校准）。

    满足全部：①无问号、无新场景词、叮嘱小句 =0；
    ②且满足 沉默 / 字数 ≤10 / 纯表情包 三者之一。
    原口径要求"≤6 字短收"过严——单发一个表情包或"嗯，晚点再聊"都算正常收尾。
    """
    if has_question_mark(text) or NEW_SCENE_RE.search(text or "") or caution_clauses(text):
        return False
    return is_silent(text) or chars(text) <= A_CLOSE_MAX_CHARS or is_pure_sticker(text)


def d_close_ok(text: str) -> bool:
    """D 单格达标（所有者裁决 2026-10-04：真人标准重校准）。

    满足全部：无图内元素词（口径不变）、无问号、无新场景词、叮嘱小句 ≤1。
    不再把"沉默或短收"当判定条件——沉默率/字数只作记录指标输出。
    """
    if D_IMG_RE.search(text or "") or has_question_mark(text):
        return False
    if NEW_SCENE_RE.search(text or ""):
        return False
    return len(caution_clauses(text)) <= D_CAUTION_MAX


def silence_ratio_ok(hit: int, total: int, max_ratio: float = F_SILENCE_MAX_RATIO) -> Optional[bool]:
    """F 的 ≤max_ratio 通过线：按实际格数向下取整缩放（10 格 → 最多 1 格）。"""
    if not total:
        return None
    return hit <= math.floor(max_ratio * total)


def key_turns(cell: Dict[str, Any], key: str) -> List[str]:
    """场景声明的关键轮回复（索引越界返回空串，不静默取错轮）。"""
    turns = cell.get("turns", [])
    sc = SCENARIOS[key]
    idx = sc.get("key_turn")
    if idx is None:
        return [t.get("reply", "") for t in turns if not t.get("user", "").startswith("[")]
    if idx < len(turns):
        return [turns[idx].get("reply", "")]
    return [""]


def eval_cell(key: str, cell: Dict[str, Any]) -> Dict[str, Any]:
    """单格逐指标数值。返回 {"values": {...}, "texts": {...}}。"""
    replies = key_turns(cell, key)
    all_replies = [
        t.get("reply", "") for t in cell.get("turns", []) if not t.get("user", "").startswith("[")
    ]
    v: Dict[str, Any] = {}
    tx: Dict[str, Any] = {}

    if key == "A":
        r = replies[0] if replies else ""
        cautions = caution_clauses(r)
        v = {
            "silent": is_silent(r),
            "pure_sticker": is_pure_sticker(r),
            "short_10": is_short_close(r, A_CLOSE_MAX_CHARS),
            "has_question": has_question_mark(r),
            "n_cautions": len(cautions),
            "cautions": cautions,
            "chars": chars(r),
            "new_scene_hits": sorted(set(NEW_SCENE_RE.findall(r))),
            "close_ok": a_close_ok(r),
        }
        v["new_scene_hit"] = bool(v["new_scene_hits"])
        tx = {"②轮": r}
    elif key == "B":
        r = replies[0] if replies else ""
        c = caution_clauses(r)
        v = {"cautions": c, "n_cautions": len(c)}
        tx = {"①轮": r}
    elif key == "C":
        r = replies[0] if replies else ""
        pure_vis, taste, vis_all = [], [], []
        for b in bubbles(r):
            has_vis, has_taste = bool(C_VISUAL_RE.search(b)), bool(C_TASTE_RE.search(b))
            if has_vis:
                vis_all.append(b)
                if not has_taste:
                    pure_vis.append(b)
            if has_taste:
                taste.append(b)
        v = {
            "pure_visual_bubbles": pure_vis,
            "taste_bubbles": taste,
            "visual_bubbles": vis_all,
            "two_stage": bool(pure_vis and taste),
        }
        tx = {"①轮": r}
    elif key == "D":
        r = replies[0] if replies else ""
        cautions = caution_clauses(r)
        v = {
            "img_words": sorted(set(D_IMG_RE.findall(r))),
            "has_question": has_question_mark(r),
            "n_cautions": len(cautions),
            "cautions": cautions,
            "chars": chars(r),
            "new_scene_hits": sorted(set(NEW_SCENE_RE.findall(r))),
            "close_ok": d_close_ok(r),
            # 记录指标（不再参与判定）：沉默/短收/该断就断；字数在 chars
            "silent": is_silent(r),
            "short": is_short_close(r),
            "stop_ok": stop_ok(r),
        }
        v["img_hit"] = bool(v["img_words"])
        v["new_scene_hit"] = bool(v["new_scene_hits"])
        tx = {"②轮": r}
    elif key == "E":
        r = replies[0] if replies else ""
        v = {"meme_words": sorted(set(E_MEME_RE.findall(r)))}
        v["meme_hit"] = bool(v["meme_words"])
        tx = {"③轮": r}
    elif key == "F":
        r = replies[0] if replies else ""
        v = {"silent": is_silent(r), "short": is_short_close(r), "chars": chars(r)}
        tx = {"②轮": r}
    elif key == "G":
        rows = []
        campus, home = [], []
        for i, t in enumerate([t for t in cell.get("turns", []) if not t.get("user", "").startswith("[")], start=1):
            r = t.get("reply", "")
            c_hits = sorted(set(CAMPUS_RE.findall(r)))
            h_hits = sorted(set(HOME_RE.findall(r)))
            if c_hits:
                campus.append({"turn": i, "words": c_hits})
            if h_hits:
                home.append({"turn": i, "words": h_hits})
            rows.append({"turn": i, "reply": r})
        contradiction = any(
            c["turn"] != h["turn"] for c in campus for h in home
        )
        v = {
            "campus": campus,
            "home": home,
            "campus_hit": bool(campus),
            "contradiction": contradiction,
        }
        tx = {"两轮": rows}
    elif key == "H":
        r = replies[0] if replies else ""
        v = {
            "longholiday_words": sorted(set(LONGHOLIDAY_RE.findall(r))),
            "holiday_vibe_words": sorted(set(HOLIDAY_VIBE_RE.findall(r))),
            "chars": chars(r),
        }
        v["hit"] = bool(v["longholiday_words"])
        tx = {"①轮": r}
    elif key == "I":
        hits, noise = [], []
        for r in all_replies:
            for m in INVITE_RE.finditer(r):
                hits.append({"reply": r, "word": m.group(0), "ctx": r[max(0, m.start() - 8): m.end() + 8]})
            for m in INVITE_NOISE_RE.finditer(r):
                noise.append({"reply": r, "word": m.group(0)})
        v = {"invite_hits": hits, "noise_suspects": noise, "n_replies": len(all_replies)}
        v["hit"] = bool(hits)
        tx = {"两轮": [{"turn": i + 1, "reply": r} for i, r in enumerate(all_replies)]}
    elif key == "J":
        attempts = cell.get("attempts") or []
        trig_sent = cell.get("trigger_sent", "")
        forced = cell.get("forced", {}) or {}
        f_sent = forced.get("sent_text", "")
        combined_sent = (trig_sent or "") + "\n" + (f_sent or "")
        v = {
            "choice_a": bool(cell.get("choice_a")),
            "attempts": [
                {
                    "attempt": a.get("attempt"),
                    "choice": a.get("choice"),
                    "raw_placeholders": a.get("raw_placeholders", []),
                    "sent_placeholders": a.get("sent_placeholders", []),
                }
                for a in attempts
            ],
            "trigger_sent": trig_sent,
            "forced_generation_raw": forced.get("generation_raw", ""),
            "forced_raw_placeholders": forced.get("raw_placeholders", []),
            "forced_sent_text": f_sent,
            "forced_sent_placeholders": forced.get("sent_placeholders", []),
            "forced_record_placeholders": forced.get("record_placeholders", []),
            "raw_layer_hits": bool(forced.get("raw_placeholders"))
            or any(a.get("raw_placeholders") for a in attempts),
            "sent_layer_hits": bool(_find_placeholders(combined_sent)),
        }
        tx = {"实发合并": combined_sent, "强制生成层裸输出": forced.get("generation_raw", "")}
    elif key == "K":
        v = eval_k(cell)
    return {"values": v, "texts": tx}


# ── K 场景：事实归属（机械线索 + 原文并排，判定权交所有者）──────────────
def _diary_sentences(diary_rows: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for row in diary_rows:
        for s in re.split(r"[。！？\n]", row.get("content", "")):
            if s.strip():
                out.append(s.strip())
    return out


def _find_self_fact(turns: List[Dict[str, str]]) -> Optional[Dict[str, Any]]:
    """从她自己的回复里现捞一条自述事实（她说的那 1 件）。捞不到就返回 None。

    判据：具体名词出现在她的回复里，且**从未出现在他的任何一条台词里**。
    这样"这个事实归谁"就没有歧义，日记里主语是"我"即正确、是"他"即颠倒嫌疑。
    要求该名词所在句子含"我"，避免捞到她替别人打的比方。
    """
    his_text = "\n".join(t.get("user", "") for t in turns)
    for i, t in enumerate(turns, start=1):
        r = t.get("reply", "")
        if not r or r.startswith("[失败]") or is_silent(r):
            continue
        for sent in re.split(r"[。！？\n]", r):
            sent = sent.strip()
            if not sent or "我" not in sent:
                continue
            for m in K_TOKEN_RE.finditer(sent):
                token = m.group(0)
                if token in his_text:
                    continue  # 他也说过 → 所有权有歧义，跳过
                return {
                    "turn": i,
                    "matched": token,
                    "keyword": token,
                    "reply": r,
                    "sentence": sent,
                }
    return None


def eval_k(cell: Dict[str, Any]) -> Dict[str, Any]:
    sc = SCENARIOS["K"]
    turns = [t for t in cell.get("turns", []) if not t.get("user", "").startswith("[")]
    diary_rows = cell.get("diary", []) or []
    sents = _diary_sentences(diary_rows)
    diary_all = "\n".join(r.get("content", "") for r in diary_rows)

    facts: List[Dict[str, Any]] = []
    # 两个"他说的"事实：出处由脚本构造保证
    for hf in sc["his_facts"]:
        pat = re.compile(hf["re"])
        src_turn = next(
            (i for i, t in enumerate(turns, start=1) if t.get("user") and pat.search(t["user"])),
            None,
        )
        related = [s for s in sents if pat.search(s)]
        hint, why = "未提及", "日记里没找到这个事实点"
        for s in related:
            if re.search(r"他|机主", s) and not re.search(r"我", s):
                hint, why = "归属正确", f"日记句子主语为『他/机主』：{s}"
                break
            if re.search(r"我", s) and not re.search(r"他|机主", s):
                hint, why = "颠倒嫌疑", f"日记句子主语为『我』（=她）：{s}"
                break
        else:
            if related:
                hint, why = "需人工判读", "日记提到该事实但主语同时含『我』和『他/机主』：" + " | ".join(related)
        facts.append(
            {
                "id": hf["id"],
                "label": hf["label"],
                "expected_owner": "他（机主）",
                "source_turn": src_turn,
                "source_text": turns[src_turn - 1]["user"] if src_turn else None,
                "diary_sentences": related,
                "mechanical_hint": hint,
                "hint_reason": why,
            }
        )

    # "她说的"那 1 件：从她自己的回复现捞
    sf = _find_self_fact(turns)
    if sf is None:
        facts.append(
            {
                "id": "F3",
                "label": "她说的·（未捞到自述事实）",
                "expected_owner": "她（青梓）",
                "source_turn": None,
                "source_text": None,
                "diary_sentences": [],
                "mechanical_hint": "无法判定",
                "hint_reason": "本采样里她没有说出可判定的自述事实，本格不计入通过线，需人工裁决",
            }
        )
        self_found = False
    else:
        pat = re.compile(re.escape(sf["keyword"]))
        related = [s for s in sents if pat.search(s)]
        hint, why = "未提及", "日记里没找到这个事实点"
        for s in related:
            if re.search(r"我", s) and not re.search(r"他|机主", s):
                hint, why = "归属正确", f"日记句子主语为『我』（=她本人）：{s}"
                break
            if re.search(r"他|机主", s) and not re.search(r"我", s):
                hint, why = "颠倒嫌疑", f"日记句子主语为『他/机主』：{s}"
                break
        else:
            if related:
                hint, why = "需人工判读", "日记提到该事实但主语混含『我』与『他/机主』：" + " | ".join(related)
        facts.append(
            {
                "id": "F3",
                "label": "她说的·" + sf["matched"],
                "expected_owner": "她（青梓）",
                "source_turn": sf["turn"],
                "source_text": sf["sentence"],
                "diary_sentences": related,
                "mechanical_hint": hint,
                "hint_reason": why,
            }
        )
        self_found = True

    suspects = sum(1 for f in facts if f["mechanical_hint"] == "颠倒嫌疑")
    return {
        "facts": facts,
        "self_fact_found": self_found,
        "inversion_suspects": suspects,
        "diary_rows": len(diary_rows),
        "diary_chars": chars(diary_all),
    }


# ==========================================
# 汇总与判定
# ==========================================
def _cells_for(data: Dict[str, Any], key: str, clock: Optional[str] = None) -> List[Dict[str, Any]]:
    out = []
    for ck in CLOCK_ORDER:
        if clock and ck != clock:
            continue
        if ck not in SCENARIOS[key]["clocks"]:
            continue
        for run in range(1, data["meta"]["runs"] + 1):
            c = data["cells"].get(f"{key}|{ck}|{run}")
            if c:
                out.append(c)
    return out


def _primary_cells(data: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    pc = SCENARIOS[key].get("primary_clock")
    if pc:
        return _cells_for(data, key, pc)
    return _cells_for(data, key)


def expected_cells(data: Dict[str, Any], key: str) -> int:
    """该场景判定所用的应跑格数。

    判定走「主判钟」的场景（G/H）只按主判钟那一列算，否则会把对照钟的格数
    误当成缺失数据，报出假的『数据不足』。"""
    sc = SCENARIOS[key]
    if sc.get("primary_clock"):
        return data["meta"]["runs"]
    return data["meta"]["runs"] * len(sc["clocks"])


def summarize(data: Dict[str, Any]) -> Dict[str, Any]:
    """把逐格数值聚成逐场景汇总 + PASS/FAIL。L 为全场景汇总（零额外调用）。

    两条防"自动通过"铁律（FIXES12 冒烟踩过的坑）：
      1. 数据不足的格数下，"0 命中"不等于"通过"——0 格时一律判「未跑」，
         少于应跑格数时判「数据不足」，绝不因为 0==0 报 PASS；
      2. "≥4/5" 这类比例通过线按实际格数缩放（ceil(0.8×n)），
         否则 --runs 1 的冒烟会因为绝对阈值 4 而把全对的样本判成 FAIL。
    """
    evals: Dict[str, Any] = {}
    for key in SCENARIOS:
        if SCENARIOS[key]["kind"] == "aggregate":
            continue
        per: List[Dict[str, Any]] = []
        for c in _cells_for(data, key):
            per.append({"clock": c["clock"], "run": c["run"], **eval_cell(key, c)})
        evals[key] = per
    data["evaluations"] = evals

    verdicts: Dict[str, Any] = {}

    def need80(name: str, hit: int, total: int) -> Dict[str, Any]:
        """≥80% 的比例通过线（任务书 "≥4/5"），按实际格数缩放阈值。"""
        thr = math.ceil(0.8 * total) if total else 0
        return {
            "name": name,
            "actual": f"{hit}/{total}",
            "required": f"≥{thr}/{total}",
            "pass": (hit >= thr) if total else None,
        }

    def need_zero(name: str, hit: int, total: int) -> Dict[str, Any]:
        """0 命中通过线（任务书 "0/5"），无数据时 pass=None 而不是 True。"""
        return {
            "name": name,
            "actual": f"{hit}/{total}",
            "required": f"0/{total}",
            "pass": (hit == 0) if total else None,
        }

    def need_ratio(name: str, hit: int, total: int, max_ratio: float) -> Dict[str, Any]:
        """≤max_ratio 通过线（F 的 "≤10%"），按实际格数向下取整缩放（10 格 → ≤1 格）。"""
        thr = math.floor(max_ratio * total) if total else 0
        return {
            "name": name,
            "actual": f"{hit}/{total}",
            "required": f"≤{thr}/{total}",
            "pass": silence_ratio_ok(hit, total, max_ratio),
        }

    def cnt(key: str, field: str, clock: Optional[str] = None, pred=None) -> int:
        cells = _cells_for(data, key, clock)
        tot = 0
        for c in cells:
            val = eval_cell(key, c)["values"].get(field)
            ok = pred(val) if pred else bool(val)
            if ok:
                tot += 1
        return tot

    # A（单格达标重定义，所有者裁决 2026-10-04）
    a_cells = _primary_cells(data, "A")
    verdicts["A"] = {
        "checks": [
            need80(
                "单格达标（无问号/新场景/叮嘱，且 沉默或≤10字或纯表情包）≥4/5",
                sum(1 for c in a_cells if eval_cell("A", c)["values"]["close_ok"]),
                len(a_cells),
            ),
            need_zero("新场景词 0/5", cnt("A", "new_scene_hit"), len(a_cells)),
        ],
        "observability": {
            "沉默（记录）": f"{cnt('A', 'silent')}/{len(a_cells)}",
            "纯表情包（记录）": f"{cnt('A', 'pure_sticker')}/{len(a_cells)}",
            "字数（记录，逐格）": [eval_cell("A", c)["values"]["chars"] for c in a_cells],
        },
        "note": OWNER_RULING + "：原口径『≤6 字短收』过严，单发一个表情包或『嗯，晚点再聊』都算正常收尾。",
    }
    # B（提醒上限 1 → 2 句，所有者裁决 2026-10-04）
    b_cells = _cells_for(data, "B")
    ok_b = sum(
        1
        for c in b_cells
        if eval_cell("B", c)["values"]["n_cautions"] <= B_CAUTION_MAX
    )
    verdicts["B"] = {
        "checks": [need80(f"叮嘱 ≤{B_CAUTION_MAX} 句的比例 ≥4/5", ok_b, len(b_cells))],
        "observability": {
            "逐格叮嘱小句数": [eval_cell("B", c)["values"]["n_cautions"] for c in b_cells],
        },
        "note": OWNER_RULING + "：两句短关照（如『路上注意安全，到家了说一声』）是真人行为，3 句以上才算妈味。",
    }
    # C
    verdicts["C"] = {
        "checks": [need_zero("两段式 0/5", cnt("C", "two_stage"), len(_cells_for(data, "C")))]
    }
    # D（单格达标重定义，所有者裁决 2026-10-04）
    d_cells = _cells_for(data, "D")
    d_ok = sum(1 for c in d_cells if eval_cell("D", c)["values"]["close_ok"])
    d_silent = cnt("D", "silent")
    verdicts["D"] = {
        "checks": [
            need80(
                "单格达标（无图内元素词/问号/新话题，叮嘱≤1）≥4/5",
                d_ok,
                len(d_cells),
            )
        ],
        "observability": {
            "沉默率（记录指标）": (
                f"{d_silent}/{len(d_cells)}（{round(100.0 * d_silent / len(d_cells), 1)}%）"
                if d_cells
                else "—"
            ),
            "字数（记录指标，逐格）": [eval_cell("D", c)["values"]["chars"] for c in d_cells],
            "图内元素词命中（记录指标）": f"{cnt('D', 'img_hit')}/{len(d_cells)}",
            "说明": OWNER_RULING + "：不再把『沉默或短收』当判定条件，仅保留为记录指标。",
        },
    }
    # E
    verdicts["E"] = {
        "checks": [need_zero("③轮梗词 0/5", cnt("E", "meme_hit"), len(_cells_for(data, "E")))]
    }
    # F（沉默上限 0 → ≤10%，所有者裁决 2026-10-04）
    f_cells = _cells_for(data, "F")
    f_silent_cells = [c for c in f_cells if eval_cell("F", c)["values"]["silent"]]
    f_thr = math.floor(F_SILENCE_MAX_RATIO * len(f_cells)) if f_cells else 0
    verdicts["F"] = {
        "checks": [
            need_ratio(
                f"②轮沉默 ≤10%（最多 {f_thr} 格）",
                len(f_silent_cells),
                len(f_cells),
                F_SILENCE_MAX_RATIO,
            )
        ],
        "silence_contexts": [
            {
                "clock": CLOCKS[c["clock"]]["label"],
                "run": c["run"],
                "语境": [
                    t["user"] for t in c.get("turns", []) if not t["user"].startswith("[")
                ],
                "该格回复": eval_cell("F", c)["texts"]["②轮"],
            }
            for c in f_silent_cells
        ],
        "note": OWNER_RULING + "：非告别语境沉默仍属异常，但 1/10 的边界波动可接受。",
    }
    # G（主判定取长假钟）
    g_cells = _primary_cells(data, "G")
    verdicts["G"] = {
        "primary_clock": "holiday",
        "checks": [
            need_zero(
                "校园场景词 0/5（长假钟）",
                sum(1 for c in g_cells if eval_cell("G", c)["values"]["campus_hit"]),
                len(g_cells),
            ),
            need_zero(
                "前后矛盾 0/5（长假钟）",
                sum(1 for c in g_cells if eval_cell("G", c)["values"]["contradiction"]),
                len(g_cells),
            ),
        ],
    }
    # H（主判定取工作日钟）
    h_cells = _primary_cells(data, "H")
    verdicts["H"] = {
        "primary_clock": "work",
        "checks": [
            need_zero(
                "长假词 0/5（工作日钟）",
                sum(1 for c in h_cells if eval_cell("H", c)["values"]["hit"]),
                len(h_cells),
            )
        ],
    }
    # I
    verdicts["I"] = {
        "checks": [need_zero("线下邀约词 0/5", cnt("I", "hit"), len(_cells_for(data, "I")))]
    }
    # J
    j_cells = _cells_for(data, "J")
    j_sent = sum(1 for c in j_cells if eval_cell("J", c)["values"]["sent_layer_hits"])
    j_raw = sum(1 for c in j_cells if eval_cell("J", c)["values"]["raw_layer_hits"])
    j_choice_a = sum(1 for c in j_cells if eval_cell("J", c)["values"]["choice_a"])
    j_gate_blocked = sum(
        1
        for c in j_cells
        for a in (c.get("attempts") or [])
        if a.get("gate_blocked")
    )
    j_attempts = sum(len(c.get("attempts") or []) for c in j_cells)
    verdicts["J"] = {
        "checks": [
            need_zero("实发层占位符 0/5", j_sent, len(j_cells)),
        ],
        "observability": {
            "决策层选 A 的采样数": f"{j_choice_a}/{len(j_cells)}",
            "规则闸门拦截的尝试数": f"{j_gate_blocked}/{j_attempts}（被拦=这条链压根没到决策层）",
            "裸输出层含占位符的采样数": f"{j_raw}/{len(j_cells)}",
            "说明": (
                "决策层闸门选不到 A 时『产出不含占位符』会自动成立，故每个采样都额外做了"
                "绕过闸门的强制最坏情况直调；实发层判定以『闸门产出 + 强制直调产出』合并为准。"
            ),
        },
    }
    # K：报告呈现，所有者人工裁决
    k_cells = _cells_for(data, "K")
    k_vals = [eval_cell("K", c)["values"] for c in k_cells]
    k_sus = sum(v["inversion_suspects"] for v in k_vals)
    k_found = sum(1 for v in k_vals if v["self_fact_found"])
    # 关键：日记是一条 ~40 字的压缩摘要，**大多数事实点压根不会被写进去**。
    # 所以"颠倒嫌疑 0 处"在很大程度上是"没提到"造成的，不等于归属正确。
    # 这里把线索分档统计，避免把"未提及"误读成"通过"。
    hint_tally: Dict[str, int] = {}
    for v in k_vals:
        for f in v["facts"]:
            hint_tally[f["mechanical_hint"]] = hint_tally.get(f["mechanical_hint"], 0) + 1
    verdicts["K"] = {
        "manual": True,
        "checks": [
            {
                "name": "归属颠倒 0 处（机械线索，需人工裁决）",
                "actual": f"颠倒嫌疑 {k_sus} 处 / 捞到自述事实的采样 {k_found}/{len(k_cells)}",
                "pass": None,
            }
        ],
        "hint_tally": hint_tally,
        "hint_reading": (
            f"三个事实点 × {len(k_cells)} 个采样共 {sum(hint_tally.values())} 条线索："
            + "，".join(f"{k} {v} 条" for k, v in sorted(hint_tally.items(), key=lambda x: -x[1]))
            + "。『未提及』占多数，说明日记作为一条压缩摘要本就写不下这些细节，"
            "因此『颠倒嫌疑 0 处』**主要来自遗漏而非归属正确**，不能当通过读。"
        ),
        "note": "通过线以报告并排呈现的原文为准，最终裁判是所有者本人；机械线索只作定位用。",
        "method": (
            "F1/F2 是他台词里写死的事实点；F3 从她回复里现捞——取一个只出现在她嘴里、"
            "他从未说过的具体名词（地点/曲目/食物），再回日记里查主语是他还是我。"
            "第一版用『我+活动词表』的写法实测 5/5 捞空（她说的 东二/德沃夏克 不在词表里），"
            "会让这条判据悬空成假通过，故已换成上述只在她嘴里出现的判据。"
        ),
    }
    # L：全场景汇总
    verdicts["L"] = eval_l(data)

    # ── 判定 + 数据充分性守卫 ──
    # 「0 命中」不等于「通过」：0 格时因为 0==0 而报 PASS 是最危险的自动通过。
    # 这里统一按实际格数与应跑格数决定 verdict，并把原因写进报告。
    for k, v in verdicts.items():
        v["cells_actual"] = (
            verdicts_l_cells(data, k) if k != "L" else v.get("stats", {}).get("样本轮数", 0)
        )
        v["cells_expected"] = expected_cells(data, k) if k != "L" else None
        if k == "L":
            enough = (v.get("stats", {}).get("样本轮数", 0) or 0) > 0
        else:
            enough = v["cells_actual"] >= v["cells_expected"]
        v["data_sufficient"] = enough
        if v.get("manual"):
            v["verdict"] = "待人工裁决" if enough else "未跑（无数据）"
        elif not enough:
            v["verdict"] = (
                "未跑（无数据）" if v["cells_actual"] == 0 else f"数据不足（{v['cells_actual']}/{v['cells_expected']} 格）"
            )
        else:
            v["verdict"] = "PASS" if all(c["pass"] for c in v["checks"]) else "FAIL"
    data["verdicts"] = verdicts
    return verdicts


def verdicts_l_cells(data: Dict[str, Any], key: str) -> int:
    return len(_primary_cells(data, key))


def eval_l(data: Dict[str, Any]) -> Dict[str, Any]:
    """L：全场景汇总的风格基线（与 v3 口径对齐：单气泡字数中位 / 称呼率 / 句尾问号率）。"""
    all_replies: List[Tuple[str, str]] = []   # (场景, 回复)
    for key in L_AGG_SCENARIOS:
        for c in _cells_for(data, key):
            if key == "J":
                s = eval_cell("J", c)["values"]
                for t in (s.get("trigger_sent", ""), s.get("forced_sent_text", "")):
                    if t and t.strip():
                        all_replies.append((key, t))
                continue
            for t in c.get("turns", []):
                if t.get("user", "").startswith("["):
                    continue
                r = t.get("reply", "")
                if r:
                    all_replies.append((key, r))

    n_turn = len(all_replies)
    silent_n = sum(1 for _, r in all_replies if is_silent(r))
    speak = [(k, r) for k, r in all_replies if not is_silent(r)]

    bubble_lens: List[int] = []
    for _, r in speak:
        for b in bubbles(r):
            bubble_lens.append(chars(b))
    reply_lens = [chars(r) for _, r in speak]

    name_n = sum(1 for _, r in speak if NAME_RE.search(r))
    sent_q = sum(1 for _, r in speak if r.rstrip().endswith(("？", "?")))
    bubble_total = len(bubble_lens)
    bubble_q = sum(1 for _, r in speak for b in bubbles(r) if b.rstrip().endswith(("？", "?")))
    tone_n = sum(len(TONE_RE.findall(r)) for _, r in speak)
    tone_chars = sum(chars(r) for _, r in speak) or 1
    emoji_bub = sum(1 for _, r in speak for b in bubbles(r) if EMOJI_RE.search(b))
    excl_bub = sum(1 for _, r in speak for b in bubbles(r) if "！" in b or "!" in b)

    stats = {
        "样本轮数": n_turn,
        "其中沉默": silent_n,
        "沉默率": round(100.0 * silent_n / n_turn, 2) if n_turn else 0.0,
        "称呼率": round(100.0 * name_n / len(speak), 2) if speak else 0.0,
        "称呼命中": sorted({w for _, r in speak for w in NAME_RE.findall(r)}),
        "单气泡字数中位": statistics.median(bubble_lens) if bubble_lens else 0,
        "单气泡字数均值": round(statistics.mean(bubble_lens), 2) if bubble_lens else 0,
        "回复总字数中位": statistics.median(reply_lens) if reply_lens else 0,
        "平均气泡数": round(bubble_total / len(speak), 2) if speak else 0,
        "句尾问号率": round(100.0 * sent_q / len(speak), 2) if speak else 0.0,
        "气泡级问号率": round(100.0 * bubble_q / bubble_total, 2) if bubble_total else 0.0,
        "语气词密度_每百字": round(100.0 * tone_n / tone_chars, 2),
        "emoji或感叹号_气泡率": round(100.0 * (emoji_bub + excl_bub) / bubble_total, 2)
        if bubble_total
        else 0.0,
        "v3基准": V3_BASELINE,
        "纳入场景": L_AGG_SCENARIOS,
        "剔除说明": "K 是 8 轮固定台词的归属测试脚手架，不代表自然风格，故不进 L 汇总。",
    }
    checks = [
        {
            "name": "称呼率 <10%（v3 基准 ≈0%）",
            "actual": f"{stats['称呼率']}%",
            "pass": stats["称呼率"] < L_PASS["称呼率"],
        },
        {
            "name": "单气泡字数中位 10~16 字（v3 基准 13）",
            "actual": f"{stats['单气泡字数中位']} 字",
            "pass": L_PASS["单气泡字数中位"][0]
            <= stats["单气泡字数中位"]
            <= L_PASS["单气泡字数中位"][1],
        },
        {
            "name": "句尾问号率 <10%（v3 基准 5.6%）",
            "actual": f"{stats['句尾问号率']}%",
            "pass": stats["句尾问号率"] < L_PASS["句尾问号率"],
        },
    ]
    return {"stats": stats, "checks": checks}


# ==========================================
# 报告
# ==========================================
def clock_env() -> Dict[str, Any]:
    """环境事实：两个钟下注入给她的作息活动 + 是否命中节假日（用于 G/H 归因）。"""
    persona = Persona.load(CARD_DIR)
    out = {}
    for ck in CLOCK_ORDER:
        dt = CLOCKS[ck]["now"]
        h = is_holiday_date(dt.strftime("%Y-%m-%d"), HOLIDAYS_INJECTED)
        out[ck] = {
            "label": CLOCKS[ck]["label"],
            "now": CLOCKS[ck]["now"].strftime("%Y-%m-%d %H:%M"),
            "weekday": "星期" + "一二三四五六日"[dt.weekday()],
            "desc": CLOCKS[ck]["desc"],
            "is_holiday": h,
            "注入的作息活动": persona.get_current_activity(dt.hour, dt.weekday(), is_holiday=h),
        }
    return out


def build_report_md(data: Dict[str, Any]) -> str:
    L: List[str] = []
    meta = data["meta"]
    L.append("# 青梓综合评测 V4 · 汇总报告（定版大考）")
    L.append("")
    L.append(f"- 生成时间：{meta['generated_at']}")
    L.append(f"- 评测卡：**{CARD_LABEL}**（单卡评测，不做 A/B）")
    L.append(f"- 主聊模型：`{meta['model']}`，thinking={meta['thinking']} effort={meta['effort']}（与生产一致，全部真实 API）")
    L.append(f"- 采样：每场景每钟 {meta['runs']} 次；总调用 {meta['total_llm_calls']} 次，估算费用 **¥{meta['total_cost_cny']}**")
    L.append(f"- 耗时：{meta['elapsed_sec']} 秒；失败格 {meta['failed_cells']}；重试 {meta['retried_cells']}；异常轮次 {meta['error_turns']}")
    L.append("- 干净库铁律：每个 (场景×钟×采样) 全新空库，初始好感度回初始值，跑完即删")
    L.append(f"- 节假日注入（仅内存，不改 config.toml）：`{', '.join(HOLIDAYS_INJECTED)}`（2026 国庆）")
    L.append("")
    L.append("## 一页汇总表")
    L.append("")
    L.append("| 场景 | 名称 | 主判钟 | 指标 | 通过线 | 实测 | 判定 |")
    L.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
    for key, sc in SCENARIOS.items():
        vd = data["verdicts"].get(key, {})
        if vd.get("primary_clock"):
            clk_label = CLOCKS[vd["primary_clock"]]["label"] + "（主判）"
        elif len(sc["clocks"]) == 1:
            clk_label = CLOCKS[sc["clocks"][0]]["label"] + "（单钟）"
        else:
            clk_label = "两钟均跑"
        checks = vd.get("checks", [])
        actual = "；".join(f"{c['name']} → {c['actual']}" for c in checks) or "—"
        L.append(
            f"| {key} | {sc['title']} | {clk_label} | {'；'.join(sc['metrics'])} | "
            f"{sc['pass_line']} | {actual} | **{vd.get('verdict','—')}** |"
        )
    L.append("")
    short = [
        f"{k}（{v['cells_actual']}/{v['cells_expected']} 格）"
        for k, v in data["verdicts"].items()
        if k != "L" and v.get("cells_expected") and not v.get("data_sufficient")
    ]
    if short:
        L.append(
            f"> ⚠ 数据不足的场景（**未判 PASS/FAIL**，按未跑处理）：{'、'.join(short)}。"
            "『0 命中』不等于『通过』，无数据的场景一律不报 PASS。"
        )
        L.append("")
    L.append("## 时钟环境事实（用于 G/H 归因）")
    L.append("")
    L.append("| 时钟 | 伪装时刻 | 星期 | 命中节假日 | 生产注入给她的作息活动 |")
    L.append("| :--- | :--- | :--- | :--- | :--- |")
    for ck in CLOCK_ORDER:
        e = data["environment"][ck]
        L.append(
            f"| {e['label']} | {e['now']} | {e['weekday']} | {'是' if e['is_holiday'] else '否'} | {e['注入的作息活动']} |"
        )
    L.append("")
    L.append("## 风格基线明细（L 场景）")
    L.append("")
    stats = data["verdicts"]["L"]["stats"]
    for k, v in stats.items():
        if k in ("v3基准", "纳入场景", "剔除说明"):
            continue
        L.append(f"- {k}：{v}")
    L.append("")
    L.append("## 逐场景逐次结果（先看事实，再看判定）")
    L.append("")
    for key, sc in SCENARIOS.items():
        if sc["kind"] == "aggregate":
            continue
        L.append(f"### 场景{key} · {sc['title']} — 判定 {data['verdicts'][key]['verdict']}")
        L.append("")
        L.append(f"通过线：{sc['pass_line']}")
        L.append("")
        for c in _cells_for(data, key):
            e = eval_cell(key, c)
            v = e["values"]
            st = c.get("clean_state") or {}
            L.append(
                f"- [{CLOCKS[c['clock']]['label']} 第{c['run']}次] "
                f"库状态 阶段{st.get('stage')}/复合分 {st.get('composite')}/turns={st.get('turns')}"
                f" | 调用 {(c.get('cost') or {}).get('calls',0)} 次 ¥{(c.get('cost') or {}).get('cost_cny',0)}"
                f" | {_one_line(key, v)}"
            )
        for c in data["verdicts"][key].get("checks", []):
            req = f"（要求 {c['required']}）" if c.get("required") else ""
            mark = "PASS" if c["pass"] else ("FAIL" if c["pass"] is False else "待裁决")
            L.append(f"  - {c['name']}{req}：{c['actual']} → {mark}")
        if data["verdicts"][key].get("observability"):
            for kk, vv in data["verdicts"][key]["observability"].items():
                L.append(f"  - {kk}：{vv}")
        if data["verdicts"][key].get("silence_contexts"):
            L.append("  - 沉默格语境（供所有者复核该格是否误伤）：")
            for ctx in data["verdicts"][key]["silence_contexts"]:
                L.append(f"    - {ctx['clock']} 第{ctx['run']}次｜语境：{ctx['语境']}｜该格回复：{ctx['该格回复']}")
        if data["verdicts"][key].get("note"):
            L.append(f"  - 说明：{data['verdicts'][key]['note']}")
        if data["verdicts"][key].get("method"):
            L.append(f"  - 判据方法：{data['verdicts'][key]['method']}")
        if data["verdicts"][key].get("hint_reading"):
            L.append(f"  - 线索分档解读：{data['verdicts'][key]['hint_reading']}")
        L.append("")
    L.append("## FAIL 场景的全部原文")
    L.append("")
    fails = [k for k, v in data["verdicts"].items() if v.get("verdict") == "FAIL"]
    if not fails:
        L.append("本轮无 FAIL 场景。")
    for key in fails:
        sc = SCENARIOS[key]
        L.append(f"### 场景{key} · {sc['title']}（FAIL 原文）")
        L.append("")
        for c in _cells_for(data, key):
            e = eval_cell(key, c)
            v = e["values"]
            L.append(f"#### {CLOCKS[c['clock']]['label']} 第{c['run']}次 — 指标 {json.dumps(v, ensure_ascii=False)}")
            L.append("")
            for t in c.get("turns", []):
                L.append(f"**机主：** {t.get('user','')}")
                L.append("")
                if t.get("raw"):
                    L.append(f"**青梓（模型裸输出）：** {t['raw']}")
                    L.append("")
                if t.get("reply"):
                    L.append(f"**生产实发：** {t['reply']}")
                    L.append("")
            for t in v.get("turns", []) or []:
                L.append(f"（第{t.get('turn')}轮）{t.get('reply')}")
                L.append("")
            if key == "K":
                L.append("**日记原文：**")
                L.append("")
                for row in c.get("diary", []):
                    L.append(f"- {row.get('content')}")
                L.append("")
    L.append("## 判读纪律")
    L.append("")
    L.append("- 客观指标全部由脚本正则判定；风格类（比喻质量、像不像人）只收集原文，不用 LLM 裁判打分。")
    L.append("- 场景 K 按任务书要求以原文并排呈现，最终归属裁决由所有者做，机械线索只用于定位。")
    L.append("- 任何 FAIL 均按 FAIL 呈现，未为凑 PASS 调整场景脚本或指标口径。")
    L.append("- 本轮未改动 companion/ 生产代码、characters/ 角色卡、config.toml 配置。")
    L.append("")
    return "\n".join(L)


def _one_line(key: str, v: Dict[str, Any]) -> str:
    if key in ("A",):
        return (
            f"达标={v['close_ok']} 沉默={v['silent']} 纯表情包={v['pure_sticker']} "
            f"字数={v['chars']} ≤10字={v['short_10']} 问号={v['has_question']} "
            f"叮嘱={v['n_cautions']} 新场景词={v['new_scene_hits'] or '无'}"
        )
    if key == "B":
        return f"叮嘱小句数={v['n_cautions']}（上限{B_CAUTION_MAX}）{v['cautions']}"
    if key == "C":
        return f"两段式={v['two_stage']} 只评图气泡={v['pure_visual_bubbles']} 接话气泡={v['taste_bubbles']}"
    if key == "D":
        return (
            f"达标={v['close_ok']} 图内元素词={v['img_words'] or '无'} 问号={v['has_question']} "
            f"叮嘱={v['n_cautions']} 新场景词={v['new_scene_hits'] or '无'} "
            f"｜记录：沉默={v['silent']} 字数={v['chars']}"
        )
    if key == "E":
        return f"③轮梗词={v['meme_words'] or '无'}"
    if key == "F":
        return f"②轮沉默={v['silent']} 短收={v['short']} 字数={v['chars']}"
    if key == "G":
        return f"校园词={v['campus'] or '无'} 老家词={v['home'] or '无'} 矛盾={v['contradiction']}"
    if key == "H":
        return f"长假词={v['longholiday_words'] or '无'} 假期氛围词={v['holiday_vibe_words'] or '无'} 字数={v['chars']}"
    if key == "I":
        return f"邀约命中={len(v['invite_hits'])} 处 {[h['word'] for h in v['invite_hits']] or '无'} 噪声疑似={len(v['noise_suspects'])}"
    if key == "J":
        return (
            f"决策选A={v['choice_a']} 裸输出占位符={v['forced_raw_placeholders'] or '无'} "
            f"实发占位符={v['forced_sent_placeholders'] or '无'}"
        )
    if key == "K":
        hints = "；".join(f"{f['label']}→{f['mechanical_hint']}" for f in v["facts"])
        return f"日记{v['diary_rows']}条 捞到自述事实={v['self_fact_found']} 颠倒嫌疑={v['inversion_suspects']} | {hints}"
    return ""


def build_transcripts_md(data: Dict[str, Any]) -> str:
    L: List[str] = []
    L.append("# 青梓综合评测 V4 · 全部 transcript 留档")
    L.append("")
    L.append(f"生成时间：{data['meta']['generated_at']}｜卡：{CARD_LABEL}｜采样：每场景每钟 {data['meta']['runs']} 次")
    L.append("")
    for ck in CLOCK_ORDER:
        e = data["environment"][ck]
        L.append(f"- **{e['label']}**：伪装 {e['now']}（{e['weekday']}），节假日={'是' if e['is_holiday'] else '否'}，注入作息：{e['注入的作息活动']}")
    L.append("")
    L.append("说明：`裸输出` = 模型完整输出；`生产实发` = 经 companion/replier.py 全管道（沉默权→换行还原→标签剥离→旁白剥离→表情包拆分→图片占位符滤网→切段→表情包硬上限→max_chunks）后真正发到 QQ 上的内容。")
    L.append("")
    for key, sc in SCENARIOS.items():
        if sc["kind"] == "aggregate":
            continue
        L.append("---")
        L.append("")
        L.append(f"## 场景{key} · {sc['title']}")
        L.append("")
        if sc["kind"] == "chat":
            L.append("机主脚本：")
            for i, m in enumerate(sc["turns"], start=1):
                L.append(f"{i}. {m}")
            L.append("")
        elif sc["kind"] == "proactive":
            L.append(f"预置欲言又止念头：`{sc['desire']}`（种子历史：{sc['seed_user']} / {sc['seed_bot']}）")
            L.append("")
        elif sc["kind"] == "diary":
            L.append("机主脚本：")
            for i, m in enumerate(sc["turns"], start=1):
                L.append(f"{i}. {m}")
            L.append("")
        for c in _cells_for(data, key):
            L.append(f"### {CLOCKS[c['clock']]['label']} · 第{c['run']}次")
            L.append("")
            st = c.get("clean_state") or {}
            L.append(
                f"库状态：turns={st.get('turns')} 阶段={st.get('stage')} 复合分={st.get('composite')}"
                f"｜调用 {(c.get('cost') or {}).get('calls',0)} 次 ¥{(c.get('cost') or {}).get('cost_cny',0)}"
            )
            L.append("")
            for t in c.get("turns", []):
                L.append(f"**机主：** {t.get('user','')}")
                L.append("")
                if t.get("raw"):
                    L.append("> 裸输出：" + t["raw"].replace("\n", "\n> "))
                    L.append("")
                if t.get("reply"):
                    L.append("> 生产实发：" + t["reply"].replace("\n", "\n> "))
                    L.append("")
            if key == "J":
                L.append("**主动消息决策层/生成层留证：**")
                L.append("")
                for a in c.get("attempts", []):
                    gate = f"闸门拦截（{a.get('gate_reason')}）" if a.get("gate_blocked") else "闸门通过"
                    L.append(f"- 第{a.get('attempt')}次 {gate} choice={a.get('choice')}")
                    if a.get("decision_raw"):
                        L.append("  - 决策层原文：" + str(a["decision_raw"])[:600])
                    if a.get("generation_raw"):
                        L.append("  - 生成层原文：" + str(a["generation_raw"])[:600])
                    L.append(f"  - 实发：{a.get('sent_text') or '（未发送）'}")
                    L.append(f"  - 裸输出占位符：{a.get('raw_placeholders') or '无'}｜实发占位符：{a.get('sent_placeholders') or '无'}")
                f = c.get("forced") or {}
                L.append("")
                L.append("**绕过决策闸门的强制最坏情况直调：**")
                L.append("")
                L.append(f"- 说明：{f.get('note','')}")
                L.append(f"- 生成层裸输出：{f.get('generation_raw','')}")
                L.append(f"- 裸输出占位符：{f.get('raw_placeholders') or '无'}")
                L.append(f"- 滤网后实发：{f.get('sent_text') or '（空）'}")
                L.append(f"- 落库记录：{f.get('record') or '（空）'}")
                L.append(f"- 实发占位符：{f.get('sent_placeholders') or '无'}")
                L.append("")
            if key == "K":
                L.append("**事实归属并排（对话原文 vs 日记原文）：**")
                L.append("")
                for f in eval_cell("K", c)["values"]["facts"]:
                    L.append(f"- {f['label']}（应属：{f['expected_owner']}）")
                    L.append(f"  - 出处：第{f['source_turn']}次轮 — {f['source_text']}")
                    L.append(f"  - 日记命中句：{f['diary_sentences'] or '（日记未提及）'}")
                    L.append(f"  - 机械线索：{f['mechanical_hint']} — {f['hint_reason']}")
                L.append("")
                L.append("**日记表新行原文：**")
                L.append("")
                for row in c.get("diary", []):
                    L.append(f"- id={row.get('id')} importance={row.get('importance')} sentiment={row.get('sentiment')}")
                    L.append(f"  - {row.get('content')}")
                L.append("")
    return "\n".join(L)


# ==========================================
# 入口
# ==========================================
def arg_value(name: str, default: str) -> str:
    for i, a in enumerate(sys.argv):
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
        if a == name and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def _finalize(data: Dict[str, Any]) -> None:
    summarize(data)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(RAW_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write(build_report_md(data))
    with open(TRANSCRIPTS_MD, "w", encoding="utf-8") as f:
        f.write(build_transcripts_md(data))


async def main() -> None:
    os.chdir(ROOT)
    runs = int(arg_value("--runs", "5"))
    concurrency = int(arg_value("--concurrency", "4"))
    sel = arg_value("--scenarios", "all")
    smoke = "--smoke" in sys.argv
    from_raw = "--from-raw" in sys.argv

    if from_raw:
        with open(RAW_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        _finalize(data)
        print(f"[复算] 已用 {RAW_JSON} 的原始输出零成本重算指标与判定")
        for k, v in data["verdicts"].items():
            print(f"  {k} {SCENARIOS[k]['title'][:16]:<18} {v['verdict']}")
        print(f"\n报告: {REPORT_MD}\n留档: {TRANSCRIPTS_MD}")
        return

    if smoke:
        runs, sel = 1, "A"
        print("[冒烟] 1 场景 × 1 采样 × 1 时钟（先验 harness，再全量）", flush=True)

    keys = (
        [k.strip() for k in sel.split(",") if k.strip() in SCENARIOS and SCENARIOS[k.strip()]["kind"] != "aggregate"]
        if sel != "all"
        else [k for k, s in SCENARIOS.items() if s["kind"] != "aggregate"]
    )
    if not keys:
        print("没有可执行的场景")
        return

    patch_time()
    started = datetime.now()
    results: Dict[Tuple[str, str, int], Dict[str, Any]] = {}
    log: List[str] = []
    try:
        for ck in CLOCK_ORDER:
            jobs_keys = [k for k in keys if ck in SCENARIOS[k]["clocks"]]
            if not jobs_keys:
                continue
            set_clock(ck)
            sem = asyncio.Semaphore(concurrency)
            print(
                f"[跑分] 时钟={CLOCKS[ck]['label']} {CLOCKS[ck]['now']}｜场景 {jobs_keys}｜每格 {runs} 次",
                flush=True,
            )
            jobs = []
            for key in jobs_keys:
                for run in range(1, runs + 1):
                    jobs.append(run_cell(key, ck, run, sem, results, log))
            await asyncio.gather(*jobs)
    finally:
        restore_time()

    cells = {f"{k}|{c}|{r}": v for (k, c, r), v in results.items()}
    total_calls = sum((c.get("cost") or {}).get("calls", 0) for c in cells.values())
    total_cost = round(sum((c.get("cost") or {}).get("cost_cny", 0.0) for c in cells.values()), 4)
    failed = sum(1 for c in cells.values() if c.get("error"))
    retried = sum(1 for c in cells.values() if (c.get("attempt_no") or 1) > 1)
    err_turns = sum(
        1
        for c in cells.values()
        for t in c.get("turns", [])
        if str(t.get("reply", "")).startswith("[失败]") or t.get("user") == "[ERROR]"
    )
    dirty = [
        f"{k}"
        for k, c in cells.items()
        if (c.get("clean_state") or {}).get("clean") is False
    ]

    data: Dict[str, Any] = {
        "meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "card": CARD_DIR,
            "card_label": CARD_LABEL,
            "model": "deepseek-v4-pro",
            "thinking": "enabled (thinking_chat=true)",
            "effort": "low (thinking_effort_chat=low)",
            "runs": runs,
            "scenarios_run": keys,
            "clocks": {ck: CLOCKS[ck]["now"].strftime("%Y-%m-%d %H:%M") for ck in CLOCK_ORDER},
            "holidays_injected_in_memory": HOLIDAYS_INJECTED,
            "holidays_note": (
                "本地 config.toml 的 [llm.pricing].holidays 为空；长假钟所需的 2026 国庆日期"
                "只在进程内注入内存对象，未落盘改动 config.toml（红线文件）。"
                "服务器侧需按 DEPLOY.md 手工填同一份日期。"
            ),
            "clean_db_policy": "每个 (场景×钟×采样) 全新空库，初始阶段 1，跑完即删 -wal/-shm",
            "clean_db_violations": dirty,
            "elapsed_sec": int((datetime.now() - started).total_seconds()),
            "total_llm_calls": total_calls,
            "total_cost_cny": total_cost,
            "cost_note": (
                "费用取自各沙箱库 llm_calls.cost_estimate 汇总。计价按调用发生时的真实系统时间判峰谷"
                "（本次运行时段为非高峰），而沙箱内伪装钟是 15:00（生产高峰），故此数为保守下限。"
            ),
            "failed_cells": failed,
            "retried_cells": retried,
            "error_turns": err_turns,
            "produced_by": "scripts/benchmark_v4.py",
        },
        "environment": clock_env(),
        "scenarios": SCENARIOS,
        "cells": cells,
    }
    _finalize(data)

    print("\n".join(log))
    print(f"\n完成：{len(cells)} 格，失败 {failed}，重试 {retried}，异常轮 {err_turns}")
    if dirty:
        print(f"⚠ 干净库铁律违例：{dirty}")
    print(f"总调用 {total_calls} 次，估算费用 ¥{total_cost}")
    print("\n=== 判定 ===")
    for k in list(SCENARIOS.keys()):
        if k in data["verdicts"]:
            print(f"  {k} {SCENARIOS[k]['title'][:20]:<22} {data['verdicts'][k]['verdict']}")
    print(f"\n报告: {REPORT_MD}\n留档: {TRANSCRIPTS_MD}\n原始: {RAW_JSON}")


if __name__ == "__main__":
    asyncio.run(main())
