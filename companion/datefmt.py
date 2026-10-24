"""相对日期 (datefmt.py)：给她看的文字里，把具体日期换成"今天/前天/3 天前/后天"。

游戏世界（如科幻的塔卫二用塔罗斯历）里不会有人说"10 月 22 号"。系统内部照旧按真实日期
算间隔、到点、过期，只在提示词组装完的最后一步换说法。星期和几点几分保留。
开关：角色卡 world.date_style = "relative"（默认 "absolute" = 不转换）。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

# 2026-10-22 / 10-22 / 10-22号 / 2026年10月22日 / 10月22日。横杠写法只认两位数月日（系统自己
# 写日期都是 %m-%d），免得把"比分 3-1""第 2-3 题"当成日期；前后不能紧挨冒号、点、数字、横杠，
# 免得把"12:10-22:00""1.05-22 版本"里的片段当成日期。中文写法带年份时把年份一起吞掉
_DATE_RE = re.compile(
    r"(?<![\d:.\-])(?:(?P<y>\d{4})-)?(?P<m>\d{2})-(?P<d>\d{2})[日号]?"
    r"(?![\d:\-]|\.\d)"
    r"|(?<!\d)(?:(?P<y2>\d{4})年)?(?P<m2>\d{1,2})月(?P<d2>\d{1,2})[日号](?!\d)"
)

# "编号 10-22""单号 2026-10-19"这类是流水号/订单号，不是日期
_CODE_PREFIX = ("编号", "序号", "单号", "工号", "型号", "编码", "订单")


def relative_day(target: date, today: date) -> str:
    n = (target - today).days
    named = {0: "今天", -1: "昨天", -2: "前天", 1: "明天", 2: "后天"}
    if n in named:
        return named[n]
    if -13 <= n <= -3:
        return f"{-n} 天前"
    if 3 <= n <= 13:
        return f"{n} 天后"
    weeks = round(abs(n) / 7)
    if abs(n) < 60:
        return f"大约 {weeks} 周{'前' if n < 0 else '后'}"
    months = round(abs(n) / 30)
    return f"大约 {months} 个月{'前' if n < 0 else '后'}"


def _resolve(y: Optional[str], m: int, d: int, today: date) -> Optional[date]:
    try:
        if y:
            return date(int(y), m, d)
        cand = date(today.year, m, d)
        # 没写年份：离今天超过半年的按跨年算（12 月说"01-03"是明年，1 月说"12-28"是去年）
        # 跨到非闰年时 02-29 会踩 replace 的 ValueError，认不出就原样留着
        if (cand - today).days > 183:
            cand = cand.replace(year=cand.year - 1)
        elif (today - cand).days > 183:
            cand = cand.replace(year=cand.year + 1)
    except ValueError:
        return None
    return cand


def relativize_dates(text: str, now: datetime) -> str:
    """把文字里的具体日期换成相对说法；认不出的（比如 13-40 这种）原样留着。"""
    if not text:
        return text
    today = now.date()

    def _sub(m: "re.Match[str]") -> str:
        if text[:m.start()].rstrip().endswith(_CODE_PREFIX):
            return m.group(0)
        try:
            if m.group("m") is not None:
                target = _resolve(m.group("y"), int(m.group("m")), int(m.group("d")), today)
            else:
                target = _resolve(m.group("y2"), int(m.group("m2")), int(m.group("d2")), today)
        except Exception:
            return m.group(0)
        return relative_day(target, today) if target else m.group(0)

    return _DATE_RE.sub(_sub, text)
