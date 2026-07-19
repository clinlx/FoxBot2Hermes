"""极简 5 字段 cron 表达式解析与匹配(纯 stdlib,无第三方依赖)。

字段: 分 时 日 月 周(与标准 crontab 一致),支持:
  * 、数字、a-b 范围、a,b,c 列表、*/n 与 a-b/n 步进;周字段 0 和 7 都是周日。
不支持名字写法(mon/jan)与 L/W/# 等扩展,遇到即 ValueError。
日与周同时受限时按标准 cron 语义取"或"。
"""

from __future__ import annotations

from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 兜底(理论不会走到)
    ZoneInfo = None  # type: ignore

# (标签, 下限, 上限);周允许 0-7,解析后 7 归并为 0(周日)
_FIELDS = (("分", 0, 59), ("时", 0, 23), ("日", 1, 31), ("月", 1, 12), ("周", 0, 7))


def _parse_field(text: str, label: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"{label}字段存在空项")
        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) < 1:
                raise ValueError(f"{label}字段步进非法: /{step_s}")
            step = int(step_s)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise ValueError(f"{label}字段范围非法: {part!r}")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
        else:
            raise ValueError(f"{label}字段无法解析: {part!r}")
        if not (lo <= start <= hi and lo <= end <= hi and start <= end):
            raise ValueError(f"{label}字段越界({lo}-{hi}): {part!r}")
        values.update(range(start, end + 1, step))
    return values


class CronSpec:
    """已解析的 cron 表达式;matches(dt) 判定某时刻(精确到分)是否命中。"""

    def __init__(self, expr: str) -> None:
        fields = expr.split()
        if len(fields) != 5:
            raise ValueError(f"须为 5 个字段(分 时 日 月 周),实得 {len(fields)} 个")
        self.expr = expr
        self.minutes, self.hours, self.days, self.months, self.weekdays = (
            _parse_field(f, label, lo, hi)
            for f, (label, lo, hi) in zip(fields, _FIELDS)
        )
        if 7 in self.weekdays:  # 7 也是周日
            self.weekdays.add(0)
        # 标准 cron: 日/周都受限时取"或";记录哪边是 * 以区分
        self.dom_star = fields[2] == "*"
        self.dow_star = fields[4] == "*"

    def matches(self, dt: datetime) -> bool:
        if (dt.minute not in self.minutes or dt.hour not in self.hours
                or dt.month not in self.months):
            return False
        dom_ok = dt.day in self.days
        dow_ok = ((dt.weekday() + 1) % 7) in self.weekdays  # cron 周制: 周日=0
        if self.dom_star and self.dow_star:
            return True
        if self.dom_star:
            return dow_ok
        if self.dow_star:
            return dom_ok
        return dom_ok or dow_ok


def parse_cron(expr: str) -> CronSpec:
    """解析 cron 表达式;非法/空串时抛 ValueError(含中文原因)。"""
    if not isinstance(expr, str) or not expr.strip():
        raise ValueError("表达式为空")
    return CronSpec(expr.strip())


def local_now(tz_name: str) -> datetime:
    """按 IANA 时区名取当前时间;时区非法/不可用时回退系统本地时间。"""
    if tz_name and ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(tz_name))
        except Exception:
            pass
    return datetime.now().astimezone()
