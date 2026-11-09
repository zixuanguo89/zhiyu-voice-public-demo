import re
import unicodedata

from services.bank import BANK_BRANCH
from services.dialogue import IDENTITY_FILLERS


NAME_PROMPT = "请问怎么称呼您？麻烦说一下您的全名。"
PHONE_PROMPT = "方便留一下预约手机号吗？您可以逐位说。"
CUSTOMER_OPENING = f"您好，这里是{BANK_BRANCH}的预约服务，我是 AI 客服。{NAME_PROMPT}"
NAME = r"[\u3400-\u9fff]{2,6}(?:·[\u3400-\u9fff]{1,12}){0,3}"
NAME_LABEL = r"(?:(?:我|本人)?(?:的)?(?:全名|姓名|名字)|我叫|我是)(?:是|叫|为|改成|改为|更正为)?"
PHONE_DIGITS = str.maketrans("零〇洞一幺二两三四五六七八九", "00011223456789")
PHONE_RUN = re.compile(r"\+?[0-9](?:[\s,、，.。;；()（）\-－–—]*[0-9])*")
NOT_NAMES = {"本人", "是本人", "是我", "是的", "对的", "没错", "好的", "可以", "不行", "不是",
             "全名", "姓名", "名字", "手机", "手机号", "电话号码", "你是谁", "怎么称呼", "您好",
             "不要", "没听清", "再说一遍", "什么", "稍等", "不知道", "我姓", "我是", "你好"}


def customer_name(text, *, unlabelled=False):
    clean = unicodedata.normalize("NFKC", text).strip().replace("・", "·")
    for clause in re.split(r"[，,。；;！!、]+", clean):
        clause = IDENTITY_FILLERS.sub("", re.sub(r"\s+", "", clause))
        labelled = re.match(NAME_LABEL, clause)
        if not labelled and not unlabelled:
            continue
        name = clause[labelled.end():] if labelled else clause
        name = re.split(r"(?:我的)?(?:手机号(?:码)?|电话(?:号码)?|联系方式)[:：]?", name, maxsplit=1)[0]
        name = name.strip(":：")
        if name.endswith(("先生", "女士", "小姐")) or (not labelled and re.search(r"^[我你您]|[吗嘛呢吧啊啦了]$", name)):
            continue
        if name not in NOT_NAMES and re.fullmatch(NAME, name):
            return name
    return None


def customer_phone(text):
    normalized = unicodedata.normalize("NFKC", text).translate(PHONE_DIGITS)
    numbers = set()
    for match in PHONE_RUN.finditer(normalized):
        digits = re.sub(r"[^0-9]", "", match[0])
        if len(digits) == 13 and digits.startswith("86"):
            digits = digits[2:]
        if re.fullmatch(r"1[3-9][0-9]{9}", digits):
            numbers.add(digits)
    return next(iter(numbers)) if len(numbers) == 1 else None


def intake_stage(intake):
    if not intake["name"]:
        return "name"
    return "confirm" if intake["phone_key"] else "phone"
