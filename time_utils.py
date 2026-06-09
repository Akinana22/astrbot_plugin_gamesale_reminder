"""
时区工具模块
支持两种配置方式：
  - 偏移量：如 "+8"、"-5"（必须以 +/- 开头，后跟整数）
  - 标准时区名：如 "Asia/Shanghai"、"America/New_York"
包含日期解析、Cron表达式、定时任务调度等。
"""

import asyncio
import re
from datetime import datetime, date, timedelta, timezone
from typing import Optional, Union, Set, Dict, Callable, Tuple

from astrbot.api import logger
from dateutil import parser as date_parser

# ==================== 全局时区配置 ====================
_TZ_MODE: Optional[str] = None  # 'offset' 或 'zone'
_TZ_VALUE: Optional[Union[int, str]] = None  # 偏移小时数或时区名


def parse_timezone_config(config: str) -> Tuple[str, Union[int, str]]:
    """
    解析时区配置字符串
    :param config: 用户配置，如 "+8" 或 "Asia/Shanghai"
    :return: (mode, value)
        mode: 'offset' 或 'zone'
        value: offset 模式下为 int（偏移小时数），zone 模式下为 str（时区名）
    """
    config = config.strip()
    match = re.match(r"^([+-])(\d+)$", config)
    if match:
        sign = match.group(1)
        hours = int(match.group(2))
        if sign == "-":
            hours = -hours
        return ("offset", hours)
    return ("zone", config)


def init_timezone_config(config: str) -> Tuple[str, Union[int, str]]:
    """
    初始化全局时区配置，并尝试验证有效性
    :param config: 用户配置字符串
    :return: (mode, value) 同 parse_timezone_config
    """
    global _TZ_MODE, _TZ_VALUE
    try:
        mode, value = parse_timezone_config(config)
        if mode == "zone":
            # 验证时区名是否有效
            try:
                import zoneinfo

                zoneinfo.ZoneInfo(value)
            except Exception as e:
                logger.warning(f"无效的时区名 '{value}'，回退到默认偏移 +8: {e}")
                mode, value = ("offset", 8)
        _TZ_MODE, _TZ_VALUE = mode, value
        logger.info(f"时区配置已加载: mode={mode}, value={value}")
        return mode, value
    except Exception as e:
        logger.error(f"解析时区配置失败: {e}，使用默认 +8")
        _TZ_MODE, _TZ_VALUE = ("offset", 8)
        return _TZ_MODE, _TZ_VALUE


def get_config_tz() -> Tuple[str, Union[int, str]]:
    """获取当前时区配置（模式，值）"""
    if _TZ_MODE is None or _TZ_VALUE is None:
        return ("offset", 8)
    return _TZ_MODE, _TZ_VALUE


def now_in_config_tz() -> datetime:
    """
    获取当前时间（转换为配置时区/偏移后的本地时间）
    返回 naive datetime 对象（无时区信息）
    """
    utc_now = datetime.now(timezone.utc)
    mode, value = get_config_tz()
    if mode == "offset":
        local_dt = utc_now + timedelta(hours=value)
    else:
        import zoneinfo

        tz = zoneinfo.ZoneInfo(value)
        local_dt = utc_now.astimezone(tz)
    return local_dt.replace(tzinfo=None)


def utc_now() -> datetime:
    """获取当前 UTC 时间（naive datetime）"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_now_str() -> str:
    """获取当前 UTC 时间的 ISO 字符串（不带时区）"""
    return utc_now().isoformat()


def convert_to_utc(dt: Union[datetime, str]) -> datetime:
    """
    将任意 datetime 或 ISO 字符串转换为 UTC 时间（naive）
    如果输入是 naive datetime，假设其为本地时间（配置时区）
    """
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        # naive，假设为配置时区的本地时间
        mode, value = get_config_tz()
        if mode == "offset":
            # 减去偏移得到 UTC
            return dt - timedelta(hours=value)
        else:
            import zoneinfo

            tz = zoneinfo.ZoneInfo(value)
            aware = tz.localize(dt)
            utc_aware = aware.astimezone(timezone.utc)
            return utc_aware.replace(tzinfo=None)
    else:
        utc_aware = dt.astimezone(timezone.utc)
        return utc_aware.replace(tzinfo=None)


def convert_to_utc_str(dt: Union[datetime, str]) -> str:
    """将任意时间对象或字符串转换为 UTC 字符串（ISO 8601，不带时区）"""
    utc_dt = convert_to_utc(dt)
    return utc_dt.isoformat()


def convert_to_config_tz(dt: Union[datetime, str]) -> datetime:
    """
    将任意 datetime 或 ISO 字符串转换为配置时区/偏移的本地时间（naive）
    """
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        # naive，假设为 UTC
        utc_aware = dt.replace(tzinfo=timezone.utc)
    else:
        utc_aware = dt
    mode, value = get_config_tz()
    if mode == "offset":
        local_dt = utc_aware.astimezone(timezone.utc).replace(tzinfo=None) + timedelta(
            hours=value
        )
    else:
        import zoneinfo

        tz = zoneinfo.ZoneInfo(value)
        local_dt = utc_aware.astimezone(tz).replace(tzinfo=None)
    return local_dt


# ==================== 日期解析工具 ====================
class DateParser:
    """
    日期/时间解析工具类
    - parse(): 返回 date 或 datetime 对象（如果包含时间信息则返回 datetime，否则返回 date）
    - parse_date(): 始终返回 date 对象（如果输入包含时间，则自动截取日期部分）
    """

    @staticmethod
    def parse(date_str: str) -> Union[date, datetime, None]:
        """
        解析日期时间字符串，根据是否包含时间信息返回 date 或 datetime

        支持格式示例：
        - 仅日期: "2025-01-01", "2025年1月1日", "Jan 1, 2025"
        - 日期+时间: "2025-01-01 15:30:00", "2025年1月1日 15:30", "2025-01-01T15:30:00"
        - 包含时区: 忽略时区，返回本地时间

        返回：
            - 若输入包含时间信息 -> datetime 对象（naive）
            - 若输入仅日期 -> date 对象
            - 解析失败 -> None
        """
        if not date_str:
            return None

        original = date_str.strip()
        logger.debug(f"尝试解析日期时间: {original}")

        try:
            dt = date_parser.parse(original)
            has_time = any(
                [
                    ":" in original,
                    re.search(r"[\d一二三四五六七八九十]+[\s]*时", original),
                    re.search(r"[\d]+[\s]*分", original),
                    re.search(r"[\d]+[\s]*秒", original),
                    "T" in original,
                ]
            )
            if has_time or (dt.hour != 0 or dt.minute != 0 or dt.second != 0):
                return dt
            else:
                return dt.date()
        except Exception:
            pass

        return DateParser._parse_date_only(original)

    @staticmethod
    def _parse_date_only(date_str: str) -> Optional[date]:
        """纯日期解析（不含时间）"""
        # 中文格式
        match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", date_str)
        if match:
            y, m, d = int(match[1]), int(match[2]), int(match[3])
            try:
                return date(y, m, d)
            except ValueError:
                pass

        month_map = {
            "jan": 1,
            "feb": 2,
            "mar": 3,
            "apr": 4,
            "may": 5,
            "jun": 6,
            "jul": 7,
            "aug": 8,
            "sep": 9,
            "oct": 10,
            "nov": 11,
            "dec": 12,
        }
        # YYYY-MM-DD
        match = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", date_str)
        if match:
            y, m, d = int(match[1]), int(match[2]), int(match[3])
            try:
                return date(y, m, d)
            except ValueError:
                pass
        # YYYY/MM/DD
        match = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", date_str)
        if match:
            y, m, d = int(match[1]), int(match[2]), int(match[3])
            try:
                return date(y, m, d)
            except ValueError:
                pass
        # DD MMM YYYY
        match = re.match(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", date_str)
        if match:
            d, month_str, y = int(match[1]), match[2].lower()[:3], int(match[3])
            if month_str in month_map:
                try:
                    return date(y, month_map[month_str], d)
                except ValueError:
                    pass
        # MMM DD, YYYY
        match = re.match(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", date_str)
        if match:
            month_str, d, y = match[1].lower()[:3], int(match[2]), int(match[3])
            if month_str in month_map:
                try:
                    return date(y, month_map[month_str], d)
                except ValueError:
                    pass

        logger.warning(f"无法解析日期: {date_str}")
        return None

    @staticmethod
    def parse_date(date_str: str) -> Optional[date]:
        """
        始终返回 date 对象（如果输入包含时间，则自动截取日期部分）
        """
        result = DateParser.parse(date_str)
        if isinstance(result, datetime):
            return result.date()
        return result


# ==================== Cron 表达式 ====================
class CronExpression:
    """
    简化版 Cron 表达式，格式：分 时 日 月 周
    字段含义：
        - 分：0-59
        - 时：0-23
        - 日：1-31
        - 月：1-12
        - 周：0-6（0=周日，1=周一，...，6=周六）
    支持：
        - * 任意值
        - 枚举：1,2,3
        - 范围：1-5
        - 步长：*/5（仅分、时支持，其他字段未测试）
    表达式可省略后面的字段，例如：
        "1 2"       → 每天2点1分
        "30 9 * * 1" → 每周一9点30分
    """

    def __init__(self, expr: str):
        self.fields = ["minute", "hour", "day", "month", "weekday"]
        parts = expr.strip().split()
        while len(parts) < 5:
            parts.append("*")
        self.minute = self._parse_field(parts[0], 0, 59)
        self.hour = self._parse_field(parts[1], 0, 23)
        self.day = self._parse_field(parts[2], 1, 31)
        self.month = self._parse_field(parts[3], 1, 12)
        self.weekday = self._parse_field(parts[4], 0, 6)

    def _parse_field(self, field: str, min_val: int, max_val: int) -> Set[int]:
        if field == "*":
            return set(range(min_val, max_val + 1))
        values = set()
        for part in field.split(","):
            if "-" in part:
                start, end = map(int, part.split("-"))
                values.update(range(start, end + 1))
            elif "/" in part:
                base, step = part.split("/")
                if base == "*":
                    start = min_val
                else:
                    start = int(base)
                step = int(step)
                for v in range(start, max_val + 1, step):
                    if v <= max_val:
                        values.add(v)
            else:
                values.add(int(part))
        return values

    def matches(self, dt: datetime) -> bool:
        # 转换 weekday：Python 的 weekday() 返回 0=周一，6=周日
        # 我们约定 0=周日，1=周一，...，6=周六
        wd = (dt.weekday() + 1) % 7
        return (
            dt.minute in self.minute
            and dt.hour in self.hour
            and dt.day in self.day
            and dt.month in self.month
            and wd in self.weekday
        )


# ==================== 定时任务调度器 ====================
class TaskScheduler:
    """定时任务调度器，管理基于 Cron 表达式的异步任务。"""

    def __init__(self, tz_mode: str, tz_value: Union[int, str]):
        """
        :param tz_mode: 'offset' 或 'zone'
        :param tz_value: 偏移小时数（int）或时区名（str）
        """
        self.tz_mode = tz_mode
        self.tz_value = tz_value
        self.tasks: Dict[str, Dict] = {}
        self._lock = asyncio.Lock()
        self._running = False
        self._task = None

    async def add_task(
        self, task_id: str, expression: str, callback: Callable, *args, **kwargs
    ):
        """
        添加定时任务
        :param task_id: 唯一标识（覆盖已存在的同名任务）
        :param expression: Cron 表达式，如 "0 9" 表示每天9:00
        :param callback: 异步回调函数
        :param args, kwargs: 传递给回调函数的参数
        """
        cron = CronExpression(expression)
        async with self._lock:
            self.tasks[task_id] = {
                "cron": cron,
                "callback": callback,
                "args": args,
                "kwargs": kwargs,
                "last_trigger": None,
            }
        logger.info(f"定时任务已添加: {task_id} -> {expression}")

    async def remove_task(self, task_id: str):
        async with self._lock:
            if task_id in self.tasks:
                del self.tasks[task_id]
                logger.info(f"定时任务已删除: {task_id}")

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("定时任务调度器已启动")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("定时任务调度器已停止")

    def _now_in_config_tz(self) -> datetime:
        """获取当前配置时区时间（naive）"""
        if self.tz_mode == "offset":
            return datetime.utcnow() + timedelta(hours=self.tz_value)
        else:
            import zoneinfo

            tz = zoneinfo.ZoneInfo(self.tz_value)
            return datetime.now(tz).replace(tzinfo=None)

    async def _run(self):
        """主循环：每分钟检查一次任务触发条件，捕获所有异常防止调度器退出"""
        while self._running:
            try:
                # 获取当前配置时区时间，若失败则使用 UTC 时间作为后备
                try:
                    now = self._now_in_config_tz()
                except Exception as e:
                    logger.error(
                        f"[TaskScheduler] 获取配置时区时间失败: {e}，使用 UTC 时间作为后备"
                    )
                    now = datetime.now(timezone.utc).replace(tzinfo=None)
                logger.info(
                    f"[TaskScheduler] 当前时间: {now}, 任务数: {len(self.tasks)}"
                )
                async with self._lock:
                    for task_id, task in self.tasks.items():
                        cron = task["cron"]
                        last = task.get("last_trigger")
                        # 修复：比较到分钟级别（含日期），避免跨日误判
                        if last and last.replace(
                            second=0, microsecond=0
                        ) == now.replace(second=0, microsecond=0):
                            continue
                        if cron.matches(now):
                            logger.info(
                                f"[TaskScheduler] 触发定时任务: {task_id} at {now}"
                            )
                            task["last_trigger"] = now
                            asyncio.create_task(
                                task["callback"](*task["args"], **task["kwargs"])
                            )
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                logger.info("[TaskScheduler] 调度器循环被取消")
                break
            except Exception as e:
                logger.error(
                    f"[TaskScheduler] 调度器循环发生未捕获异常: {e}", exc_info=True
                )
                await asyncio.sleep(60)

