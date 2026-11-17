import re
import unicodedata

from services.bank import BANK_BRANCH
from services.dialogue import IDENTITY_FILLERS


NAME_PROMPT = "请问怎么称呼您？麻烦说一下您的全名。"
PHONE_PROMPT = "方便留一下预约手机号吗？您可以逐位说。"
PHONE_RETRY = "刚才没收齐号码。可以先说前三位，我接着问后面的；也可以点「打开文字输入」直接输入，或说「转人工」。"
CUSTOMER_OPENING = f"您好，这里是{BANK_BRANCH}的预约服务，我是 AI 客服。{NAME_PROMPT}"
NAME = r"[\u3400-\u9fff]{2,6}(?:·[\u3400-\u9fff]{1,12}){0,3}"
NAME_LABEL = r"(?:(?:我|本人)?(?:的)?(?:全名|姓名|名字)|我(?:这边)?叫|我是)(?:是|叫|为|改成|改为|更正为|改)?"
PHONE_DIGITS = str.maketrans("零〇洞一幺二两三四五六七八九", "00011223456789")
PHONE_VARIANTS = str.maketrans("拐勾", "79")
PHONE_RUN = re.compile(r"\+?[0-9](?:[\s,/／、，.。;；()（）\-－–—]*[0-9])*")
SPOKEN_PHONE_EN = {word: str(digit) for digit, word in enumerate("zero one two three four five six seven eight nine".split())}
ENGLISH_PHONE_PART = r"(?<![A-Za-z])(?:" + "|".join(SPOKEN_PHONE_EN) + r")(?![A-Za-z])"
ENGLISH_PHONE_WORD = re.compile(ENGLISH_PHONE_PART, re.IGNORECASE)
PHONE_OWNER = re.compile(r"(?:朋友|同事|家属|妈妈|爸爸|丈夫|妻子|对象|爱人|我妈|我爸|我哥|我姐|别人|对方|他|她)"
                         r"(?:(?:自己的?|的)?(?:手机号(?:码)?|电话(?:号码)?|联系方式|号码)(?:是|为)?|的是(?=\s*\+?[0-9]))")
OWN_CONTACT = re.compile(r"(?:我自己(?:的)?|本人(?:的)?|我的)(?:实际|现在的?|正确的?)?"
                         r"(?:(?:手机号(?:码)?|电话(?:号码)?|联系号码|号码)(?:是|为)?|是(?=\s*[+0-9一幺Oo]))")
PHONE_SEGMENT = re.compile(r"^(?:(?:我的)?(?:手机号|号码)(?:的)?)?(?P<position>最前|前(?:面|头)?|中间|最后|末尾|后)"
                           r"\s*(?:那)?[三四七347](?:个)?位")


def phone_tokens(text):
    normalized = unicodedata.normalize("NFKC", text).translate(PHONE_VARIANTS)
    return ENGLISH_PHONE_WORD.sub(lambda match: SPOKEN_PHONE_EN[match[0].lower()], normalized)


def phone_archive_text(text, capturing):
    # Only normalize spoken English digits when they form a number run; keep ordinary speech verbatim.
    if capturing:
        return ENGLISH_PHONE_WORD.sub(lambda match: SPOKEN_PHONE_EN[match[0].lower()], text)
    run = re.compile(rf"(?:{ENGLISH_PHONE_PART})(?:[\s,/／、，.。;；\-–—]+(?:{ENGLISH_PHONE_PART})){{2,}}",
                     re.IGNORECASE)
    return run.sub(lambda match: ENGLISH_PHONE_WORD.sub(lambda word: SPOKEN_PHONE_EN[word[0].lower()], match[0]), text)


def phone_context(text):
    return bool(re.search(r"手机号|手机号码|电话号码|联系电话|联系方式|报号码|尾号", text))


def phone_segment(text):
    clean = unicodedata.normalize("NFKC", text)
    clean = re.sub(r"^[嗯啊呃额哦喂\s，,]+", "", clean)
    match = PHONE_SEGMENT.match(clean)
    if not match:
        return None
    return "middle" if match["position"] == "中间" else "end" if match["position"] in {"最后", "末尾", "后"} else "start"


def other_phone(text):
    return bool(PHONE_OWNER.search(text))
NOT_NAMES = {"本人", "是本人", "是我", "是的", "对的", "没错", "好的", "可以", "不行", "不是",
             "全名", "姓名", "名字", "手机", "手机号", "电话号码", "你是谁", "怎么称呼", "您好",
             "不要", "没听清", "再说一遍", "什么", "稍等", "不知道", "我姓", "我是", "你好"}


def customer_name(text, *, unlabelled=False, strict=False):
    clean = unicodedata.normalize("NFKC", text).strip().replace("・", "·")
    if strict:
        corrected = re.search(r"(?:不是[\u3400-\u9fff]{2,6}[，,]是|[，,](?:不对|说错了)[，,])\s*"
                              rf"(?:我叫|我姓|叫|是)?({NAME})(?=[，,。；;！!]|$)", clean)
        if corrected and (unlabelled or re.search(NAME_LABEL, clean[:corrected.start()])):
            return corrected[1]
        if unlabelled:
            clean = re.sub(r"^我姓[\u3400-\u9fff]{1,2}[，,]\s*叫", "我叫", clean)
    for clause in re.split(r"[，,。；;！!、]+", clean):
        clause = IDENTITY_FILLERS.sub("", re.sub(r"\s+", "", clause))
        labelled = re.match(NAME_LABEL, clause)
        if not labelled and not unlabelled:
            continue
        name = clause[labelled.end():] if labelled else clause
        phone_label = r"(?:我的)?(?:手机号(?:码)?|电话(?:号码)?|联系方式)[:：]?"
        if strict and not labelled:
            phone_label += r"(?=(?:是|为)?[:：]?\+?[0-9零〇洞一幺二两三四五六七八九])"
        name = re.split(phone_label, name, maxsplit=1)[0]
        name = name.strip(":：")
        if strict and labelled:
            name = re.sub(r"[啊呀呢啦]$", "", name)
        if name.endswith(("先生", "女士", "小姐")) or (not labelled and re.search(r"^[我你您]|[吗嘛呢吧啊啦了]$", name)):
            continue
        if name not in NOT_NAMES and re.fullmatch(NAME, name):
            return name
    return None


def customer_phone(text):
    corrected = re.split(r"说错了|报错了|正确(?:的)?(?:手机号|电话|号码)(?:是|为)?", text)
    if len(corrected) > 1 and len(corrected[-1].strip(" ，,。")) >= 11:
        text = corrected[-1]
    own = OWN_CONTACT.search(text) if other_phone(text) else None
    if other_phone(text) and not own:
        return None
    if own:
        text = re.split(r"[，,。；;]", text[own.end():], maxsplit=1)[0]
    normalized = phone_tokens(text).translate(PHONE_DIGITS)
    normalized = re.sub(r"(?<=[0-9])([\s，,、。]+)[嗯啊呃额哦]+([\s，,、。]+)(?=[0-9])", r"\1\2", normalized)
    numbers = set()
    for match in PHONE_RUN.finditer(normalized):
        digits = re.sub(r"[^0-9]", "", match[0])
        if len(digits) == 13 and digits.startswith("86"):
            digits = digits[2:]
        if len(digits) == 15 and digits.startswith("0086"):
            digits = digits[4:]
        if re.fullmatch(r"1[3-9][0-9]{9}", digits):
            numbers.add(digits)
    return next(iter(numbers)) if len(numbers) == 1 else None


def customer_phone_chunk(text):
    if other_phone(text):
        return None
    clean = phone_tokens(text)
    clean = re.sub(r"[\s，,。；;：:！!、.()（）－–—-]+", "", clean)
    clean = re.sub(r"^[嗯啊呃额哦喂]+", "", clean)
    clean = re.sub(r"^(?:我(?:的)?)?(?:预约)?(?:手机号(?:码)?|电话(?:号码)?|"
                   r"(?:最前|最后|末尾|后|前(?:面|头)?|中间)?(?:[三四七347](?:个)?位|一段)|接着是|还有|然后)"
                   r"(?:是|为|改成|改为|更正为|说成)?", "", clean)
    clean = re.sub(r"(?:就这些|说完了|完了|啊|吧|哈)$", "", clean)
    clean = clean.translate(PHONE_DIGITS).translate(str.maketrans("拐勾", "79"))
    if re.fullmatch(r"[0-9]{3}|[0-9]{4}|1[3-9][0-9]{5}|1[3-9][0-9]{9}", clean):
        return clean
    return None


def customer_phone_declined(text):
    return bool(re.search(r"(?:不方便|不想|不愿意?|不太好|不好)(?:跟您|跟你|跟)?(?:说|报|留|提供)|"
                          r"不(?:方便|想|愿意?)?给(?:手机号|手机号码|号码|电话)|"
                          r"不(?:报|留|提供)(?:手机号|手机号码|号码|电话)|"
                          r"(?:手机号|手机号码|号码|电话)(?:我)?(?:不|就不|不用)(?:报|留|提供|给)|"
                          r"(?:不用|别|不要)(?:再)?用(?:这个|原来(?:的)?|刚才(?:的)?)号码|"
                          r"(?:号码|电话)(?:已经)?(?:作废|不用了)", text))


def customer_phone_revoked(text):
    return bool(re.search(r"(?:手机号|号码|电话)(?:已经)?(?:作废|不用了)|撤回.{0,10}(?:手机号|号码|电话).{0,4}授权|"
                          r"(?:不要|别|不用)(?:再)?用(?:这个|原先|原来|刚才)(?:的)?(?:手机号|号码|电话)|"
                          r"(?:不同意|不授权|不允许)(?:使)?用.{0,8}(?:手机号|号码|电话)(?!查预约|查这笔|查询预约)|"
                          r"(?:不同意|不授权|不允许).{0,10}(?:联系方式|手机号).{0,6}(?:办|改|撤|提交)", text))


def customer_phone_correction(text):
    return bool(re.search(r"(?:手机号|电话|号码|尾号|最后.{0,3}位|中间.{0,4}位|前.{0,3}位)"
                          r".{0,14}(?:不是|不对|不准确|错了|有误|说错|改一?下|改成|改为|更正)|"
                          r"(?:更正|改一下|改成).{0,8}(?:手机号|电话|号码|尾号)", text))


def customer_tail_differs(text, tail):
    clean = phone_tokens(text)
    label = re.search(r"(?:尾号|末尾|后[四4]位|最后[四4]位)\s*(?:应当|应该|记得)?(?:是|为)?\s*", clean)
    match = re.match(r"([0-9](?:[\s.、-]*[0-9]){3})(?!\s*[0-9])", clean[label.end():].translate(PHONE_DIGITS)) if label else None
    return bool(match and re.sub(r"\D", "", match[1]) != tail)


def customer_tail_acknowledged(text, tail):
    compact = re.sub(r"[，,。！!\s]", "", unicodedata.normalize("NFKC", text)).translate(PHONE_DIGITS)
    return bool(re.fullmatch(rf"(?:手机号)?尾号(?:是)?{re.escape(tail)}(?:对|没错|正确)", compact))


def intake_stage(intake):
    if not intake["name"]:
        return "name"
    return "confirm" if intake["phone_key"] else "phone"
