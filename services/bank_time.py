import re
from calendar import monthrange
from datetime import date, datetime, timedelta


CHINESE_NUMBERS = {digit: number for number, digit in enumerate("零一二三四五六七八九")}
CHINESE_NUMBERS.update({"〇": 0, "两": 2})
NUMBER = r"(?:\d{1,2}|[零〇一二两三四五六七八九十]{1,3})"
YEAR = r"(?:(?P<year>\d{4}|\d{2}|[零〇一二三四五六七八九]{4}|[零〇一二三四五六七八九]{2})年|(?P<relative_year>明年|今年|后年|去年))\s*"
DATE = re.compile(rf"(?:(?:{YEAR})?(?P<month>{NUMBER})月\s*|(?P<relative_month>这个月|本月|下个月|下月))?(?P<day>{NUMBER})[日号]")
ISO_DATE = re.compile(r"(?<!\d)(?P<year>\d{4})(?P<separator>[-/])(?P<month>\d{1,2})(?P=separator)(?P<day>\d{1,2})(?!\d)")
WEEKDAY = re.compile(r"(?P<week>下下|下|这|本|上)?(?:个)?(?:周|星期|礼拜|禮拜|週)(?P<day>[一二三四五六日天末1234567])")
WEEK_RANGE = re.compile(r"(?P<week>下下|下|这|本|上)(?:个)?(?:周|星期|礼拜|禮拜|週)")
WEEKDAY_RANGE = re.compile(r"(?P<week>下下|下|这|本|上)?(?:个)?周一(?:到|至)(?:周)?五")
RELATIVE = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3, "昨天": -1}
CLOCK = re.compile(rf"({NUMBER})(?:[:：](\d{{2}})|点(?:一刻|三刻|钟|整|半|{NUMBER}分?)?)")
DAY_DISTANCE = re.compile(rf"(?:过(?P<a>{NUMBER})天|(?P<b>{NUMBER})天(?:之?后))")
MONTH_END = re.compile(r"(?P<next>下(?:个)?月)(?:的)?(?:月底|末|最后一(?:天|日))|"
                       r"(?:本月|这个月)(?:的)?最后一(?:天|日)|月底|月末")
INTERVAL = re.compile(r"^\s*(?:到|至|[-~～—])\s*(?:上午|下午|中午|晚上)?\s*$")
QUARTER_BEFORE = re.compile(rf"差(?:一刻(?:钟)?|十五分钟)(?:到)?({NUMBER})点")
TIME_BOUND = re.compile(r"^\s*(?P<inclusive>及以后|以后(?:都|均)可|开始|起|及以前)|^\s*(?P<strict>(?:以|之)?[前后]|过了)")
PERIOD = re.compile(r"凌晨|半夜|夜里|上午|早上|中午|下午|晚上")
AVAILABILITY_QUESTION = re.compile(
    r"行不行|可不可以|能不能|方不方便|有没有|合不合适|来得及吗|(?:可以|行|方便|合适|有空|能约|能办|能去)[吗嘛么]|"
    r"几点|什么时间|啥时候|什么时候|能(?:约|去)|有(?:空|号|位置)|还有|[？?]"
)


def has_calendar_day(text):
    return bool(DATE.search(text) or ISO_DATE.search(text) or WEEKDAY.search(text) or WEEK_RANGE.search(text)
                or DAY_DISTANCE.search(text) or MONTH_END.search(text) or re.search(r"明儿个?|今儿个?|后儿个?", text)
                or any(word in text for word in (*RELATIVE, "工作日", "平日", "周末")))


def time_revision(text, *, contextual=False):
    text = ISO_DATE.sub(r"\g<year>年\g<month>月\g<day>日", text)
    if contextual:
        # Keep a weekday numeral from joining an adjacent hour, as in 周五四点半.
        text = WEEKDAY.sub(r"\g<0> ", text)
        for colloquial, standard in (("明儿个", "明天"), ("今儿个", "今天"), ("后儿个", "后天"),
                                     ("明儿", "明天"), ("今儿", "今天"), ("后儿", "后天")):
            text = text.replace(colloquial, standard)
        text = QUARTER_BEFORE.sub(lambda match: f"{(number(match[1]) - 1) % 24}点45分", text)
        if DAY_DISTANCE.search(text):
            text = re.sub(r"(?:从|打)?今天(?:开始|算起|算)[，,]?\s*", "", text)
        query, separator, constraint = text.partition("？")
        if separator and CLOCK.search(query) and re.search(r"(?:我|还)(?:得|要|需要)(?:接|送|上班|开会|赶)", constraint):
            text = query
    parts = re.split(r"改到|改成|改为|换到|换成|还是约|还是选|我说的是|我的意思是", text)
    revised = parts[-1]
    if len(parts) > 1 and re.fullmatch(r"\s*(?:那个|这个)吧?[。！!？?\s]*", revised):
        revised = parts[-2]
    revised = re.sub(rf"^[嗯啊呃额哦哎，,\s]*(?:那|我|我想)?还是(?=\s*(?:上午|下午|晚上)?\s*{CLOCK.pattern})",
                     "", revised)
    contrast = re.search(r"不是([^，,。；;]+?)[，,\s]*是\s*", revised)
    if contrast and (CLOCK.search(contrast[1]) or has_calendar_day(contrast[1])) and (
            CLOCK.search(revised, contrast.end()) or has_calendar_day(revised[contrast.end():])):
        return revised[contrast.end():]
    clauses = [clause for clause in re.split(r"[，,。；;]", revised) if clause.strip()]
    if contextual and len(clauses) > 1:
        primary = [clause for clause in clauses if has_calendar_day(clause) and CLOCK.search(clause) and re.search(
            r"(?:只|仅)(?:能|想|要)(?:约|选|订)|(?:我要|我想)(?:约|选|订)(?:了|上|这个)?\s*$", clause)]
        if len(primary) == 1 and any(re.search(r"不能来|来不了|只是备用|先别约|先不订", clause) for clause in clauses if clause != primary[0]):
            return primary[0]
    if contextual and len(clauses) > 1 and has_calendar_day(clauses[-1]) and CLOCK.search(clauses[-1]) and re.search(
            r"说错了|更正一下|不对.{0,5}是|我想改成", "，".join(clauses[:-1])):
        return clauses[-1]
    if contextual and len(clauses) > 1 and re.search(r"^(?:等等|等一下|说错了|更正|应该是)", clauses[-1].strip()) and CLOCK.search(clauses[-1]):
        day_text = re.split(CLOCK, clauses[0], maxsplit=1)[0] if has_calendar_day(clauses[0]) else ""
        revised = day_text + clauses[-1].strip()
        clauses = [revised]
    if contextual and len(clauses) > 1 and re.search(
            r"(?:不要|不行|不用|不约|不选)\s*$", clauses[-1]) and CLOCK.search(clauses[-1]) and CLOCK.search(clauses[0]):
        clauses.pop()
        revised = "，".join(clauses)
    for i in range(len(clauses) - 2, -1, -1):
        declined = re.search(r"不是|不要|不选|不约|(?<!行)不行|太早|太晚|(?<!方)不方便", clauses[i])
        conflict = contextual and has_calendar_day(clauses[i]) and re.search(
            r"有事|有安排|要开会|去不了|来不了|走不开|抽不开|赶不及", clauses[i])
        if ((declined or conflict)
                and (CLOCK.search(clauses[i + 1]) or has_calendar_day(clauses[i + 1]))):
            return "，".join(clauses[i + 1:])
    return revised


def clock_parts(clock):
    suffix = clock[0].split("点", 1)[-1]
    minute_text = re.search(NUMBER, suffix) if "点" in clock[0] else None
    spoken_minutes = {"半": 30, "一刻": 15, "三刻": 45}
    minute = int(clock[2]) if clock[2] else spoken_minutes.get(suffix, number(minute_text[0]) if minute_text else 0)
    return number(clock[1]), minute


def bank_wall_minutes(text, clock):
    hour, minute = clock_parts(clock)
    period = PERIOD.search(text)
    if 1 <= hour < 12 and (period and period[0] in {"中午", "下午", "晚上"} or not period and not clock[2]):
        hour += 12
    return hour * 60 + minute


def split_nearest(text, slots, today):
    """Choose an in-window suggestion, without proposing a write."""
    reference = re.search(r"离\s*(?P<clock>" + CLOCK.pattern + r")\s*最近", text)
    if not reference or not re.search(r"只有|只能", text) or not has_calendar_day(text):
        return None
    windows = re.split(r"[、，,]?或者|[、，,]?或是", text[:reference.start()])
    if len(windows) != 2 or not all(CLOCK.search(window) for window in windows):
        return None
    clock = CLOCK.search(windows[0])
    prefix = windows[0][:clock.start()]
    allowed = set(slot_matches(windows[0], slots, today) or ())
    allowed.update(slot_matches(prefix + windows[1], slots, today) or ())
    reference_clock = CLOCK.search(reference["clock"])
    ref = bank_wall_minutes(prefix + reference["clock"], reference_clock)
    ranked = []
    for slot in slots:
        if slot["id"] in allowed:
            when = datetime.fromisoformat(slot["start"])
            ranked.append((abs(when.hour * 60 + when.minute - ref), when, slot["id"]))
    return (min(ranked)[2],) if ranked else ()


def inconsistent_clock_bounds(text):
    lower, upper = [], []
    for clock in CLOCK.finditer(text):
        bound = TIME_BOUND.match(text[clock.end():])
        if not bound:
            continue
        minute = bank_wall_minutes(text, clock)
        if bound[0].endswith("前"):
            upper.append(minute - bool(bound["strict"]))
        else:
            lower.append(minute + bool(bound["strict"]))
    return lower and upper and max(lower) > min(upper)


def ambiguous_time(text):
    times = list(CLOCK.finditer(text))
    if len(times) >= 2 and INTERVAL.fullmatch(text[times[0].end():times[1].start()]):
        return False
    return len({clock_parts(match) for match in times}) > 1


def number(text):
    if text.isdecimal():
        return int(text)
    if "十" in text:
        tens, ones = text.split("十", 1)
        if len(tens) > 1 or len(ones) > 1 or "十" in ones:
            return -1
        return CHINESE_NUMBERS.get(tens, 1) * 10 + CHINESE_NUMBERS.get(ones, 0)
    return int("".join(str(CHINESE_NUMBERS[part]) for part in text))


def date_weekday_conflict(text, today):
    calendar_date = DATE.search(text)
    weekday = WEEKDAY.search(text)
    if not calendar_date or not weekday:
        return False
    day = number(calendar_date["day"])
    year = year_for(calendar_date, today)
    if calendar_date["month"]:
        month = number(calendar_date["month"])
        try:
            requested = date(year if year is not None else today.year + (month < today.month), month, day)
        except ValueError:
            return True
    else:
        month_start = today.replace(day=1)
        if calendar_date["relative_month"] in {"下个月", "下月"}:
            month_start = (month_start + timedelta(days=32)).replace(day=1)
        if not calendar_date["relative_month"] and (day < today.day or day > monthrange(today.year,today.month)[1]):
            month_start = (month_start + timedelta(days=32)).replace(day=1)
        try:
            requested = month_start.replace(day=day)
        except ValueError:
            return True
    weekday_name = weekday["day"]
    if weekday["week"]:
        monday = today - timedelta(days=today.weekday()) + timedelta(
            weeks={"下": 1, "下下": 2, "上": -1}.get(weekday["week"], 0))
        if not monday <= requested < monday + timedelta(days=7):
            return True
    if weekday_name == "末":
        return requested.weekday() < 5
    spoken_weekday = 7 if weekday_name in "日天" else number(weekday_name)
    return requested.isoweekday() != spoken_weekday


def year_for(date_match, today):
    if not date_match:
        return None
    if date_match["year"]:
        year = number(date_match["year"])
        return year if len(date_match["year"]) == 4 else today.year // 100 * 100 + year
    if date_match["relative_year"]:
        return today.year + {"今年": 0, "明年": 1, "后年": 2, "去年": -1}[date_match["relative_year"]]
    return None


def weekday_limits(text, today):
    weekdays = []
    for match in WEEKDAY.finditer(text):
        if match["day"] == "末":
            continue
        tail = re.split(r"[，,。！？?；;]", text[match.end():], maxsplit=1)[0].lstrip()
        day = 6 if match["day"] in "日天" else number(match["day"]) - 1
        if re.match(r"之前不行|以前不行|当天(?:也)?(?:有事|要上班|不能去|没空)|轮班不能去|不能去", tail):
            kind = "until" if tail.startswith(("之前", "以前")) else "excluded"
        elif re.match(r"(?:之|以)?后|起|开始|当天(?:就)?可以", tail):
            kind = "after" if tail.startswith(("之后", "以后", "后")) else "from"
        else:
            continue
        weekdays.append((match["week"], day, kind))
    if not weekdays:
        return None
    weeks = {week for week, _, _ in weekdays if week}
    if len(weeks) > 1:
        return None
    offset = {"下": 1, "下下": 2, "上": -1}.get(next(iter(weeks), None), 0)
    week_start = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    selected = range(7)
    for _, day, kind in weekdays:
        if kind == "excluded":
            selected = [index for index in selected if index != day]
        else:
            earliest = day + (kind == "after")
            selected = [index for index in selected if index >= earliest]
    return {week_start + timedelta(days=index) for index in selected}


def slot_matches(text, slots, today):
    """Filter real inventory using the calendar words the caller actually said."""
    text = WEEKDAY_RANGE.sub(lambda match: f'{match["week"]}周工作日' if match["week"] else "工作日", text)
    workdays = any(word in text for word in ("工作日", "平日"))
    dates = {tuple(match.groupdict().values()): match for match in DATE.finditer(text)}
    weeks = {(match["week"], match["day"].replace("天", "日")): match for match in WEEKDAY.finditer(text)}
    week_range = WEEK_RANGE.search(text) if not weeks else None
    relative = set(re.findall(r"大后天|今天|明天|后天|昨天", text))
    distance = DAY_DISTANCE.search(text)
    month_end = MONTH_END.search(text)
    week_limits = weekday_limits(text, today) if weeks and not (dates or relative or distance or month_end) else None
    explicit_date_with_weekday = len(dates) == len(weeks) == 1 and not (relative or distance)
    if explicit_date_with_weekday and date_weekday_conflict(text, today):
        return ()
    if week_limits is None and not explicit_date_with_weekday and len(dates) + len(weeks) + len(relative) + bool(distance) > 1:
        return None
    date_match = next(iter(dates.values()), None)
    week = next(iter(weeks.values()), None)
    clock = CLOCK.search(text)
    period_match = PERIOD.search(text)
    period = period_match[0] if period_match else None
    if not (date_match or week or week_range or relative or distance or month_end or clock or period or workdays):
        return None

    days = None
    if week_limits is not None:
        days = week_limits
    elif relative:
        days = {today + timedelta(days=RELATIVE[next(iter(relative))])}
    elif distance:
        days = {today + timedelta(days=number(distance["a"] or distance["b"]))}
    elif month_end:
        month_anchor = (today.replace(day=1) + timedelta(days=32)) if month_end["next"] else today
        days = {month_anchor.replace(day=monthrange(month_anchor.year, month_anchor.month)[1])}
    elif (week or week_range) and not explicit_date_with_weekday:
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
    year = year_for(date_match, today)
    relative_month = date_match["relative_month"] if date_match else None
    month_start = today.replace(day=1)
    if relative_month in {"下个月", "下月"}:
        month_start = (month_start + timedelta(days=32)).replace(day=1)

    minute = None
    hours = set()
    bound = None
    interval = None
    if clock:
        hour, minute = clock_parts(clock)
        if period in {"中午", "下午", "晚上"} and 1 <= hour < 12:
            hour += 12
        hours.add(hour)
        if not period and not clock[2] and 1 <= hour < 12:
            hours.add(hour + 12)
        bound = TIME_BOUND.match(text[clock.end():])
        upper_clock = CLOCK.search(text, clock.end())
        if upper_clock and INTERVAL.fullmatch(text[clock.end():upper_clock.start()]):
            upper_hour, upper_minute = clock_parts(upper_clock)
            if (period in {"中午", "下午", "晚上"} or not period and not upper_clock[2]) and 1 <= upper_hour < 12:
                upper_hour += 12
            interval = (max(hours) * 60 + minute, upper_hour * 60 + upper_minute)

    matching = []
    for slot in slots:
        start = datetime.fromisoformat(slot["start"])
        if workdays and start.weekday() >= 5:
            continue
        if days is not None and start.date() not in days:
            continue
        if day is not None and (start.day != day or month is not None and start.month != month
                                or year is not None and start.year != year):
            continue
        if relative_month and (start.year, start.month) != (month_start.year, month_start.month):
            continue
        if (period in {"凌晨", "半夜", "夜里"} and not 0 <= start.hour < 6
                or period in {"上午", "早上"} and start.hour >= 12 or period == "中午" and not 11 <= start.hour < 14
                or period == "下午" and not 12 <= start.hour < 18 or period == "晚上" and start.hour < 18):
            continue
        if clock:
            if interval:
                lower, upper = interval
                if not lower <= start.hour * 60 + start.minute <= upper:
                    continue
            elif bound:
                reference = max(hours) * 60 + minute
                starts_at = start.hour * 60 + start.minute
                difference = reference - starts_at if bound[0].endswith("前") else starts_at - reference
                if difference < 0 or difference == 0 and bound["strict"]:
                    continue
            elif start.hour not in hours or start.minute != minute:
                continue
        matching.append(slot["id"])
    return tuple(matching)
