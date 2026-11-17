import re
from datetime import datetime

from services.bank_time import (AVAILABILITY_QUESTION, CLOCK, ambiguous_time, date_weekday_conflict, has_calendar_day,
                                inconsistent_clock_bounds, slot_matches, split_nearest, time_revision)
from services.dialogue import parse

BANK_SCENARIO = "bank_card"
BANK_APPOINTMENT = "A101"
BANK_BRANCH = "海棠银行科创支行"
BANK_SERVICE = "Visa 借记卡到店办理"
BANK_OPENING = f"您好，这里是{BANK_BRANCH}的预约服务，我是 AI 客服。请问，是您本人要预约办卡吗？"
BANK_CONFIRM = "我先问一下，这张卡是给您自己办的吗？回答“是”就行。"
BANK_CONFIRM_HELP = "想确认这张卡是不是给您自己办。如果是，直接回答“是”就可以了。"
BANK_CONFIRM_FALLBACK = "这句我还没确认清楚。您可以再说一声“是”，也可以在电话页打开文字输入框，输入“我是本人”。"
BANK_CHECKING = "稍等一下，我还在查这笔预约的结果。有结果我再跟您说，您也可以转人工。"
BANK_HANDOFF = "好的，稍后由人工客服与您对接"
BANK_GOODBYE = "好，那先到这儿。您有需要再联系，再见。"
BANK_TERMINAL_STATES = {"HANDOFF", "ENDING"}
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
    "alternative_document": "请带本人有效身份证件原件。您提到的证件或临时证明能否用于办卡，请向网点确认。",
    "applicant_eligibility": "年龄、是否需要监护人、外籍申请和代签等条件，请向网点确认。这里不能代为确认办理资格。",
    "branch": f"您到{BANK_BRANCH}就可以。进门后，可以先到服务台报一下预约编号。",
    "address": f"{BANK_BRANCH}是演示网点，这里没有提供真实的详细地址。",
    "other_branch": f"这条预约只对应{BANK_BRANCH}。其他网点的安排，可以帮您转人工咨询。",
    "review": "我可以先帮您约个到店时间。具体能不能办、要不要收费、什么时候领卡，得等网点现场看过材料再跟您说。",
    "card_terms": "是否有首存金额或其他办理条件，请向网点确认。这通电话只做预约，不要求存款、购买保险或转账。",
    "payment": "电话里不用交费，也不用转账。如有工本费或其他费用，请到网点现场核实。",
    "scope": "我这边只办理 Visa 借记卡的到店预约。信用卡、Mastercard 等其他卡可以请人工客服跟进。",
    "agent": "我是 AI 客服，可以帮您约到店办卡。想找人工的话，直接告诉我就行。",
    "privacy": "电话里不用报身份证号码，也不用提供银行卡号或验证码。",
    "contact_privacy": "预约手机号用来区分和核对您的预约。这个演示中，预约后台只显示尾号，通话记录会保留。关于保留多久或删除记录，可以请人工跟进。",
    "in_person": "这里可以预约本人到店办理。线上申请、邮寄和代办不在这项预约服务的范围内。",
    "additional_items": "目前能确认的是请带本人有效身份证件原件。手机、现金等其他物品是否需要，请向网点确认。",
    "handoff_help": "您明确说要转人工后，我会登记人工跟进，再结束这通电话。",
    "handoff_number": "人工跟进时可以核对您留的预约联系方式。这里无法确认实际拨出号码。",
    "handoff_timing": "本演示可以登记人工跟进，无法确认能否即时接通或具体回电时间。",
    "text_mode": "您可以点电话页的「打开文字输入」，接着输入，不会丢掉当前通话。",
    "weekend_hours": "周末没有这个预约服务的时段。网点周末是否营业，请向网点确认。",
    "branch_hours": "预约办理时段在工作日下午。网点实际营业和开门时间，请向网点确认。",
    "foreign_contact": "目前这个预约演示只接受 11 位中国大陆手机号。境外号码无法在这里登记，您可以先咨询办理流程。",
    "cancel_help": "有有效预约的话，可以取消。我会先跟您核对是哪一笔，再请您确认。",
    "walk_in": "我能帮您预约到店时段。直接到店能否办理、需不需要排队，请向网点确认。",
    "duration": "这边能确认预约时间。到店办理要多久、需不需要排队，得看现场安排和材料审核情况。",
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
    return re.sub(r"^(?:[嗯啊呃额哦噢哎]|唔(?![好使系係想要需要]))+", "", compact)


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
    words = re.sub(r"^(?:就)?按(?:您|你)?刚(?:才|刚)(?:说的|报的)", "", words)
    correction = re.split(r"(?:等等|等一下|说错了|更正|改选|我说的是)", words)
    if len(correction) > 1:
        words = correction[-1].lstrip("，,。… ")
    match = re.fullmatch(r"(?:那就|就|选|我选)*(最后|最早|最晚|头|前面|后面|前|后)"
                         r"的?(?:那|这|一)?个[吧啊呀]?", words)
    if not match:
        match = re.fullmatch(r"(最末|末尾|末)(?:的)?(?:那|这|一)?个[吧啊呀]?", words)
    if not match:
        return None
    position = match[1]
    if position in {"前面", "后面", "前", "后"} and len(offered_slots) > 2:
        return ""
    return offered_slots[-1] if position in {"最后", "最晚", "后面", "后", "最末", "末尾", "末"} else offered_slots[0]


def bank_contextual_agreement(text, action=None):
    """An affirmative reply to the exact proposal, with no condition or question."""
    if any(mark in text for mark in ("?", "？")):
        return False
    words = re.sub(r"^(?:喂|你好|您好)+", "", bank_words(text))
    words = re.sub(r"^(?:明白了|知道了|对)(?=确认)", "", words)
    words = re.sub(r"^(?:确认){2,}", "确认", words)
    if action and words in {"book": {"确认预约", "我确认预约"},
                            "change": {"确认改期", "我确认改期"},
                            "cancel": {"确认取消", "我确认取消", "确认取消预约"}}.get(action, ()):
        return True
    if action in {"book", "change"} and re.fullmatch(
            r"确认(?:(?:刚才|刚刚|这|那)(?:个|的)?(?:时间|时段)|这个具体日期时间和网点)"
            r"|(?:还是)?刚刚那个时间(?:给我|帮我)(?:约|办)吧?|就这么办", words):
        return True
    if action == "book" and re.fullmatch(r"(?:是的|对的?|好(?:的)?)?确认预约(?:这|那)个(?:时间|时段)", words):
        return True
    if action == "cancel":
        return bool(re.fullmatch(r"(?:对)?我(?:就是)?(?:要|想)把(?:这个|那个|这笔)预约取消掉?|"
                                 r"(?:是|对)(?:的)?别保留了", words))
    if action == "book" and re.fullmatch(r"(?:那就)?(?:你|您)(?:帮我)?(?:约|预约)吧", words):
        return True
    if action == "book" and re.fullmatch(r"(?:(?:可以|好(?:的)?|行)(?:那就)?|那就)?(?:请)?(?:帮我|给我)"
                                        r"(?:预约|约|订)(?:(?:这|那)个(?:时间|时段)|一下)(?:吧)?", words):
        return True
    if action == "change" and re.fullmatch(
            r"(?:可以|好|行|那就)(?:(?:帮我|给我)(?:改|改期)|(?:就)?麻烦(?:您|你)(?:这么|这样)改)(?:吧|一下)?", words):
        return True
    return bool(re.fullmatch(r"(?:好(?:的)?|行|可以)(?:就(?:用|按)?这个(?:时间|时段)(?:吧)?|"
                             r"麻烦(?:你|您)(?:了|一下)?|帮我定下来(?:吧)?)", words))


def bank_handoff_transcript(text, question):
    words = bank_words(text).strip("？?")
    words = re.sub(r"^(?:喂|你好|您好)+", "", words)
    if re.fullmatch(r"(?:法|反|换)人工(?:客服)?[吧啊呀]?", words):
        return question in {"handoff", "time_or_handoff", "phone"}
    return bool(re.fullmatch(
        r"(?:我(?:想|要|需要)|可不可以|可以|能不能|能否|能|麻烦(?:你|您)?|请)?(?:帮|给)?我?"
        r"(?:接(?:通)?|联系|换|找|转接?)?(?:个|一位)?(?:人工(?:客服|坐席)?|真人(?:客服|坐席)?)"
        r"(?:说话|聊聊|可以吗|行吗|吗|吧|一下)?", words))


def bank_topics(text, *, contextual=False):
    other_card = r"(?:信用卡|信貸卡|万事达|萬事達|mastercard|银联(?:卡|借记卡)|(?<![a-z])credit\s*card(?![a-z]))"
    debit_card = r"(?:借记卡|借記卡|储蓄卡|儲蓄卡|(?<![a-z])debit\s*card(?![a-z]))"
    contrast = bool(re.search(debit_card, text, re.IGNORECASE) and re.search(
        rf"(?:不办|不需要|不要|不是(?:办)?|唔係|唔系|不想办)\s*(?:Visa\s*(?:的)?)?{other_card}", text, re.IGNORECASE))
    topics = []
    other_product = bool(re.search(other_card, text, re.IGNORECASE) and not contrast)
    if other_product or re.search(r"境外汇款|跨境汇款|办理汇款", text):
        topics.append("scope")
    if re.search(r"(?:手机号|预约号码).{0,15}(?:干什么用|用来干什么|拿来干什么|有啥用)", text) or contextual and re.search(
                                r"(?:为什?么|为啥|用途).{0,10}(?:手机号|电话|号码)|"
                                r"(?:手机号|电话|号码|个人信息|资料|通话记录).{0,12}(?:拿来|用来|干嘛|怎么用|保留|保存|删除|多久|隐私|给谁|泄露)|"
                                r"(?:删除|保存|保留).{0,8}(?:手机号|电话|号码|资料|记录)|"
                                r"录音|营销|完整(?:手机号|号码)|(?:谁能看到|第三方).{0,12}(?:手机号|手机号码|电话|号码)|"
                                r"(?:手机号|手机号码|电话|号码).{0,14}(?:谁能看|第三方|末四位)", text):
        topics.append("contact_privacy")
    if re.search(r"身份证号|证件号|银行卡号|验证码", text):
        topics.append("privacy")
    if "护照" in text:
        topics.append("document_type")
    if re.search(r"驾照|驾驶证|回乡证|台胞证|临时身份证|临时证明|电子身份证|身份证.{0,12}(?:到期|过期)", text):
        topics.append("alternative_document")
    if re.search(r"未成年|监护人|外国人|外籍|代签|替我签|小孩.{0,8}\d{1,2}岁", text):
        topics.append("applicant_eligibility")
    if (re.search(r"带(?:什么|哪些|啥)|携带|材料|证件(?!号)|身份证(?!号)", text)
            or contextual and re.search(r"(?:带|准备)(?:点|些|一下|点儿|哪些)?(?:什么|啥|哪些)(?:东西|资料)?", text)):
        topics.append("documents")
    if re.search(r"(?:还|另)(?:需不需要|需要|要不要|要).{0,7}(?:手机|现金|银行卡|复印件)|"
                 r"复印件.{0,5}(?:吗|么)|(?:需要不要|需不需要|要不要).{0,5}(?:现金|手机)|"
                 r"居住证明|工作证明|银行流水|近.{0,4}个月的流水", text):
        topics.append("additional_items")
    if re.search(
            r"(?:在线|网上|手机(?:上)?|APP).{0,6}(?:申请|办理|办卡|开户)|邮寄|寄给我|(?:家属|家人|儿子|女儿|亲戚).{0,4}替我|"
            r"(?:能不能|能否|是否|可不可以|可以|怎么).{0,7}(?:代办|替办)", text, re.IGNORECASE):
        topics.append("in_person")
    if re.search(r"(?:境外|国外|香港|澳门|台湾|美国|加拿大|非大陆).{0,5}(?:号码|手机号)|"
                                r"(?:没有|不用|不支持)大陆.{0,3}(?:号码|手机号)|"
                                r"\+(?!86)\d(?:[\s().-]*\d){7,14}", text):
        topics.append("foreign_contact")
    if contextual and re.search(r"(?:改用|切换到?|怎么用|想用|能用|可以用|如何用).{0,5}(?:文字|打字|文本)|文字交谈", text):
        topics.append("text_mode")
    branch_name = re.findall(r"海棠银行([\u3400-\u9fff]{2,8})支行", text)
    if any(name != "科创" for name in branch_name) or re.search(
            r"其他网点|别的网点|其他支行|别家银行|最近的(?:网点|支行)|换.{0,10}(?:另一家|别家)(?:网点|支行)", text):
        topics.append("other_branch")
    elif re.search(r"门牌|详细地址|具体地址|地址.{0,4}(?:在哪|哪里|哪儿)|"
                   r"(?:网点|支行).{0,4}(?:哪条路|什么路|哪个路口)", text):
        topics.append("address")
    elif re.search(r"地址|在哪里|在哪儿|哪(?:一)?家(?:网点|支行)|哪个网点|怎么走|柜台怎么说", text):
        topics.append("branch")
    if (re.search(r"发卡|领卡|当天能领|拿卡|出卡|收费|年费|工本费|手续费|要不要钱|多少钱|能办下来|把卡拿走|能保证.{0,10}办成", text)
            or contextual and re.search(r"年费|拿到卡|拿到这张卡|当场办下来|现场办下来", text)):
        topics.append("review")
    if re.search(r"先存(?:一笔|点|多少)|首存|最低(?:金额|存款)|买.{0,8}保险|搭售", text):
        topics.append("card_terms")
    if re.search(r"(?:电话里|现在).{0,6}(?:交|付)|(?:交费|付款).{0,8}(?:柜台|电话)", text):
        topics.append("payment")
    if re.search(r"你(?:是|到底是)(?:真人|人工|AI|机器人)|你是人吗", text, re.IGNORECASE):
        topics.append("agent")
    if re.search(r"(?:能不能|能否|可以|怎么|如何)取消|取消(?:预约)?(?:可以吗|怎么弄|怎么操作)|取消.{0,7}(?:该怎么|如何)", text):
        topics.append("cancel_help")
    if re.search(r"不预约.{0,8}(?:去|到店)|直接到店|直接过去|现场排队", text):
        topics.append("walk_in")
    if re.search(r"要多久|得多久|排多久|等多久|花多长时间|办卡.{0,8}(?:几分钟|多长时间)|"
                 r"(?:走|插)队|(?:不用|要不要|需不需要)排队", text):
        topics.append("duration")
    if re.search(r"(?:周末|周六|周日|星期六|星期日).{0,6}(?:营业|开门|上班)", text):
        topics.append("weekend_hours")
    elif re.search(r"(?:支行|网点).{0,12}(?:开门|营业时间)|几点(?:开门|关门)", text):
        topics.append("branch_hours")
    return tuple(topics)


def bank_detour(text):
    if re.search(r"办卡|借记卡|信用卡|身份证|预约号|预约编号|到店预约|我的预约|(?:哪家|哪个)支行|本人|改期|可约时段|"
                 r"取消预约(?=[吗？?，。！!]|$)|(?:约|订|选|改成|改到|换成|换到)第[一二三四1234]", text):
        return None
    if re.search(r"[，,。；;？?]\s*(?:我想|我要|帮我|麻烦).{0,5}(?:预约|改|取消)|"
                 r"[，,。；;？?]\s*(?:改成|改到|换成|换到|选第|(?:还)?能约)", text):
        return None
    return next((reply for pattern, reply in DETOURS if re.search(pattern, text, re.IGNORECASE)), None)


def bank_cancellation(words, *, contextual=False):
    if re.search(r"取消(?:成功|结果|好了|了没|了没有|了吗)|(?:是不是|有没有|是否)(?:已经)?取消|撤销(?:成功|了吗)", words):
        return "cancel_status"
    if re.search(r"(?:不(?:太)?想|不打算|不准备|没想|不要|不用|不必|先不|暂不|别|不需要|不)取消|保留.{0,5}预约|还是保留原来", words):
        return "discard"
    if contextual and re.search(r"别(?:撤|取消)|(?:原来|原先).{0,6}(?:留住|保留|不动)|不换了|不(?:用|想)改", words):
        return "discard"
    if contextual and re.search(r"改期(?:先)?不用了", words):
        return "discard"
    if not re.search(r"[？?吗么]$", words) and re.fullmatch(
            r"(?:我)?(?:确认|确定|同意|对(?:的)?|就)(?:要|帮我|把它|那就)?取消(?:掉|预约|这笔预约)?(?:吧|了)?(?:[aA][0-9]+)?", words):
        return "confirm"
    if re.search(r"(?:能不能|能否|怎么|如何)取消|取消(?:预约)?(?:可以吗|怎么弄|怎么操作)|(?:只是(?:想)?问|只想问).{0,8}取消", words):
        return None
    if re.search(r"取消.{0,5}(?:刚才|刚刚)?(?:选的|挑的|新选的|新时段|改期申请)|(?:刚才选的|新选的).{0,5}(?:取消|不要)", words):
        return "discard"
    if contextual and re.search(r"(?:取消|撤销|撤掉)(?:这条|那条|这单|那单|这个|那个|我(?:的)?原来(?:的)?|原来(?:的)?)(?:到店)?预约|"
                                r"(?:那个|这个|这笔|那笔|我的)预约.{0,8}(?:去掉|撤了|删掉)", words):
        return "cancel"
    if contextual and re.fullmatch(r"(?:(?:你|您)?先)?(?:帮我|给我)办(?:理|一下)?取消(?:预约)?吧?", words):
        return "cancel"
    if re.search(r"(?:预约|这笔|那笔).{0,7}(?:取消|撤掉|撤销|退掉)|(?:取消|撤销|撤掉)(?:这笔|那笔|这个|那个|我的|原来的)?预约", words):
        return "cancel"
    if re.fullmatch(r"(?:(?:请|你|您|麻烦你?|麻烦您?)?帮我|我要|我想|请)?取消(?:掉|语|约|预|一约)?(?:吧|啊|呀)?[？?]?", words):
        return "cancel"
    return None


def bank_intent(text, available=(), active_slot=None, *, today=None, ordinal_slots=None, contextual_knowledge=False,
                schedule_recovery=False):
    action, target = parse(text, available)
    words = bank_words(text)
    topics = bank_topics(text, contextual=contextual_knowledge)
    if "documents" in topics and re.search(r"(?:我)?(?:只是|只想)(?:替|帮).{0,6}(?:问|咨询)|(?:我)?只想问.{0,6}(?:材料|证件)", words):
        return "documents_only", topics
    if action == "refuse":
        return action, target
    schedule_inquiry = bool((has_calendar_day(text) or CLOCK.search(text)) and re.search(
        r"哪些(?:时段|档)|分别有(?:哪|什么)|能不能办[？?]?$|比一下|哪天(?:更)?空|只(?:想|是)(?:查|问|看看)|"
        + (r"先看看|先帮我看看|先查|查(?:查|一下)|只是想知道|只是问|想知道|有没有(?:号|档)|还有(?:什么|哪些|哪几|号)|"
           r"哪些号|空档|空位|比较|不先约|先别(?:给我)?约|别订" if schedule_recovery else r"只(?:想|是)(?:查|问)"),
        words)
        and not re.search(r"(?:帮我|请|给我)(?:约|预约|订).{1,12}(?:最早|最晚)", words))
    if topics == ("scope",):
        return "bank_faq", topics
    if contextual_knowledge and re.search(r"旅游签证|出国签证|申请签证", words):
        return "bank_faq", ("scope",)
    if re.search(r"(?:转人工|人工客服|回电话|回拨).{0,10}(?:大概|需要|要)?(?:多久|多长时间|什么时候|何时)"
                 r".{0,8}(?:接上|接通|能接|联系|回拨)?|(?:暂时|先|现在)不转(?:人工)?[。！!？?]*$", words):
        return "bank_faq", (*topics, "handoff_timing")
    if re.search(r"(?:人工|客服).{0,5}(?:回拨|回电|打过来).{0,12}(?:哪个|什么|哪一个).{0,5}(?:号码|电话)", words):
        return "bank_faq", (*topics, "handoff_number")
    if re.search(r"(?:客服|人工).{0,10}(?:有(?:无)?|多少|哪里找|哪儿找).{0,8}(?:直拨|联系电话|电话号码|电话|联系方式)|"
                 r"(?:客服电话|人工热线|人工电话).{0,8}(?:多少|怎么找|在哪|哪儿)", words):
        return "bank_hotline", None
    if re.search(r"(?:删除|清除|清理|删掉).{0,10}(?:录音|转写|通话记录)|(?:录音|转写|通话记录).{0,8}(?:删掉|删除)", words):
        return "bank_record_removal", None
    if re.search(r"(?:别|不要|不用|不允许|不同意).{0,20}(?:推销|营销|广告)", words):
        return "bank_marketing_optout", None
    last_clause = re.split(r"[，,。！!；;…]+", text.strip().strip("。！!"))[-1].strip()
    if re.search(r"(?:客服|同事|朋友|对方)(?:之前)?说.{0,14}(?:转人工|回电|回电话)", words) and re.search(
            r"(?:我|现在)(?:先|现在)?(?:不用|不想|不要)|什么意思|怎么操作|怎么回事", words):
        return "bank_faq", (*topics, "handoff_help")
    if has_calendar_day(text) and re.search(r"(?:没(?:有)?(?:号|空位)|约不上).{0,8}(?:转人工|人工)|"
                                           r"(?:有号|可约).{0,14}(?:如果|要是)?没有就转人工", words):
        return "fallback_handoff", None
    if re.match(r"(?:那|另外|最后|改主意了|改口|说反了|还是)*(?:请|麻烦)(?:给我|帮我)?(?:直接)?转人工", last_clause):
        return "handoff", None
    if re.search(r"(?:改|取消|撤|查|核对).{0,15}(?:朋友|同事|家属|别人的|他人).{0,5}(?:预约|订单)|"
                 r"(?:朋友|同事|家属|别人的|他人).{0,5}(?:预约|订单).{0,12}(?:改|取消|撤|查|核对)", words):
        return "third_party_appointment", None
    quoted_ending = re.search(r"(?:之前|上一通|那个)?(?:客服|同事|朋友|对方)说(?:了)?.{0,5}(?:挂断|挂电话|再见|拜拜)", words)
    if not quoted_ending and re.search(
            r"(?:^|[，,。！!；;])\s*(?:好(?:的)?[，,])?(?:那|我|咱(?:们)?|请(?:你|您)?)?(?:就|先)?"
            r"(?:挂(?:断)?(?:电话)?(?:了|啦|吧)?|拜拜|再见|回头再聊|今天就到这[儿里]|别(?:再)?给我打(?:电话)?了)"
            r"(?:$|[，,。！!；;])", text.strip()):
        requested_handoff = re.search(r"(?:^|[，,。；;])\s*(?:(?:麻烦|请)(?:给我|帮我)?|帮我|给我)?"
                                      r"(?:直接)?转人工(?:吧|一下)?(?=$|[，,。；;])", text)
        if requested_handoff and not re.search(r"(?:别|不|不要|不用)(?:再|先)?转人工", words):
            return "handoff", None
        return "end_call", None
    if schedule_recovery:
        if re.search(r"班表|排班|换班", words) and re.search(r"还没|尚未|不确定|才有空|先等|可能", words):
            return "tentative_schedule", None
        if re.search(r"(?:转人工|回电|回电话|打电话).{0,12}(?:直接接通|还是等|等多久|什么时候|多久|得多长时间)|"
                     r"(?:只有|除非).{0,25}(?:回电话|回电).{0,10}(?:才同意|才会|才要)", words):
            return "bank_faq", (*topics, "handoff_help", "handoff_timing")
        if re.search(r"(?:如需|如果|假如|要是).{0,10}(?:转人工|回电).{0,18}(?:哪个|哪一个|打到|拨给|什么号码|给谁)", words):
            return "bank_faq", (*topics, "handoff_number")
        revised_callback = re.search(r"(?:说错了|改主意了).{0,6}(?:还是|请|就让).{0,8}(?:人员|客服).{0,8}(?:回电|回电话)", words)
        if revised_callback and not re.search(r"(?:再别|不要|不用).{0,6}(?:回电|打电话)", words[revised_callback.end():]):
            return "handoff", None
    if re.search(r"(?:不要|不用|别|不必|唔好|唔使|唔要|noneedto)(?:现在|先|立刻|立即|马上)?(?:帮我|给我)?转人工|"
                     r"先别(?:帮我|给我)?(?:接|找|转)人工|"
                     r"(?:仅|只)(?:是|想)?(?:知道|了解|问).{0,10}(?:转接|转人工).{0,5}(?:流程|步骤|方式)|"
                     r"如果.{0,24}(?:再决定|再考虑|再转).{0,8}人工|"
                     r"(?:只有|除非).{0,26}(?:才会|才同意|才要).{0,8}(?:转|回电)|"
                     r"(?:回电|回电话|打电话).{0,10}(?:等多久|多久|什么时候)|"
                     r"(?:不要|别|不用)(?:再)?(?:给我)?(?:回电|打电话)|不想让.{0,8}(?:回电|回电话|打电话)", words, re.IGNORECASE):
        return "bank_faq", (*topics, "handoff_help")
    if not re.search(r"(?:别|不要|不用|不想)(?:再|先|帮我)?(?:查|核对)", words) and re.search(
            r"(?:到底|现在|目前|一共)?(?:有|约了|办了)(?:几|多少)(?:笔|个|条|次)(?:预约|订单)|"
            r"(?:只|仅)(?:是|想)?(?:看|知道|查|核对|问).{0,14}(?:系统[里的中]*|刚才|刚刚|上[一单次回]|原来).{0,10}(?:结果|记录|预约)|"
            r"(?:只|仅)(?:是|想)?(?:核对|查|看|问)(?:一下)?(?:预约|改期|取消)(?:的)?结果|"
            r"(?:刚才|刚刚|上次|上回).{0,10}(?:约上|约好|约成|预约成功|办成|定下来)(?:了?吗|了?没|没有|了?[？?]|$)|"
            r"(?:刚才|刚刚|刚|上次).{0,10}(?:取消|撤销|撤掉|撤)(?:成功|掉|好了?)(?:了)?(?:没|没有|吗|么|[？?])|"
            r"(?:只|仅)(?:是|想)?(?:查|核对|问).{0,12}(?:有没有|是否).{0,5}(?:约成功|预约成功|提交成功)|"
            r"(?:订没订上|约没约上|到底有没有订上)|(?:看|查|核实).{0,12}(?:回执|办理结果).{0,8}(?:没|没有|吗|么)", words):
        return "appointment_result", None
    if re.search(r"(?:另外|再|加)(?:给我)?(?:新)?(?:约|预约|订)(?:一|1)(?:单|笔|个)|"
                 r"(?:保留|别动|不动).{0,10}(?:旧单|原(?:来|先)的预约).{0,18}(?:另|再).{0,6}(?:约|订)|"
                 r"(?:原预约|旧单).{0,5}(?:照旧|不动|保留).{0,20}(?:再|另外).{0,5}(?:占|约|订)|"
                 r"(?:另外|另|再)(?:帮我|给我)?(?:留|占)(?:一个|个)?.{0,20}(?:位置|位子|号|时段|档).{0,8}"
                 r"(?:原先|原来|旧单|那笔).{0,8}(?:保留|留着|照旧|不动)", words):
        return "extra_booking", None
    if (not schedule_inquiry and re.search(
            r"还没(?:想好|决定|定)|没想好|没拿定主意|得再问|得问下|得问一下", words)) or re.search(
                 r"不用(?:这个|那个)|[啊哦呃嗯]?不[，,]?(?:先)?留着|"
                 r"留(?:着)?(?:原先|原来|旧单)|^原先那个$|"
                 r"(?:先)?(?:别|不要|不用)(?:再)?(?:给我|帮我)?(?:撤(?:掉|销)?|取消)(?:原来的?|原先的?)?.{0,6}"
                 r"(?:留着|保留|照旧)|^(?:先)?别(?:撤|取消)(?:了|吧)?$|"
                 r"(?:旧单|原预约).{0,6}(?:原封不动|照旧)|别(?:把|给我)?(?:之前的|原来的)?改了|"
                 r"(?:撤掉|撤回|撤销).{0,9}(?:改期申请|改期请求)|"
                 r"留(?:着)?(?:周|星期|礼拜)[一二三四五六日天].{0,12}别动", words):
        return "discard", None
    if re.search(r"(?:人工|真人).{0,10}(?:怎么|咋|如何)(?:接|转|联系)|"
                 r"(?:怎么|咋|如何).{0,5}(?:接|转|联系)(?:人工|真人)", words):
        return "bank_faq", (*topics, "handoff_help")
    if not has_calendar_day(text) and re.search(r"声音断了|没听清|没听到|没听明白|慢一点再说|再说一遍|说慢一点", words) and re.search(
            r"刚才|最后|刚刚|声音断了|说慢一点|再说一遍", words):
        return "repeat_offer_only", None
    if re.search(r"(?:不太合适|时间不行|时间不方便|那天不行).{0,14}(?:之前|原来|刚才).{0,10}(?:哪天|几号|几点)", words):
        return "decline_recap_only", None
    if re.search(r"(?:我)?(?:只)?(?:想)?(?:查|核对).{0,6}(?:原预约|原来(?:的)?预约|我(?:的)?预约)(?:的)?(?:时间|日期|网点)", words):
        return "appointment_result", None
    if re.search(r"^(?:不用|不要|先别)(?:转人工|转)(?:了)?(?:我)?(?:还是|就)?(?:自己选|自己约|直接说)", words) and has_calendar_day(text):
        remainder = re.split(r"自己选|自己约|直接说", text, maxsplit=1)[-1]
        return bank_intent(remainder, available, active_slot, today=today, ordinal_slots=ordinal_slots,
                           contextual_knowledge=contextual_knowledge, schedule_recovery=schedule_recovery)
    if re.search(r"(?:周|星期|礼拜)几(?:来着|来著)|(?:刚才|刚刚).{0,10}(?:周|星期|礼拜)几", words):
        return "recap_time_only", None
    if re.search(r"(?:原来|原先|之前).{0,12}(?:时间|时段).{0,6}(?:还能|能不能|可不可以|能否).{0,5}(?:约回|再约|重新约)", words):
        return "previous_slot_availability", None
    if schedule_recovery:
        old_time, separator, correction = re.sub(r"\s+", "", text).partition("改成")
        if separator and re.search(r"不要|别(?:订|约)|不选|说错", old_time) and has_calendar_day(correction) and CLOCK.search(correction):
            return bank_intent(correction, available, active_slot, today=today, ordinal_slots=ordinal_slots,
                               contextual_knowledge=contextual_knowledge, schedule_recovery=schedule_recovery)
        if re.search(r"(?:^|[，,。])\s*(?:我)?(?:先|现在)?不(?:想)?约了(?=$|[，,。！!])", text):
            return "discard", None
        if not schedule_inquiry and re.search(r"别(?:先)?(?:帮我|给我)?(?:提交|预约|预订|订|约)|先别(?:办|改|动)(?:啊|吧)?$|(?:免了|不用办了|不约了)$|"
                     r"(?:暂时|先|就)(?:只是|只)?(?:看看|看一下)(?:$|了)|没说要(?:约|订)|"
                     r"也许(?:行|能去).{0,10}(?:得问|再问|再看看)|现在接不了|明确拒绝授权|"
                     r"(?:先)?等等.{0,14}(?:再跟|再和|再告诉)|没说要改|唔好(?:约|約|改|提交)|唔使(?:约|約|改)|"
                     r"^(?:no(?:thanks|谢谢)?|notnow|holdon)$|^不想(?:约|预约|办理)了",
                     words, re.IGNORECASE):
            return "discard", None
        if topics and re.search(r"^先不要|^先等等|^等会儿|^等一下", words):
            return "discard", None
        if re.search(r"(?:那就|那)?(?:赶紧|快|马上)取消$", words):
            return "cancel", None
        if re.search(r"(?:只|只是|仅)(?:想)?(?:查|核对|确认|问).{0,12}预约.{0,8}(?:成功|结果)|"
                     r"预约.{0,8}(?:日期|网点).{0,8}(?:再念|再说|再读)|(?:那个|刚才的)?号多少(?:来着)?$|"
                     r"(?:预约|我这个号|我记的是|我原来).{0,18}(?:记在几号|礼拜几|星期几|现在还要去|帮我查查.{0,4}不用改)|"
                     r"查.{0,8}(?:我的|原来的)?预约时间|按(?:我的|原来)预约(?:的)?时间", words):
            return "appointment_query", None
    if re.search(r"(?:不用|不要|不想|不需要|不是要|别|不)(?:再|先)?(?:帮我|给我)?(?:转|找|接)?(?:人工|真人)", words):
        return "hold", None
    if schedule_recovery and re.search(
            r"(?:柜台|银行|网点)?(?:工作人员|客服|专员).{0,10}(?:(?:给我|帮我)(?:打|回).{0,4}电话|回拨(?:手机|号码)?)|"
            r"(?:登记|请|帮我).{0,6}人工回拨|"
            r"(?:真人|人工).{0,4}(?:接一下|接听|来接)|(?:那边|你们).{0,8}(?:来个|找个)人.{0,10}(?:说|聊)", words):
        return "handoff", None
    if action == "handoff" or re.search(
            r"(?:跟|和|找|转|接|叫|要|请)(?:个)?(?:人工客服|真人)|"
            r"(?:人工客服|真人客服).{0,5}(?:跟我联系|联系我|回电)", words):
        return "handoff", None
    declined = DECLINED_TIMES.search(words)
    if declined and not (schedule_recovery and re.search(
            r"预约.{0,8}(?:去掉|撤了|删掉)|下(?:个)?(?:周|礼拜|星期)[一二三四五]", words)):
        return "decline_times", words[declined.end():]
    detour = bank_detour(text)
    if detour:
        return "off_topic", detour
    if action == "continue":
        return action, target
    if schedule_recovery and re.search(r"取消.{0,10}(?:手续|步骤|流程|之后|以后).{0,8}(?:什么|怎么|能|可以)|"
                                       r"(?:能不能|能否|能|可以|可不可以)取消.{0,10}(?:吗|么|[？?])", words):
        return "bank_faq", (*topics, "cancel_help") if "cancel_help" not in topics else topics
    if schedule_recovery:
        clauses = [bank_words(part) for part in re.split(r"[，,。；;！？?]", text) if part]
        if len(clauses) > 1:
            latest = clauses[-1]
            if re.search(r"(?:不|别|不用)(?:再|先)?取消", latest):
                return "discard", None
            if re.search(r"改期.{0,6}(?:不用|不要|算了)|(?:不|别)(?:用)?(?:再)?改", clauses[0]) and (
                    bank_cancellation(latest, contextual=True) == "cancel"):
                return "cancel", None
            if bank_cancellation(clauses[0], contextual=True) == "discard" and re.search(
                    r"改到|改成|换到|取消.{0,6}预约", latest):
                return bank_intent(latest, available, active_slot, today=today, ordinal_slots=ordinal_slots,
                                   contextual_knowledge=contextual_knowledge, schedule_recovery=schedule_recovery)
    cancellation = bank_cancellation(words, contextual=schedule_recovery)
    if cancellation:
        return cancellation, None
    if re.search(r"(?:恢复|重新约回|约回)(?:到)?(?:原来|原先|之前|刚才|原预约)|(?:重新约|再约)(?:回)?(?:原来|原先|之前).{0,5}(?:时间|时段)", words):
        return "rebook_previous", None
    if action == "discard":
        return action, target
    if schedule_recovery and not schedule_inquiry and re.search(
            r"(?:(?:先|暂时|现在|今天|这次)(?:就)?(?:不约|不订|不预约)(?:了|吧|了吧)?"
            r"(?:今天就到这[儿里](?:吧|了)?)?|(?:先)?别(?:帮我|给我)?(?:约|订|提交)(?:了|吧)?|"
            r"(?:先|就)挂(?:了|吧)|想好再打(?:拜拜|再见)?)"
            r"[。！!]*$", words):
        return "discard", None
    if re.fullmatch(r"我?(?:现在)?不想办了(?:吧)?", words):
        return "stop_request", None
    if not schedule_inquiry and re.search(r"别(?:约|订|改)(?:了|了吧|$)|不要了.*不订|不订了|不办了|不取消|别给我改|不用改|先不用了", words):
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
    if topics and topics != ("documents",):
        return "bank_faq", topics
    if action == "cancel" or re.search(r"(?:预约|那笔|那个).{0,6}撤掉|(?:帮我|我要)撤销预约", words):
        return "cancel", None
    if re.search(r"(?<![帮给替请])我(?:有预约|(?:已经|之前|这次)?约[的了]|的预约)|预约号|预约编号", words):
        return "appointment_query", None
    if re.fullmatch(r"(?:还是)?(?:换|改)(?:另)?一个(?:吧)?[？?]?", words):
        return "change_request", None
    if re.search(r"刚才(?:说|报)的(?:都)?有哪些(?:可约的)?(?:时间|时段)", words):
        return "continue", None

    time_words = time_revision(text, contextual=schedule_recovery)
    if schedule_recovery and schedule_inquiry and re.search(r"只是备用|不能来|来不了", words) and re.search(
            r"(?:我想|我要|只能|只想)\s*(?:约|选|订)", time_words) and has_calendar_day(time_words) and CLOCK.search(time_words):
        schedule_inquiry = False
    if today is not None and date_weekday_conflict(time_words, today):
        return "calendar_conflict", None
    if schedule_recovery and today is not None and has_calendar_day(time_words) and inconsistent_clock_bounds(time_words):
        return "clock_conflict", None
    if schedule_recovery and today is not None and re.search(r"(?:头一|第一|最早一|最后一|最晚一)档", words):
        candidates = slot_matches(time_words, available, today)
        if candidates:
            slot = candidates[-1] if re.search(r"(?:最后|最晚)", words) else candidates[0]
            return ("ask_time", (slot,)) if AVAILABILITY_QUESTION.search(text) else ("choose", slot)
    if schedule_recovery and today is not None and has_calendar_day(time_words) and re.search(
            r"(?:选|约|挑|给我|是|还有|哪(?:个|天)|查一下).{0,12}最(?:早|晚)|最(?:早|晚).{0,6}哪天", words):
        candidates = slot_matches(time_words, available, today)
        if candidates:
            selected = candidates[-1] if "最晚" in words else candidates[0]
            return ("lookup_time", (selected,)) if schedule_inquiry else ("choose", selected)
        return "ask_time", ()
    if schedule_recovery and "最晚" in words and re.search(r"哪天还没定|还没定哪天|日期还没定", words):
        return "clarify_choice", None
    if schedule_recovery and today is not None:
        fallback = re.split(r"[，,](?:(?:如果|要是)(?:那个|那天|这个)?(?:没(?:有)?(?:空|位置|位子|号)|约不上)再|"
                            r"(?:满了|没号)就)(?:约|选)", time_words, maxsplit=1)
        if len(fallback) == 2:
            clock = CLOCK.search(fallback[0])
            if clock and has_calendar_day(fallback[0]) and not has_calendar_day(fallback[1]):
                fallback[1] = fallback[0][:clock.start()] + fallback[1]
            for alternative in fallback:
                candidates = slot_matches(alternative, available, today)
                if candidates and len(candidates) == 1:
                    return "choose", candidates[0]
        if re.search(r"(?:都(?:能来|可以|有空)|都行).{0,10}(?:帮|给)我(?:约|挑|选).{0,4}[早晚]", words):
            options = re.split(r"或者|或", time_words)
            clock = CLOCK.search(options[0])
            if len(options) == 2 and clock and has_calendar_day(options[0]):
                if not has_calendar_day(options[1]):
                    options[1] = options[0][:clock.start()] + options[1]
                possible = {slot for option in options for slot in (slot_matches(option, available, today) or ())}
                found = [slot["id"] for slot in available if slot["id"] in possible]
                if found:
                    preference = re.search(r"(?:约|挑|选).{0,3}([早晚])", words)
                    return "choose", found[-1 if preference and preference[1] == "晚" else 0]
    read_only = bool(schedule_inquiry or schedule_recovery and re.search(
        r"只想查询|只是查|只想查|原预约不变|先看看|帮我(?:看看|查查)|有没有号|查询一下|查下空位|只查",
        words) and not re.search(r"帮我约|请预约|给我(?:约|订)|确认预约", words))
    choose_later = schedule_recovery and bool(re.search(r"都行|都可以|都能来|都方便", words))
    if (read_only or choose_later) and today is not None:
        options = re.split(r"或者|或|和|跟|、", time_words)
        if len(options) > 1 and any(has_calendar_day(option) for option in options):
            clock = CLOCK.search(options[0])
            day_context = options[0][:clock.start()] if clock and has_calendar_day(options[0]) else ""
            options = [option if has_calendar_day(option) or not CLOCK.search(option) else day_context + option
                       for option in options]
            shared_clock = re.search(r"(?:上午|下午|中午|晚上)?\s*[零一二两三四五六七八九十0-9]{1,3}点(?:半)?", options[-1])
            if shared_clock:
                options = [option if re.search(r"点|[:：][0-9]", option) else option + shared_clock[0]
                           for option in options]
            if all(has_calendar_day(option) for option in options):
                matches = tuple(slot for option in options for slot in (slot_matches(option, available, today) or ()))
                return ("compare_times" if read_only else "calendar_choices"), tuple(dict.fromkeys(matches))
    if schedule_recovery and today is not None:
        nearest = split_nearest(time_words, available, today)
        if nearest is not None:
            return "lookup_time", nearest
    if re.search(r"或(?:者|是)?|还是", time_words):
        return "clarify_choice", None
    if re.search(r"让我想想|我再想想|考虑一下|^不确定$", words) or (
        re.match(r"^(?:稍等|等等|等一下|等会儿)", words) and not re.search(r"改|换|选|约|还有|几点|[0-9一二三四五六七八九十][号日]", words)
    ):
        return "hold", None

    lookup_only = bool(read_only or re.search(r"只是问|先问问|只想问|只问一下|不用.{0,5}(?:约|订)", words)
                       or schedule_recovery and re.search(
                           r"想知道.{0,6}(?:可以|能不能|能否)预约|并不是说.{0,10}(?:想|要)预约|只(?:是|想)?查(?:下|一下|询)|"
                           r"先别动|原来.{0,4}不动|打听|还有哪几档", words))
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

    asks_time = bool(AVAILABILITY_QUESTION.search(text))
    if topics and not asks_time:
        return "bank_faq", topics
    if schedule_recovery and not topics and asks_time and re.search(
            r"最晚几点(?:能到|可到|到|能约)?|(?:最靠后|最后一档|最晚那一档).{0,8}几点", words):
        return "latest_window", None
    if not topics and not has_calendar_day(time_words):
        offset = re.search(r"(早|晚|提前|推迟|延后|往后挪|往前挪)(?:个)?(半[个]?小时|半[个]?钟(?:头)?|一[个]?小时|三十分钟|30分钟|一刻钟)"
                           r"(?:可以吗|行吗|呢|吧|好吗|日子别动)?[。？?！!]*$", words)
        directive = re.sub(r"能不能|可不可以|行不行", "", words)
        if offset and not re.search(r"(?:不要|不想|不能|别).{0,4}(?:早|晚|提前|推迟|延后)", directive):
            minutes = 15 if offset[2] == "一刻钟" else 60 if offset[2].startswith("一") else 30
            return "offset_time", -minutes if offset[1] in {"早", "提前", "往前挪"} else minutes
    relative = re.search(r"([早晚])(?:一)?点(?:儿)?|([早晚])一些", words)
    if relative and not topics and not (schedule_recovery and has_calendar_day(time_words)):
        return "relative_time", -1 if "早" in relative[0] else 1
    if today is not None and not re.search(r"不要|不选|不约", time_words):
        contrast = re.split(r"(?<!行)不行|太早|太晚|(?<!方)不方便", time_words)
        if ambiguous_time(contrast[-1]):
            return "clarify_choice", None
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
