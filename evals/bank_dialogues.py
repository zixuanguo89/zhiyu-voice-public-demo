"""Replay realistic bank conversations through the same HTTP API as the workbench."""

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from fastapi.testclient import TestClient

from services.api import create_app
from services.business import ZONE
from services.config import Settings


@dataclass(frozen=True)
class Turn:
    text: str
    contains: tuple[str, ...] = ()
    offer: str | None = None
    write: str | None = None
    verified: bool = True


def t(text, *contains, offer=None, write=None, verified=True):
    return Turn(text, contains, offer, write, verified)


START = t("您好，我是本人，我想办一张 Visa 借记卡。", "哪个方便")
SCENARIOS = {
    "01 真实录音回放与按月日询价": (
        t("你好你好，我要办一张没在卡。", "给您自己办", verified=False),
        t("嗯，试过这一半。对对。", "改用文字交谈", verified=False),
        t("嗯，是是。", "哪个方便"),
        t("呃，这个月11号可以吗？", "11月11号下午4点", offer="S3"),
        t("那天是几点来着？", "11月11号下午4点", offer="S3"),
        t("嗯可以可以可以。", "预约成功", write="book:S3"),
        t("帮我查查我约的是几点。", "11月11号下午4点", "A101"),
        t("谢谢您。", "不客气"),
    ),
    "02 明后天与换成另一个": (
        START, t("明天还有位置吗？", "11月9号上午9点", offer="S1"),
        t("等一下，我先看看日历。", "您慢慢看", offer="S1"),
        t("后天下午有空吗？", "11月10号下午2点", offer="S2"),
        t("啊我看错了，还是约明天吧。", "11月9号上午9点", offer="S1"),
        t("可以，麻烦你帮我约。", "预约成功", write="book:S1"),
        t("明天几点到？", "您约的是11月9号上午9点"),
        t("我到哪家支行？", "海棠银行科创支行"),
    ),
    "03 中文日期与星期": (
        START, t("这个周二下午有号吗？", "11月10号下午2点", offer="S2"),
        t("十一月十一号几点能去？", "11月11号下午4点", offer="S3"),
        t("先别约了，我再看看。", "先不办"),
        t("周三下午四点可以吗？", "11月11号下午4点", offer="S3"),
        t("就这个时间，麻烦帮我约上。", "预约成功", write="book:S3"),
        t("我想确认一下我的预约时间。", "11月11号下午4点", "A101"),
        t("谢了。", "不客气"),
    ),
    "04 只报时间与筛选时段": (
        START, t("有上午的吗？", "11月9号上午9点", offer="S1"),
        t("我要下午的。", "11月10号下午2点", "11月11号下午4点"),
        t("下午两点的那个吧。", "11月10号下午2点", offer="S2"),
        t("我想问一下下午三点有没有。", "没有这个时段"),
        t("那就选下午四点。", "11月11号下午4点", offer="S3"),
        t("好，就它吧。", "预约成功", write="book:S3"),
        t("我约的是几点？", "11月11号下午4点"),
    ),
    "05 两个选项与拒绝误订": (
        START, t("9号还是10号，我还没想好。", "选定"),
        t("10号可以吗？", "11月10号下午2点", offer="S2"),
        t("我不确定，或者11号呢？", "先选定"),
        t("可以。", "想约哪一天"),
        t("不要10号，改到11号吧。", "11月11号下午4点", offer="S3"),
        t("可以吗？", "要我帮您约", offer="S3"),
        t("行，那就11号。", "11月11号下午4点", offer="S3"),
        t("好。", "预约成功", write="book:S3"),
    ),
    "06 材料地址与到店须知": (
        START, t("去的时候带啥？在哪家网点啊？", "身份证件原件", "科创支行"),
        t("我能拿护照代替身份证吗？", "请向网点确认"),
        t("证件的照片可以吗？", "原件"),
        t("预约办卡要不要钱？", "收费", "现场"),
        t("当天能把卡拿走吗？", "什么时候领卡", "现场"),
        t("去柜台怎么说？", "预约编号"),
        t("你们具体门牌号多少？", "演示", "详细地址"),
    ),
    "07 问材料后继续原来的预约": (
        START, t("选第二个吧。", "下午2点", offer="S2"),
        t("那天需要带什么呀？", "身份证件原件", offer="S2"),
        t("好。", "要我帮您约", offer="S2"),
        t("刚刚说的是几点？", "11月10号下午2点", offer="S2"),
        t("嗯。", "11月10号下午2点", offer="S2"),
        t("可以的，帮我办。", "预约成功", write="book:S2"),
        t("能告诉我预约编号吗？", "A101"),
    ),
    "08 预约后改期和取消": (
        START, t("第一个吧。", "上午9点", offer="S1"),
        t("可以。", "预约成功", write="book:S1"),
        t("改成十一号下午四点。", "改到这个时间", offer="S3"),
        t("没问题，就按这个来。", "改期成功", write="change:S3"),
        t("帮我把刚才的预约撤掉。", "确认取消", offer="cancel"),
        t("好的。", "确认取消", offer="cancel"),
        t("嗯，确认取消。", "已取消", write="cancel:S3"),
    ),
    "09 拒绝本次选择与重新预约": (
        START, t("想约第三个。", "下午4点", offer="S3"),
        t("不要了，不订了。", "先不办"),
        t("确定。", "有需要您再跟我说"),
        t("我有预约吗？", "还没约时间"),
        t("那约10号。", "下午2点", offer="S2"),
        t("行啊。", "预约成功", write="book:S2"),
        t("别给我改原来的。", "没有变动"),
    ),
    "10 不受理日期与重新定位": (
        START, t("这个月25号能约吗？", "没有这个时段"),
        t("11月12号还有空吗？", "没有这个时段"),
        t("本周末有空吗？", "没有这个时段"),
        t("下下周三还有号吗？", "没有这个时段"),
        t("那近期还有哪些时间？", "11月9号", "11月11号"),
        t("十号有号吗？", "下午2点", offer="S2"),
        t("可以。", "预约成功", write="book:S2"),
    ),
    "11 说明身份、隐私和转人工": (
        t("你是真人吗？", "AI 客服", verified=False),
        t("我替我妈来办。", "先不往下办", verified=False),
        t("要报身份证号码吗？", "不用报", verified=False),
        t("确认什么？", "给您自己办", verified=False),
        t("对方说，是本人。", "改用文字交谈", verified=False),
        t("嗯，是我自己来办卡。", "哪个方便"),
        t("谢谢。", "不客气"),
        t("我想跟真人说。", "好的，稍后由人工客服与您对接"),
    ),
    "12 卡种咨询和不实承诺": (
        START, t("能办 Visa 信用卡吗？", "Visa 借记卡", "人工"),
        t("可以申请万事达的卡吗？", "Visa 借记卡", "人工"),
        t("这卡保证能办下来吗？", "现场"),
        t("我可以直接在电话里办好发卡吗？", "到店时间", "现场"),
        t("那就只约办借记卡。", "哪个方便"),
        t("第2个时间几点？", "下午2点", offer="S2"),
        t("好，就按这个来。", "预约成功", write="book:S2"),
    ),
    "13 已约之后不重复提交": (
        START, t("十一月十一号下午四点。", "下午4点", offer="S3"),
        t("确认预约。", "预约成功", write="book:S3"),
        t("可以。", "还有什么想问"),
        t("11号几点来着？", "您约的是11月11号下午4点"),
        t("你再说一遍我的预约号。", "A101"),
        t("这笔预约能不能取消？", "可以取消"),
        t("现在不取消。", "没有变动"),
    ),
    "14 含糊回应、改口与人工": (
        START, t("第二项。", "下午2点", offer="S2"),
        t("不是这个，我想换个时间。", "想换到"),
        t("嗯，可以。", "想约哪一天"),
        t("哪个最早？", "11月9号上午9点", offer="S1"),
        t("我再想想。", "您慢慢看", offer="S1"),
        t("不确定。", "您慢慢看", offer="S1"),
        t("还是帮我转人工吧。", "好的，稍后由人工客服与您对接"),
    ),
    "15 问候、重复与一次问两件事": (
        START, t("你刚才说的有哪些时间？", "第1个", "第3个"),
        t("11月11号下午四点能约吗？到店带啥？", "身份证件原件", "下午4点", offer="S3"),
        t("那个时间再说一遍。", "下午4点", offer="S3"),
        t("我能在其他网点办吗？", "科创支行", "人工", offer="S3"),
        t("好，那个时间。　", "要我帮您约", offer="S3"),
        t("行，麻烦帮我订。", "预约成功", write="book:S3"),
        t("好嘞，谢谢。", "不客气"),
    ),
}

VARIATIONS = {
    "16 语音改口与自然否定": (
        START, t("九号的那个。", "11月9号上午9点", offer="S1"),
        t("九号不行，那十号呢？", "11月10号下午2点", offer="S2"),
        t("可以，但先别订。", "先不办"),
        t("还是换一个吧。", "换到"),
        t("那就改到十一号的四点。", "11月11号下午4点", offer="S3"),
        t("可以，麻烦您帮我约一下。", "预约成功", write="book:S3"),
        t("以后再取消，现在不用改。", "没有变动"),
    ),
    "17 只看下午再选第一个": (
        START, t("上午太早，有下午的吗？", "11月10号下午2点", "11月11号下午4点"),
        t("还得带身份证是吧？", "身份证件原件"),
        t("我就要刚才两个里面的第一个。", "11月10号下午2点", offer="S2"),
        t("呃，刚才那个能不能晚一点？", "11月11号下午4点", offer="S3"),
        t("再早一点呢？", "11月10号下午2点", offer="S2"),
        t("就按这个来吧。", "预约成功", write="book:S2"),
        t("我这次约的时间你再讲一下。", "11月10号下午2点"),
    ),
    "18 多个时间说法与有歧义的同意": (
        START, t("能不能晚点儿？", "比哪个时间"),
        t("后天14:00有空吗？", "11月10号下午2点", offer="S2"),
        t("听着行，不过我得再考虑一下。", "您慢慢看", offer="S2"),
        t("好。", "要我帮您约", offer="S2"),
        t("可以？", "要我帮您约", offer="S2"),
        t("嗯好嘞。", "预约成功", write="book:S2"),
        t("好嘞，谢谢您。", "不客气"),
    ),
    "19 不确定事项转人工": (
        t("我只是问问，要提供银行卡号吗？", "不用报", verified=False),
        t("给我自己办。", "哪个方便"),
        t("不预约可以直接过去吗？", "请向网点确认"),
        t("到店办卡要多久？", "现场排队"),
        t("当天能领吗，手续费多少？", "收费", "现场"),
        t("住址不用告诉你吧？你是人工吗？", "AI 客服"),
        t("谢了。", "不客气"),
        t("能帮我找个真人吗？", "好的，稍后由人工客服与您对接"),
    ),
    "20 日期冲突、同意之前又反悔": (
        START, t("九号上午还是十一号下午呢？", "先选定"),
        t("周三下午4点半能约吗？", "没有这个时段"),
        t("这周三下午4点可以吗？", "没有这个时段"),
        t("下周三下午四点。", "11月11号下午4点", offer="S3"),
        t("刚才那天不方便。", "想换到"),
        t("大后天几点有号？", "11月11号下午4点", offer="S3"),
        t("确认预约。", "预约成功", write="book:S3"),
    ),
    "21 新预约和取消权限分开": (
        START, t("确认取消。", "先说想办什么"),
        t("给我安排个最早的。", "11月9号上午9点", offer="S1"),
        t("好，那就帮我约吧。", "预约成功", write="book:S1"),
        t("把我的预约撤掉。", "确认取消", offer="cancel"),
        t("行吧。", "确认取消", offer="cancel"),
        t("我说错了，先不取消。", "没有变动"),
        t("11月9号几点过去？", "您约的是11月9号上午9点"),
    ),
}

MONTH_END = {
    "22 跨月后的日期指代": (
        START, t("这个月1号还有吗？", "没有这个时段"),
        t("下个月1号下午有空吗？", "12月1号下午2点", offer="S2"),
        t("等会儿，我想想。", "慢慢看", offer="S2"),
        t("那本月30号呢？", "11月30号上午9点", offer="S1"),
        t("还是约12月2号下午4点吧。", "12月2号下午4点", offer="S3"),
        t("行，就这天。", "预约成功", write="book:S3"),
        t("后面我约了什么时候？", "12月2号下午4点", "A101"),
    ),
}

FRESH = {
    "23 意思反转、重述和收尾": (
        t("早，我是预约人，我要办储蓄卡。", "哪个方便"),
        t("就选第二项，不对，我改成第三项吧。", "11月11号下午4点", offer="S3"),
        t("等等，我找一下我的课表。", "您慢慢看", offer="S3"),
        t("刚才说的是啥时候？", "下午4点", offer="S3"),
        t("嗯，行，帮我订吧。", "预约成功", write="book:S3"),
        t("多谢。", "不客气"),
    ),
    "24 时间格式和后续改约": (
        START, t("想约十一月十日下午二点整。", "下午2点", offer="S2"),
        t("可以了，就这个时段。", "预约成功", write="book:S2"),
        t("我周三下午四点有空，换成那个。", "改到这个时间", offer="S3"),
        t("我同意改期。", "改期成功", write="change:S3"),
        t("现在我约的是几点来着？", "下午4点", "A101"),
    ),
    "25 核验与操作都拒绝模糊授权": (
        t("我在替别人办，这个号能用吗？", "先不往下办", verified=False),
        t("是给我自己办的。", "哪个方便"),
        t("我预约11号吧。", "下午4点", offer="S3"),
        t("先别急着约，我再想想。", "您慢慢看", offer="S3"),
        t("不是要约，先不用了。", "先不办"),
        t("嗯。", "没听明白"),
    ),
    "26 说不清时逐步澄清": (
        START, t("我那个就可能吧，先这样。", "没听明白"),
        t("有更晚一些的吗？", "比哪个时间"),
        t("最快是哪天？", "11月9号上午9点", offer="S1"),
        t("呃，那个我没有听清，再说一遍。", "上午9点", offer="S1"),
        t("成，就这么定。", "预约成功", write="book:S1"),
    ),
    "27 只咨询、拒绝与改选": (
        START,
        t("我只想问11号几点，不用替我约。", "11月11号下午4点", "只帮您查时间"),
        t("可以。", "想约哪一天"),
        t("就第一个吧。", "上午9点", offer="S1"),
        t("不行，改成第二个。", "下午2点", offer="S2"),
        t("只问一下第三个，不用帮我约。", "下午4点", "只帮您查时间"),
        t("确认预约。", "先说想办什么"),
    ),
    "28 跑题后保留刚才的时段": (
        t("明天会下雨吗？", "天气", "给您自己办", verified=False),
        START,
        t("只看下午的。", "下午2点", "下午4点"),
        t("午饭吃火锅怎么样？", "吃饭", "选哪个时段"),
        t("就第一个。", "下午2点", offer="S2"),
        t("帮我预约11号的酒店。", "对应的服务方", "下午2点", offer="S2"),
        t("我刚看完电影，改成第二个吧。", "下午4点", offer="S3"),
        t("行，就这个时间。", "预约成功", write="book:S3"),
        t("今晚球赛几比几？", "比赛消息", "核对办理结果"),
    ),
    "29 跑题时保留独立的取消确认": (
        START,
        t("我要第一个。", "上午9点", offer="S1"),
        t("可以。", "预约成功", write="book:S1"),
        t("帮我取消预约。", "确认取消", offer="cancel"),
        t("能帮我取消演唱会的票吗？", "这个话题", "确认取消", offer="cancel"),
        t("可以。", "确认取消", offer="cancel"),
        t("确认取消。", "已取消", write="cancel:S1"),
    ),
    "30 回放真实取消请求和语音错字": (
        t("嗯，是的是的。", "哪个方便"),
        t("我约第三个。", "下午4点", offer="S3"),
        t("可以。", "预约成功", write="book:S3"),
        t("我不想办了，你帮我把这个预约取消掉吧。", "下午4点", "A101", "确认取消", offer="cancel"),
        t("呃你帮我取消语。", "确认取消", offer="cancel"),
        t("取消了吗？", "还没有取消", "等您确认", offer="cancel"),
        t("确定。", "确认取消", offer="cancel"),
        t("对，取消吧。", "已取消", write="cancel:S3"),
        t("取消成功了吗？", "已经取消", "A101", "已释放"),
        t("再帮我把这笔预约取消掉。", "已经取消", "A101"),
    ),
    "31 暂停、放弃改期与取消后重新预约": (
        START, t("第二个吧。", "下午2点", offer="S2"),
        t("确认预约。", "预约成功", write="book:S2"),
        t("我不想办了。", "目前还保留着"),
        t("改成第一个。", "上午9点", offer="S1"),
        t("取消刚才选的新时间。", "没有变动"),
        t("我约的是几点？", "下午2点"),
        t("我只是想问取消预约怎么弄。", "可以取消"),
        t("现在不太想取消预约。", "没有变动"),
        t("请取消这笔预约。", "确认取消", offer="cancel"),
        t("先不要取消了，保留原来的预约。", "没有变动"),
        t("取消了吗？", "还没有取消", "下午2点"),
        t("帮我撤掉这笔预约。", "确认取消", offer="cancel"),
        t("确认取消。", "已取消", write="cancel:S2"),
        t("我想恢复原预约。", "下午2点", offer="S2"),
        t("可以。", "预约成功", write="book:S2"),
    ),
    "32 新预约还未提交就取消": (
        START,
        t("我选第一个。", "上午9点", offer="S1"),
        t("帮我取消预约。", "没查到有效预约", "没有提交"),
        t("预约取消成功了吗？", "还没约时间"),
        t("我想约第二个。", "下午2点", offer="S2"),
        t("我不想办了。", "没有提交预约"),
    ),
}


def replay(scenarios, day=8):
    rows = []
    with TemporaryDirectory(prefix="zhiyu-bank-eval-") as folder:
        root = Path(folder)
        settings = Settings({"ASR_PROVIDER": "mock", "TTS_PROVIDER": "mock", "LLM_PROVIDER": "mock",
                             "DATABASE_URL": f"sqlite:///{(root / 'app.db').as_posix()}",
                             "BUSINESS_DATABASE": str(root / "bank.db"), "ADMIN_TOKEN": "test-only"})
        app = create_app(settings)
        fixed_time = datetime(2026, 11, day, 12, tzinfo=ZONE).timestamp()
        clock = lambda: fixed_time
        app.state.conversation.clock = app.state.conversation.business.clock = clock
        with TestClient(app) as client:
            for scenario, turns in scenarios.items():
                desk = {"X-Desk-Token": "desk_" + uuid4().hex + uuid4().hex}
                opened = client.post("/api/sessions", json={"scenario": "bank_card"}, headers=desk)
                opened.raise_for_status()
                info = opened.json()
                path = "/api/sessions/" + info["session_id"]
                auth = {"Authorization": "Bearer " + info["token"]}
                seen = sent_before = 0
                for index, turn in enumerate(turns):
                    result = client.post(path + "/turns", headers=auth, json={
                        "text": turn.text, "event_id": "evt_" + uuid4().hex, "expected_turn": index,
                    })
                    result.raise_for_status()
                    snapshot = result.json()["snapshot"]
                    trail = client.get(path + "/events", headers=auth).json()
                    writes = [e["payload"] for e in trail if e["event_type"] == "operation.receipt"
                              and e["payload"]["status"] == "succeeded"]
                    actions = {e["payload"]["operation_id"]: e["payload"]["action"]
                               for e in trail if e["event_type"] == "operation.prepared"}
                    sent = [e["payload"]["operation_id"] for e in trail if e["event_type"] == "operation.sent"]
                    effects = [f'{actions[e["operation_id"]]}:{e["result"]["slot"]}' for e in writes[seen:]]
                    seen = len(writes)
                    offer = snapshot["offer"]
                    target = (offer["target_slot"] or "cancel") if offer else None
                    problems = [f"回复缺少 {phrase}" for phrase in turn.contains if phrase not in snapshot["reply"]]
                    if target != turn.offer:
                        problems.append(f"待确认时段预期 {turn.offer}，实际 {target}")
                    if snapshot["verified"] != turn.verified:
                        problems.append(f"身份确认预期 {turn.verified}")
                    if effects != ([turn.write] if turn.write else []):
                        problems.append(f"写入预期 {turn.write}，实际 {effects}")
                    sent_now = len(sent) - sent_before
                    if sent_now != int(turn.write is not None):
                        problems.append(f"操作提交次数预期 {int(turn.write is not None)}，实际 {sent_now}")
                    sent_before = len(sent)
                    rows.append({"scenario": scenario, "turn": index + 1, "customer": turn.text,
                                 "agent": snapshot["reply"], "offer": target, "sent": sent_now,
                                 "writes": effects, "issues": problems})
                client.post(path + "/close", headers=auth).raise_for_status()
    return rows


def main():
    parser = argparse.ArgumentParser(description="银行客服多轮问答验收")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--suite", choices=("original", "variations", "fresh", "all"), default="all")
    args = parser.parse_args()
    rows = replay(SCENARIOS) if args.suite in {"original", "all"} else []
    if args.suite in {"variations", "all"}:
        rows += replay(VARIATIONS) + replay(MONTH_END, day=29)
    if args.suite in {"fresh", "all"}:
        rows += replay(FRESH)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    passed = sum(not row["issues"] for row in rows)
    print(f"{len({row['scenario'] for row in rows})} conversations, {passed}/{len(rows)} turns passed. {args.out}")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
