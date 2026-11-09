import re
from datetime import datetime

from services.bank_time import slot_matches
from services.dialogue import parse

BANK_SCENARIO = "bank_card"
BANK_APPOINTMENT = "A101"
BANK_BRANCH = "海棠银行科创支行"
BANK_SERVICE = "Visa 借记卡到店办理"
BANK_OPENING = f"您好，这里是{BANK_BRANCH}的预约服务，我是 AI 客服。请问，是您本人要预约办卡吗？"
BANK_CONFIRM = "我先问一下，这张卡是给您自己办的吗？回答“是”就行。"
BANK_CONFIRM_HELP = "想确认这张卡是不是给您自己办。如果是，直接回答“是”就可以了。"
BANK_CONFIRM_FALLBACK = "这句我还没确认清楚。您可以再说一声“是”，也可以点电话页的“改用文字交谈”，输入“我是本人”。"
BANK_CHECKING = "稍等一下，我还在查这笔预约的结果。有结果我再跟您说，您也可以转人工。"
BANK_HANDOFF = "好的，稍后由人工客服与您对接"
BANK_HANDOFF_QUESTION = "要我帮您转人工吗？"
BANK_TIME_OR_HANDOFF = "您想再换个时间，还是我帮您转人工？"
BANK_NO_MATCH = "目前没有这个时段。您可以换个工作日下午，也可以说‘转人工’，我帮您登记。"
BANK_ASK_PREFERENCE = "那您哪天下午方便？大概几点也行，我帮您查。"
BANK_WEEKDAY_WINDOW = "办卡可以约在工作日下午两点到五点，每半小时一档。您一般周几方便？"
BANK_AFTERNOON_WINDOW = "那天下午还有空位。您想约早一点还是晚一点？也可以直接说几点。"
DECLINED_TIMES = re.compile(
    r"(?:都|全|一个也)(?:没(?:有)?(?:时间|空)|不(?:行|合适|方便|太方便)|来不了|去不了|赶不上|对不上)"
    r"|(?:这些|这几个|刚才(?:说|报)的)(?:时间|时段)(?:我)?(?:没(?:有)?(?:时间|空)|不方便|不合适)"
    r"|(?:没有一个|没一个)(?:合适|方便|能去)"
    r"|^(?:我)?(?:没(?:有)?(?:时间|空)|去不了|来不了)(?:啊|呀|呢)?[？?]?$"
)
BANK_INTENT_HINT = ("这是银行借记卡到店预约。区分询问某天几点、明确选时间、要求换时间和单纯应答。"
                    "只问几点或说‘嗯’‘可以吗’时，不要猜客户选了哪个时间。只引用给出的时段。")
BANK_DETOUR = "您说的这个我帮不上忙。"
DETOURS = (
    (r"天气|下雨|下雪|气温|刮风|天气预报|晴天", "天气这边查不了，出门前可以看一下本地预报。"),
    (r"球赛|足球|篮球|世界杯|球队|比分|(?:今晚|今天|昨晚)的比赛", "比赛消息这边查不了。"),
    (r"电影|电视剧|演唱会|游戏攻略|笑话|段子|唱首歌", "这个话题我不太熟。"),
    (r"餐厅|饭店|吃什么|吃啥|菜谱|午饭|晚饭|火锅|外卖", "吃饭这方面我帮不上忙。"),
    (r"快递|机票|火车票|酒店|手机维修", "这个得找对应的服务方。"),
)
BANK_FAQ = {
    "documents": "嗯，您来办卡的时候，记得带上本人有效身份证件原件，到柜台出示一下。",
    "document_type": "请带上本人有效身份证件原件。护照能否作为办卡证件，请向网点确认。",
    "branch": f"您到{BANK_BRANCH}就可以。进门后，可以先到服务台报一下预约编号。",
    "address": f"{BANK_BRANCH}是演示网点，这里没有提供真实的详细地址。",
    "other_branch": f"这条预约只对应{BANK_BRANCH}。其他网点的安排，可以帮您转人工咨询。",
    "review": "我可以先帮您约个到店时间。具体能不能办、要不要收费、什么时候领卡，得等网点现场看过材料再跟您说。",
    "scope": "我这边可以帮您约 Visa 借记卡。您想问其他卡的话，我可以帮您转人工。",
    "agent": "我是 AI 客服，可以帮您约到店办卡。想找人工的话，直接告诉我就行。",
    "privacy": "电话里不用报身份证号码，也不用提供银行卡号或验证码。",
    "cancel_help": "有有效预约的话，可以取消。我会先跟您核对是哪一笔，再请您确认。",
    "walk_in": "我能帮您预约到店时段。直接到店能否办理、需不需要排队，请向网点确认。",
    "duration": "这边能确认预约时间。到店办理要多久，得看现场排队和材料审核情况。",
}
ORDINALS = {"一": 0, "二": 1, "两": 1, "三": 2, "四": 3, "1": 0, "2": 1, "3": 2, "4": 3}


def bank_space(desk):
    return BANK_SCENARIO + ":" + desk


def is_bank_space(space):
    return space.startswith(BANK_SCENARIO + ":")


def bank_slot_time(slot):
    time = datetime.fromisoformat(slot["start"])
    return f"{time.month}月{time.day}号{bank_slot_hour(slot)}"


def bank_slot_hour(slot):
    time = datetime.fromisoformat(slot["start"])
    period = "上午" if time.hour < 12 else "下午"
    minutes = "半" if time.minute == 30 else f"{time.minute}分" if time.minute else ""
    return f"{period}{time.hour % 12 or 12}点{minutes}"


def bank_availability(slots, *, include_date=True):
    if not slots:
        return "目前没有可约的时段。您需要的话，我可以帮您转人工。"
    label = bank_slot_time if include_date else bank_slot_hour
    options = [f"第{number}个是{label(slot)}" for number, slot in enumerate(slots, 1)]
    return "我看看啊，现在还有这些时间。" + "。".join(options) + "。您看哪个方便？"


def bank_slot_offer(params, lead="", *, asked_time=False):
    if params["action"] == "cancel":
        return f"{lead}您要取消预约 {params['appointment_id']}，对吗？确定的话请说‘确认取消’。"
    when = bank_slot_time({"start": params["target_start"]})
    if params["action"] == "change":
        return f"{lead}{when}。要我帮您把到店预约改到这个时间吗？"
    if asked_time:
        return f"{when}。要我帮您约这个时间到{BANK_BRANCH}办 Visa 借记卡吗？"
    return f"{lead}{when}到{BANK_BRANCH}办 Visa 借记卡。要我帮您约这个时间吗？"


def bank_words(text):
    compact = re.sub(r"[，,。！!、；;\s]", "", text)
    return re.sub(r"^[嗯啊呃额唔哦噢哎]+", "", compact)


def bank_short_answer(text):
    if any(mark in text for mark in ("?", "？")):
        return None
    words = bank_words(text)
    if not words and re.search(r"[嗯啊呃额唔哦噢哎]", text):
        return "ack"
    if re.fullmatch(r"(?:那就|那)?(?:可以|好嘞|好吧|好的?|行的?|成|没问题|对的?|是的?|"
                    r"没错|同意|确定|确认|我同意|我确认|就这么办|就按这个来)+[啊呀呢啦吧哈]?"
                    r"(?:麻烦了|麻烦您了|麻烦你了)?", words):
        return "yes"
    if re.fullmatch(r"(?:不用|不必|不要|不需要|算了|不了|不行|不可以|不|不是|不对)[了啊呀呢吧]?", words):
        return "no"
    if re.fullmatch(r"(?:还是|那就|就是|就|我选|选|刚才)+(?:这|那)(?:个|天|个时间|个时段)[了啊呀呢吧]?"
                    r"|(?:就)?(?:按|照)刚才的[了啊呀呢吧]?", words):
        return "reference"
    if re.fullmatch(r"(?:那就|那|就)?麻烦(?:您|你)?(?:了|一下|啦)", words):
        return "polite"
    if re.fullmatch(r"(?:好(?:的)?)?(?:那就|那|就)?(?:麻烦|请)?(?:您|你)?(?:帮我)?"
                    r"转(?:吧|一下(?:吧)?|过去吧)", words):
        return "transfer"
    return None


def bank_list_reference(text, offered_slots):
    """Resolve order words against the list the caller actually heard."""
    if not offered_slots or any(mark in text for mark in ("?", "？")):
        return None
    words = bank_words(text)
    match = re.fullmatch(r"(?:那就|就|选|我选)*(最后|最早|最晚|头|前面|后面|前|后)"
                         r"的?(?:那|这|一)?个[吧啊呀]?", words)
    if not match:
        return None
    position = match[1]
    if position in {"前面", "后面", "前", "后"} and len(offered_slots) > 2:
        return ""
    return offered_slots[-1] if position in {"最后", "最晚", "后面", "后"} else offered_slots[0]


def bank_topics(text):
    if re.search(r"(?:信用卡|万事达|mastercard)", text, re.IGNORECASE):
        return ("scope",)
    topics = []
    if re.search(r"身份证号|证件号|银行卡号|验证码|报手机号", text):
        topics.append("privacy")
    elif "护照" in text:
        topics.append("document_type")
    elif re.search(r"带(?:什么|哪些|啥)|携带|材料|证件|身份证", text):
        topics.append("documents")
    if re.search(r"其他网点|别的网点|其他支行|别家银行", text):
        topics.append("other_branch")
    elif re.search(r"门牌|详细地址|具体地址", text):
        topics.append("address")
    elif re.search(r"地址|在哪里|在哪儿|哪家(?:网点|支行)|哪个网点|怎么走|柜台怎么说", text):
        topics.append("branch")
    if re.search(r"发卡|领卡|当天能领|拿卡|出卡|收费|工本费|手续费|要不要钱|多少钱|能办下来|把卡拿走", text):
        topics.append("review")
    if re.search(r"你(?:是|到底是)(?:真人|人工|AI|机器人)|你是人吗", text, re.IGNORECASE):
        topics.append("agent")
    if re.search(r"(?:能不能|能否|可以|怎么|如何)取消|取消(?:预约)?(?:可以吗|怎么弄|怎么操作)", text):
        topics.append("cancel_help")
    if re.search(r"不预约.{0,8}(?:去|到店)|直接到店|直接过去|现场排队", text):
        topics.append("walk_in")
    if re.search(r"要多久|得多久|花多长时间|办卡.*几分钟", text):
        topics.append("duration")
    return tuple(topics)


def bank_detour(text):
    if re.search(r"办卡|借记卡|信用卡|身份证|预约号|预约编号|到店预约|我的预约|(?:哪家|哪个)支行|本人|改期|可约时段|"
                 r"取消预约(?=[吗？?，。！!]|$)|(?:约|订|选|改成|改到|换成|换到)第[一二三四1234]", text):
        return None
    if re.search(r"[，,。；;？?]\s*(?:我想|我要|帮我|麻烦).{0,5}(?:预约|改|取消)|"
                 r"[，,。；;？?]\s*(?:改成|改到|换成|换到|选第|(?:还)?能约)", text):
        return None
    return next((reply for pattern, reply in DETOURS if re.search(pattern, text, re.IGNORECASE)), None)


def bank_cancellation(words):
    if re.search(r"取消(?:成功|结果|好了|了没|了没有|了吗)|(?:是不是|有没有|是否)(?:已经)?取消|撤销(?:成功|了吗)", words):
        return "cancel_status"
    if re.search(r"(?:不(?:太)?想|不打算|不准备|没想|不要|不用|不必|先不|暂不|别|不需要|不)取消|保留.{0,5}预约|还是保留原来", words):
        return "discard"
    if not re.search(r"[？?吗么]$", words) and re.fullmatch(
            r"(?:我)?(?:确认|确定|同意|对(?:的)?|就)(?:要|帮我|把它|那就)?取消(?:掉|预约|这笔预约)?(?:吧|了)?", words):
        return "confirm"
    if re.search(r"(?:能不能|能否|怎么|如何)取消|取消(?:预约)?(?:可以吗|怎么弄|怎么操作)|(?:只是(?:想)?问|只想问).{0,8}取消", words):
        return None
    if re.search(r"取消.{0,5}(?:刚才|刚刚)?(?:选的|挑的|新选的|新时段|改期申请)|(?:刚才选的|新选的).{0,5}(?:取消|不要)", words):
        return "discard"
    if re.search(r"(?:预约|这笔|那笔).{0,7}(?:取消|撤掉|撤销|退掉)|(?:取消|撤销|撤掉)(?:这笔|那笔|这个|那个|我的|原来的)?预约", words):
        return "cancel"
    if re.fullmatch(r"(?:(?:请|你|您|麻烦你?|麻烦您?)?帮我|我要|我想|请)?取消(?:掉|语|约|预|一约)?(?:吧|啊|呀)?[？?]?", words):
        return "cancel"
    return None


def bank_intent(text, available=(), active_slot=None, *, today=None, ordinal_slots=None):
    action, target = parse(text, available)
    if action == "refuse":
        return action, target
    words = bank_words(text)
    if re.search(r"(?:不用|不要|不想|不需要|不是要|别|不)(?:再|先)?(?:帮我|给我)?(?:转|找|接)?(?:人工|真人)", words):
        return "hold", None
    if action == "handoff" or re.search(
            r"(?:跟|和|找|转|接|叫|要|请)(?:个)?(?:人工客服|真人)|"
            r"(?:人工客服|真人客服).{0,5}(?:跟我联系|联系我|回电)", words):
        return "handoff", None
    declined = DECLINED_TIMES.search(words)
    if declined:
        return "decline_times", words[declined.end():]
    detour = bank_detour(text)
    if detour:
        return "off_topic", detour
    if action == "continue":
        return action, target
    cancellation = bank_cancellation(words)
    if cancellation:
        return cancellation, None
    if re.search(r"(?:恢复|重新约回|约回)(?:到)?(?:原来|原先|之前|刚才|原预约)|(?:重新约|再约)(?:回)?(?:原来|原先|之前).{0,5}(?:时间|时段)", words):
        return "rebook_previous", None
    if action == "discard":
        return action, target
    if re.fullmatch(r"我?(?:现在)?不想办了(?:吧)?", words):
        return "stop_request", None
    if re.search(r"别(?:约|订|改)(?:了|了吧|$)|不要了.*不订|不订了|不办了|不取消|别给我改|不用改|先不用了", words):
        return "discard", None
    if words.rstrip("？?") in {"可以吗", "行吗", "好吗", "确认预约吗"}:
        return "clarify", None
    if re.fullmatch(r"(?:好嘞|好的?)?(?:谢谢(?:你|您)?|谢了|多谢)", words):
        return "courtesy", None
    short = bank_short_answer(text)
    if short in {"yes", "reference"}:
        return "agree", None
    if short == "no":
        return "discard", None
    if not any(mark in text for mark in ("?", "？")):
        yes = r"(?:可以|好的?|好嘞|行|成|没问题|是的)+[的啊呀吧]?"
        same = r"(?:就(?:按这个来|这么定|这样定|它)|就?(?:这个|那个)(?:时间|时段)|就(?:这个|那个|这天))吧?"
        assistance = r"(?:那就)?(?:麻烦(?:你|您)?)?帮我(?:约上?|订|办)(?:吧|一下)?"
        if re.fullmatch(rf"{yes}(?:{same}|{assistance})?|(?:可以了)?{same}(?:{assistance})?", words):
            return "agree", None
        if re.fullmatch(r"我?同意(?:改期|预约|取消(?:预约)?)", words):
            return "confirm", None
        if action == "confirm":
            return action, target
        spoken, slot = parse(words)
        if spoken == "confirm":
            return spoken, slot
        if re.fullmatch(r"(?:不|不要|不行|不可以)+[了吧]?", words):
            return "discard", None
    elif action == "confirm":
        return "clarify", None
    topics = bank_topics(text)
    if topics and topics != ("documents",):
        return "bank_faq", topics
    if action == "cancel" or re.search(r"(?:预约|那笔|那个).{0,6}撤掉|(?:帮我|我要)撤销预约", words):
        return "cancel", None
    if re.search(r"我(?:有预约|(?:已经|之前|这次)?约[的了]|的预约)|预约号|预约编号", words):
        return "appointment_query", None
    if re.fullmatch(r"(?:还是)?(?:换|改)(?:另)?一个(?:吧)?[？?]?", words):
        return "change_request", None

    correction = re.split(r"改到|改成|换到|换成|还是约|还是选", text)
    time_words = correction[-1]
    if len(correction) > 1 and re.fullmatch(r"(?:那个|这个)(?:吧)?[。！!？?]?", bank_words(time_words)):
        time_words = correction[-2]
    if re.search(r"或者|或是|还是", time_words):
        return "clarify_choice", None
    if re.search(r"让我想想|我再想想|考虑一下|^不确定$", words) or (
        re.match(r"^(?:稍等|等等|等一下|等会儿)", words) and not re.search(r"改|换|选|约|还有|几点|[0-9一二三四五六七八九十][号日]", words)
    ):
        return "hold", None

    lookup_only = bool(re.search(r"只是问|先问问|只想问|只问一下|不用.{0,5}(?:约|订)", words))
    rank_end = r"(?:个|项|(?=[。！!，,？?\s]*$))" if ordinal_slots is not None else r"(?:个|项)"
    ranks = list(re.finditer(rf"第([一二两三四1234]){rank_end}", text))
    if ranks:
        rank = ranks[-1]
        start = ranks[-2].end() if len(ranks) > 1 else 0
        prefix = re.split(r"改成|改到|换成|换到|还是选", text[start:rank.start()])[-1]
        if re.search(r"不要|不选|不行", prefix + text[rank.end():]):
            return "clarify_choice", None
        position = ORDINALS[rank[1]]
        options = available if ordinal_slots is None else ordinal_slots
        if position >= len(options):
            return "ask_time", ()
        if lookup_only:
            return "lookup_time", (options[position]["id"],)
        if re.search(r"几点|什么时间|什么时候|可以吗", text):
            return "ask_time", (options[position]["id"],)
        return "choose", options[position]["id"]
    if re.search(r"(?:哪个|什么时候|什么时间|(?:安排|约|选)个?)最(?:早|晚)|最(?:早|快)(?:是|能约|有).{0,6}(?:哪天|哪日|几点)", words):
        last = -1 if "最晚" in words else 0
        return "ask_time", (available[last]["id"],) if available else ()

    asks_time = bool(re.search(r"几点|什么时间|啥时候|什么时候|可以吗|能(?:约|去)|有(?:没有|空|号|位置)|还有|有上午", text))
    if topics and not asks_time:
        return "bank_faq", topics
    relative = re.search(r"([早晚])(?:一)?点(?:儿)?|([早晚])一些", words)
    if relative and not topics:
        return "relative_time", -1 if "早" in relative[0] else 1
    if today is not None and not re.search(r"不要|不选|不约", time_words):
        contrast = re.split(r"(?:不行|太早|太晚|不方便)[，,。；;]*", time_words)
        slots = [*available, active_slot] if asks_time and active_slot else available
        matches = slot_matches(contrast[-1], slots, today)
        if matches is not None:
            if lookup_only:
                return "lookup_time", matches
            if asks_time or len(matches) != 1:
                return "ask_time", matches
            return "choose", matches[0]
        if len(contrast) > 1:
            return "change_request", None
    if topics:
        return "bank_faq", topics
    if re.search(r"(?:换|改)(?:个|一下)?(?:时间|时段|日期)|不是这个|"
                 r"(?:还有|有没有|有无|有)(?:别的|其他|其它)(?:时间|时段|日期)", words):
        return "change_request", None
    if asks_time and re.search(r"那天|那个|刚才|刚刚|这个时间", words):
        return "recap_time", None
    if any(term in text for term in ("再说一遍", "重复一遍", "刚才没听清")):
        return ("recap_time" if re.search(r"时间|时段", text) else "continue"), None
    if re.search(r"(?:平时)?(?:什么时候|什么时段|哪天|周几|星期几).{0,8}(?:能办|可以办|能约|可以约)|"
                 r"(?:几点到几点|开放时间|营业时间|有哪些(?:可约的)?时间)", words):
        return "query", None
    if action in {"unrecognized", "clarify"} and (
        any(term in text.lower() for term in ("借记卡", "储蓄卡", "办卡", "开卡", "到店", "visa", "预约"))
        or re.search(r"(?:办(?:理)?|申请|开)\s*(?:一?张\s*)?(?:vi[az]\s*)?卡", text, re.IGNORECASE)
    ):
        return "query", None
    return action, target
