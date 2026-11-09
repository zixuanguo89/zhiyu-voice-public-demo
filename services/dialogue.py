import re
from datetime import datetime

OPENING = "您好，我是智语预约服务的 AI 客服。这是预约服务模拟通话。查询或办理前，请先输入演示验证码。"
SPOKEN_OPENING = "您好，我是智语预约服务的 AI 客服。请问您是预约本人吗？也可以一起告诉我您要办理什么。"
SPOKEN_RETRY = "查询或更改前，请先确认您是本次预约的本人，再告诉我您需要办理什么。"
AFFIRM = {"确认", "确认办理", "确认改期", "确认取消", "是的", "同意", "我确认", "我同意", "确定",
          "确认修改", "好的确认", "好的，确认", "我确认改期", "我确认取消",
          "确认预约", "我确认预约", "确认取消预约"}
POLICY_ID = "reservation.zh-CN.v1"
BASELINE = {"clarification": "请告诉我具体日期或选择一个可用时段。", "intent_prompt": ""}
ID_DIGITS = str.maketrans("〇零一二三四五六七八九０１２３４５６７８９", "001234567890123456789")
IDENTITY_DENIAL = re.compile(r"不是(?:[我您你]?本人|我自己|给我)|非本人|(?:替|帮|给)(?:别人|他人|我爸|我妈|家人)|代办")
IDENTITY_FILLERS = re.compile(r"^(?:你好|您好|喂|嗯|啊|呃|额|唔|哦|噢|哎)+")
IDENTITY_YES = re.compile(r"(?:(?:是的|对的|是我|没错)+|是+的?|对+的?)[啊呀呢]?")
IDENTITY_SELF = (r"我+是+(?:(?:本次|这次)?预约(?:人|的)?本人|预约人)"
                 r"|(?:我+就?)?是+(?:我+)?本人|我?本人")
IDENTITY_ASSERTION = re.compile(
    rf"(?:(?:{IDENTITY_SELF})+(?:是+)?(?:来?办(?:理)?(?:这张卡|卡)?的?)?"
    r"|(?:这张卡)?是?(?:给|为)我自己办(?:理)?的?|是?我自己来?办(?:理)?卡?)"
    r"(?P<request>(?:我想|我要|我需要|我来|想|要|帮我|请|查|改|换|取消|转人工).+)?"
)
IDENTITY_REQUEST = re.compile(r"(?:我想|我要|我需要|我来|想|要|帮我|请|查|改|换|取消|转人工).+")


def canonical_ids(text):
    return re.sub(r"(?<![A-Za-z0-9])(A|S|艾斯)\s*([0-9０-９〇零一二三四五六七八九]+)(?![A-Za-z0-9])",
                  lambda match: ("S" if match[1] == "艾斯" else match[1].upper()) + match[2].translate(ID_DIGITS),
                  text, flags=re.IGNORECASE)


def denies_spoken_identity(text):
    return bool(IDENTITY_DENIAL.search(text) or text.strip().strip("。！!，,") in {"不是", "我不是", "不是我", "不对"})


def spoken_identity(text):
    """Extract a first-person answer and any accompanying request for the sandbox caller."""
    if denies_spoken_identity(text):
        return None
    clauses = [IDENTITY_FILLERS.sub("", re.sub(r"\s+", "", part))
               for part in re.findall(r"[^，,。！!、；;？?]+[？?]?", text)]
    clauses = [part for part in clauses if part]
    if any(part in {"不", "不是", "我不是", "不是我", "不对"} for part in clauses):
        return None
    for index, part in enumerate(clauses):
        # A quoted phrase is not the caller's answer.
        if re.search(r"(?:他|她|对方|客服)说|(?:让|叫)我说|你说的是", part):
            return None
        assertion = IDENTITY_ASSERTION.fullmatch(part.rstrip("？?"))
        request = assertion["request"] if assertion else None
        question = part.endswith(("吗", "么", "？", "?"))
        if assertion and (not question or (request and request.startswith(("我想", "我需要")))):
            remaining = clauses[index + 1:]
            if request:
                return "，".join([request, *remaining])
            return "，".join(remaining) if remaining and IDENTITY_REQUEST.fullmatch(remaining[0]) else ""
        if index == 0 and IDENTITY_YES.fullmatch(part):
            remaining = [clause for clause in clauses[1:] if not IDENTITY_YES.fullmatch(clause)]
            if not remaining:
                return ""
            if IDENTITY_REQUEST.fullmatch(remaining[0]) and not remaining[0].startswith("请问"):
                return "，".join(remaining)
    return None


def redacted(text, minimum=6):
    # Never persist verification codes or full telephone/ID numbers.
    digits = "0-9０-９零〇洞一二两三四五六七八九幺"
    return re.sub(rf"[{digits}](?:[\s,、，.。;；()（）\-－–—]*[{digits}]){{{minimum - 1},}}", "[已隐藏]", text)


def slot_label(slot):
    time = datetime.fromisoformat(slot["start"])
    return f'{time:%m月%d日 %H:%M}（{slot["id"]}）'


def parse(text, slots=()):
    clean = canonical_ids(text).strip().strip("。！!，,？?").strip()
    upper = clean.upper()
    if clean in AFFIRM:
        return "confirm", None
    explicit = re.fullmatch(r"(?:我)?确认(?:改到|改至|改为)\s*(S\d+)", upper)
    if explicit:
        return "confirm", explicit[1]
    if "转人工" in clean or clean in {"人工", "人工客服", "找真人"}:
        return "handoff", None
    if any(x in clean for x in ("不核验", "拒绝核验", "不提供验证码")):
        return "refuse", None
    if any(x in clean for x in ("不改了", "不取消了", "放弃办理", "算了", "先不办", "撤回本次")):
        return "discard", None
    if "取消预约" in clean and not any(x in clean for x in ("不", "能否", "怎么", "如何", "可以吗", "吗", "是否", "能不能")):
        return "cancel", None
    ids = list(re.finditer(r"(?<![A-Z0-9])S\d+(?!\d)", upper))
    if ids:
        last = ids[-1]
        distinct = len({m[0] for m in ids}) > 1
        earlier = ids[-2].end() if distinct else 0
        prefix = re.split(r"[，,；;。]", upper[earlier:last.start()])[-1]
        suffix = re.split(r"[，,；;。]", upper[last.end():], maxsplit=1)[0]
        if distinct and not any(x in prefix for x in ("改到", "换成", "改成", "改为", "换到", "选")):
            return "clarify", None
        if any(x in prefix for x in ("不", "别", "还是", "或", "查", "有没有", "看看")) or any(
                x in suffix for x in ("不要", "不行", "不选", "可以吗", "能不能")):
            return "clarify", None
        return "choose", last[0]
    date = re.search(r"(\d{1,2})月(\d{1,2})日", clean)
    if date and any(x in clean for x in ("改", "换", "选", "就", "要", "可以")) and "不" not in clean:
        matches = [s for s in slots if datetime.fromisoformat(s["start"]).strftime("%m%d") ==
                   f"{int(date[1]):02d}{int(date[2]):02d}" and s["available"]]
        if len(matches) == 1:
            return "choose", matches[0]["id"]
    if any(x in clean for x in ("可用", "时段", "查询", "查一下", "原预约", "我的预约", "有哪些")):
        return "query", None
    if any(x in clean for x in ("改期", "改约", "换时间", "下周", "下午", "周二", "周三", "取消")):
        return "clarify", None
    if any(x in clean for x in ("怎么改", "流程", "收费", "政策")):
        return "help", None
    if clean in {"继续", "听得到", "听得到，继续", "请继续"}:
        return "continue", None
    return "unrecognized", None
