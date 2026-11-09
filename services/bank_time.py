import re
from datetime import datetime, timedelta


CHINESE_NUMBERS = {digit: number for number, digit in enumerate("零一二三四五六七八九")}
CHINESE_NUMBERS.update({"〇": 0, "两": 2})
NUMBER = r"(?:\d{1,2}|[零〇一二两三四五六七八九十]{1,3})"
DATE = re.compile(rf"(?:(?P<month>{NUMBER})月|(?P<relative_month>这个月|本月|下个月|下月))?(?P<day>{NUMBER})[日号]")
WEEKDAY = re.compile(r"(?P<week>下下|下|这|本|上)?(?:个)?(?:周|星期)(?P<day>[一二三四五六日天末1234567])")
WEEK_RANGE = re.compile(r"(?P<week>下下|下|这|本|上)(?:个)?(?:周|星期)")
WEEKDAY_RANGE = re.compile(r"(?P<week>下下|下|这|本|上)?(?:个)?周一(?:到|至)(?:周)?五")
RELATIVE = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3, "昨天": -1}
CLOCK = re.compile(rf"({NUMBER})(?:[:：](\d{{2}})|点(?:钟|整|半|{NUMBER}分?)?)")


def has_calendar_day(text):
    return bool(DATE.search(text) or WEEKDAY.search(text) or WEEK_RANGE.search(text)
                or any(word in text for word in (*RELATIVE, "工作日", "平日", "周末")))


def number(text):
    if text.isdecimal():
        return int(text)
    if "十" in text:
        tens, ones = text.split("十", 1)
        if len(tens) > 1 or len(ones) > 1 or "十" in ones:
            return -1
        return CHINESE_NUMBERS.get(tens, 1) * 10 + CHINESE_NUMBERS.get(ones, 0)
    return int("".join(str(CHINESE_NUMBERS[part]) for part in text))


def slot_matches(text, slots, today):
    """Filter real inventory using the calendar words the caller actually said."""
    text = WEEKDAY_RANGE.sub(lambda match: f'{match["week"]}周工作日' if match["week"] else "工作日", text)
    workdays = any(word in text for word in ("工作日", "平日"))
    dates = list(DATE.finditer(text))
    weeks = list(WEEKDAY.finditer(text))
    week_range = WEEK_RANGE.search(text) if not weeks else None
    relative = re.findall(r"大后天|今天|明天|后天|昨天", text)
    if len(dates) + len(weeks) + len(relative) > 1:
        return None
    date_match = dates[0] if dates else None
    week = weeks[0] if weeks else None
    clock = CLOCK.search(text)
    period = next((word for word in ("上午", "早上", "中午", "下午", "晚上") if word in text), None)
    if not (date_match or week or week_range or relative or clock or period or workdays):
        return None

    days = None
    if relative:
        days = {today + timedelta(days=RELATIVE[relative[0]])}
    elif week or week_range:
        offset = {"下": 1, "下下": 2, "上": -1}.get((week or week_range)["week"], 0)
        monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        if not week:
            days = {monday + timedelta(days=day) for day in range(7)}
        elif week["day"] == "末":
            days = {monday + timedelta(days=day) for day in (5, 6)}
        else:
            weekday = 7 if week["day"] in "日天" else number(week["day"])
            day = monday + timedelta(days=weekday - 1)
            if (week["week"] is None or week[0].startswith("这个")) and day < today:
                day += timedelta(weeks=1)
            days = {day}

    day = number(date_match["day"]) if date_match else None
    month = number(date_match["month"]) if date_match and date_match["month"] else None
    relative_month = date_match["relative_month"] if date_match else None
    month_start = today.replace(day=1)
    if relative_month in {"下个月", "下月"}:
        month_start = (month_start + timedelta(days=32)).replace(day=1)

    hour = number(clock[1]) if clock else None
    minute = None
    hours = set()
    if clock:
        suffix = clock[0].split("点", 1)[-1]
        minute_text = re.search(NUMBER, suffix) if "点" in clock[0] else None
        minute = int(clock[2]) if clock[2] else 30 if suffix == "半" else number(minute_text[0]) if minute_text else 0
        if period in {"中午", "下午", "晚上"} and 1 <= hour < 12:
            hour += 12
        hours.add(hour)
        if not period and not clock[2] and 1 <= hour < 12:
            hours.add(hour + 12)

    matching = []
    for slot in slots:
        start = datetime.fromisoformat(slot["start"])
        if workdays and start.weekday() >= 5:
            continue
        if days is not None and start.date() not in days:
            continue
        if day is not None and (start.day != day or month is not None and start.month != month):
            continue
        if relative_month and (start.year, start.month) != (month_start.year, month_start.month):
            continue
        if period in {"上午", "早上"} and start.hour >= 12 or period in {"中午", "下午", "晚上"} and start.hour < 12:
            continue
        if clock and (start.hour not in hours or start.minute != minute):
            continue
        matching.append(slot["id"])
    return tuple(matching)
