import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import datetime

from services.business import Rejection, Unknown, ZONE
from services.bank import (BANK_AFTERNOON_WINDOW, BANK_APPOINTMENT, BANK_ASK_PREFERENCE, BANK_BRANCH, BANK_CHECKING, BANK_CONFIRM,
                           BANK_CONFIRM_FALLBACK, BANK_CONFIRM_HELP, BANK_DETOUR, BANK_FAQ, BANK_HANDOFF,
                           BANK_HANDOFF_QUESTION, BANK_NO_MATCH, BANK_OPENING, BANK_SCENARIO, BANK_TIME_OR_HANDOFF, BANK_WEEKDAY_WINDOW,
                           bank_availability, bank_intent, bank_list_reference, bank_short_answer, bank_slot_offer, bank_slot_time,
                           bank_space, bank_topics, bank_words, is_bank_space)
from services.bank_time import has_calendar_day, slot_matches
from services.dialogue import (BASELINE, OPENING, POLICY_ID, SPOKEN_OPENING, SPOKEN_RETRY,
                               canonical_ids, denies_spoken_identity, parse, redacted, slot_label, spoken_identity)
from services.customer import (CUSTOMER_OPENING, NAME_PROMPT, PHONE_PROMPT, customer_name, customer_phone,
                               intake_stage)
from services.storage import Store, digest, emit, encode, events, ident

PENDING = "state IN ('PREPARED','SENT','UNKNOWN')"
FAILURES = {
    "VERSION_CONFLICT": "预约状态已变化。请重新查询并选择，重新确认后才能办理。",
    "SLOT_UNAVAILABLE": "该时段已不可用。请重新查询其他时段。",
    "INVALID_ARGUMENT": "当前预约状态无法执行该操作，请重新查询。",
    "FORBIDDEN": "无法办理此预约，请核对预约编号。",
}


class Conversation:
    def __init__(self, path, business, clock=time.time, confirmation_ttl=120, session_ttl=3600):
        self.store = Store(path, "app")
        self.business = business
        self.clock = clock
        self.confirmation_ttl = confirmation_ttl
        self.session_ttl = session_ttl
        with self.store.tx(write=True) as db:
            db.execute("INSERT OR IGNORE INTO policies VALUES('baseline',NULL,?,'baseline',?,'{}')",
                       (encode(BASELINE), clock()))
            db.execute("INSERT OR IGNORE INTO active VALUES(1,'baseline')")
            db.execute("INSERT OR IGNORE INTO customer_identity_key VALUES(1,?)", (secrets.token_bytes(32),))
            self.customer_key = db.execute("SELECT secret FROM customer_identity_key").fetchone()[0]

    def open(self, fault=None, space=None, desk=None, scenario=None, customer_intake=False):
        session, token = ident("ses"), secrets.token_urlsafe(32)
        space = desk or space or session
        bank = scenario == BANK_SCENARIO
        if (bank and not desk) or (customer_intake and not bank):
            raise Rejection("FORBIDDEN")
        if bank:
            space = bank_space(desk)
        opening = BANK_OPENING if bank else SPOKEN_OPENING if desk else OPENING
        if customer_intake:
            opening = CUSTOMER_OPENING
        self.business.seed(space, rolling=customer_intake)
        with self.store.tx(write=True) as db:
            policy = db.execute("SELECT policy FROM active").fetchone()[0]
            db.execute("""INSERT INTO sessions(id,token_hash,expires,appointment,policy,state,reply,fault)
                        VALUES(?,?,?,?,?,'VERIFYING',?,?)""",
                       (session, hashlib.sha256(token.encode()).hexdigest(),
                        self.clock() + self.session_ttl, BANK_APPOINTMENT if bank else "A100",
                        policy, opening, encode({"space": space, "mode": fault, "scenario": scenario})))
            if desk:
                db.execute("INSERT INTO desk_calls VALUES(?,?,?,?)", (session, desk, "U100", self.clock()))
            if customer_intake:
                db.execute("INSERT INTO customer_intakes(session) VALUES(?)", (session,))
            emit(db, session, 0, "session.disclosed", {"policy_id": POLICY_ID, "reply": opening})
        return session, token

    def _get(self, db, session):
        row = db.execute("SELECT * FROM sessions WHERE id=?", (session,)).fetchone()
        if row is None:
            raise Rejection("FORBIDDEN")
        return row

    def auth(self, session, token):
        with self.store.tx() as db:
            row = self._get(db, session)
            if row["expires"] <= self.clock() or not secrets.compare_digest(
                    hashlib.sha256(token.encode()).hexdigest(), row["token_hash"]):
                raise Rejection("FORBIDDEN")

    @staticmethod
    def _space(row):
        return json.loads(row["fault"])["space"]

    def _say(self, db, row, reply, state=None, **evidence):
        state = state or row["state"]
        db.execute("UPDATE sessions SET reply=?,state=? WHERE id=?", (reply, state, row["id"]))
        emit(db, row["id"], row["turn"], "assistant.replied",
             {"text": reply, "from": row["state"], "state": state, "policy_id": POLICY_ID, **evidence})

    def _last_reply(self, db, session):
        reply = db.execute("""SELECT payload FROM events WHERE session=? AND type='assistant.replied'
                              ORDER BY sequence DESC LIMIT 1""", (session,)).fetchone()
        return json.loads(reply[0]) if reply else {}

    def _bank_window(self, db, row, slots, lead="", *, general=False):
        if not slots:
            self._say(db, row, lead + BANK_NO_MATCH, "OFFERING", question="time_or_handoff")
        elif not general and len({slot["start"][:10] for slot in slots}) == 1:
            self._say(db, row, lead + BANK_AFTERNOON_WINDOW, "OFFERING", question="time_preference",
                      preference_slots=[slot["id"] for slot in slots])
        else:
            self._say(db, row, lead + BANK_WEEKDAY_WINDOW, "OFFERING", question="day_preference")

    def _bank_guidance(self, db, row, topics):
        for topic in topics:
            emit(db, row["id"], row["turn"], "bank.guidance", {"topic": topic})
        return "".join(BANK_FAQ[topic] for topic in topics)

    def _bank_restate(self, db, row, lead="", *, action=None):
        offered = db.execute("SELECT params FROM confirmations WHERE id=? AND status='OFFERED' AND expires>?",
                             (row["confirmation"], self.clock())).fetchone()
        if not offered:
            return False
        params = json.loads(offered["params"])
        if action and params["action"] != action:
            return False
        reply = bank_slot_offer(params, lead)
        slot_options = self._last_reply(db, row["id"]).get("slot_options", [])
        self._say(db, row, reply, "CONFIRMING", confirmation_id=row["confirmation"], slot_options=slot_options)
        return True

    def _invalidate(self, db, row, reason):
        if row["confirmation"]:
            cf = row["confirmation"]
            changed = db.execute("""UPDATE confirmations SET status='REVOKED'
                                    WHERE id=? AND status IN ('OFFERED','ACCEPTED')""", (cf,)).rowcount
            if changed:
                emit(db, row["id"], row["turn"], "confirmation.revoked", {"confirmation_id": cf, "reason": reason})
        db.execute("UPDATE sessions SET confirmation=NULL,revision=revision+1 WHERE id=?", (row["id"],))
        op = db.execute("SELECT * FROM operations WHERE session=? AND state='PREPARED'", (row["id"],)).fetchone()
        if op:
            self._finish(db, row, op, {"operation_id": op["id"], "status": "rejected", "code": reason}, speak=False)

    def _pending(self, db, session):
        return db.execute(f"SELECT * FROM operations WHERE session=? AND {PENDING}", (session,)).fetchone()

    def context(self, session):
        with self.store.tx() as db:
            row = self._get(db, session)
            config = db.execute("SELECT config FROM policies WHERE id=?", (row["policy"],)).fetchone()[0]
            return row["state"], json.loads(config), bool(row["scope"] and row["verified_until"] > self.clock())

    def intent_context(self, session):
        with self.store.tx() as db:
            previous = self._last_reply(db, session)
            question = "appointment_confirmation" if previous.get("confirmation_id") else previous.get("question")
            return {"question": question, "offered_slots": previous.get("slot_options", [])}

    def snapshot(self, session):
        with self.store.tx() as db:
            row = self._get(db, session)
            scope = row["scope"] if row["scope"] and row["verified_until"] > self.clock() else None
            appointment, slots = None, []
            if scope:
                try:
                    appointment = self.business.read(self._space(row), scope, row["appointment"])
                    slots = self.business.slots(self._space(row), scope, row["appointment"])
                except Rejection:
                    pass
            pending = self._pending(db, session)
            confirmation = db.execute("SELECT params,status,expires FROM confirmations WHERE id=?",
                                      (row["confirmation"],)).fetchone()
            authorized = bool(scope and confirmation and confirmation["status"] == "OFFERED"
                              and confirmation["expires"] > self.clock())
            intake = db.execute("SELECT * FROM customer_intakes WHERE session=?", (session,)).fetchone()
            return {"session_id": session, "state": row["state"], "turn": row["turn"],
                    "scenario": json.loads(row["fault"]).get("scenario"),
                    "intake_stage": intake_stage(intake) if intake and not scope else None,
                    "policy": row["policy"], "verified": bool(scope),
                    "reply": row["reply"] if scope or not row["scope"] or row["state"] in {"HANDOFF", "CLOSED"}
                    else "身份核验已过期，请重新核验。", "appointment": appointment,
                    "slots": slots, "closed": bool(row["closed"]), "pending": pending["state"] if pending else None,
                    "offer": json.loads(confirmation["params"]) if authorized else None,
                    "evidence": db.execute("SELECT COUNT(*) FROM events WHERE session=?", (session,)).fetchone()[0]}

    def submit(self, session, text, event_id=None, expected_turn=None, suggestion=None, reliable=True,
               reverify=False, before_send=None, auto=True):
        event_id = event_id or ident("evt")
        with self.store.tx(write=True) as db:
            row = self._get(db, session)
            seen = db.execute("SELECT session FROM events WHERE id=?", (event_id,)).fetchone()
            if seen:
                if seen["session"] != session:
                    raise Rejection("INVALID_ARGUMENT")
                return False
            if row["closed"] or (row["state"] == "HANDOFF" and is_bank_space(self._space(row))):
                raise Rejection("SESSION_CLOSED")
            if expected_turn is not None and expected_turn != row["turn"]:
                return False
            db.execute("UPDATE sessions SET turn=turn+1,expires=MAX(expires,?) WHERE id=?",
                       (self.clock() + self.session_ttl, session))
            db.execute("DELETE FROM reports WHERE session=?", (session,))
            row = self._get(db, session)
            intake = db.execute("SELECT * FROM customer_intakes WHERE session=?", (session,)).fetchone()
            capturing_phone = intake and not row["scope"] and intake_stage(intake) in {"phone", "confirm"}
            emit(db, session, row["turn"], "user.received",
                 {"text": redacted(text, minimum=3 if capturing_phone else 6)}, event_id)
            self._apply(db, row, text, suggestion, reliable, reverify)
        if before_send:
            before_send()
        if auto:
            self.advance(session, force=True)
        return True

    def _identify_customer(self, db, row, intake, text, action, target, reliable):
        stage = intake_stage(intake)
        name, phone_key, tail = intake["name"], intake["phone_key"], intake["phone_tail"]
        follow_up = spoken_identity(text) if reliable else None
        updated = False
        if reliable and action not in {"off_topic", "bank_faq"}:
            supplied_name = None if follow_up is not None else customer_name(
                text, unlabelled=stage == "name" and action in {"clarify", "unrecognized"})
            phone = customer_phone(text)
            if supplied_name or phone:
                name = supplied_name or name
                if phone:
                    phone_key = hmac.digest(self.customer_key, f"{self._space(row)}:{phone}".encode(), "sha256").hex()
                    tail = phone[-4:]
                db.execute("UPDATE customer_intakes SET name=?,phone_key=?,phone_tail=? WHERE session=?",
                           (name, phone_key, tail, row["id"]))
                updated = True

        if stage == "confirm" and not updated and follow_up is not None:
            scope, appointment = self.business.bank_customer(self._space(row), name, phone_key, tail)
            return scope, appointment, follow_up
        if not name:
            prompt = NAME_PROMPT
        elif not phone_key:
            prompt = f"好的，{name}。{PHONE_PROMPT}" if updated else "麻烦再说一下完整的 11 位预约手机号，慢慢说就好。"
        else:
            tail_spoken = " ".join(tail)
            prompt = f"我对一下，您叫{name}，手机号尾号 {tail_spoken}。是您本人办理吗？"
        if reliable and action == "bank_faq":
            prompt = self._bank_guidance(db, row, target) + prompt
        elif reliable and action == "off_topic":
            prompt = target + "咱们接着说预约。" + prompt
        question = "identity" if name and phone_key else "phone" if name else "name"
        self._say(db, row, prompt, "VERIFYING", question=question)
        return None

    def _apply(self, db, row, text, suggestion, reliable, reverify):
        session, space = row["id"], self._space(row)
        bank = is_bank_space(space)
        scope = row["scope"] if row["scope"] and row["verified_until"] > self.clock() else None
        pending = self._pending(db, session)
        today = datetime.fromtimestamp(self.clock(), ZONE).date() if bank else None
        action, target = bank_intent(text, today=today) if bank else parse(text)
        previous = self._last_reply(db, session) if bank or action == "continue" else {}
        question = previous.get("question")
        short = bank_short_answer(text) if bank else None
        if short and question in {"handoff", "time_or_handoff", "paused"}:
            if reliable and ((question == "handoff" and short in {"yes", "polite", "transfer"}) or
                             (question == "time_or_handoff" and short == "transfer")):
                action = "handoff"
            elif reliable and (short == "no" or question == "paused"):
                self._say(db, row, "好，有需要您再跟我说。", question="paused")
                return
            else:
                prompt = BANK_HANDOFF_QUESTION if question == "handoff" else BANK_TIME_OR_HANDOFF
                if question == "paused":
                    prompt = "好，有需要您再跟我说。"
                self._say(db, row, prompt, question=question)
                return
        if question == "handoff" and action in {"hold", "courtesy", "continue", "off_topic"}:
            lead = target if action == "off_topic" else "好，您慢慢考虑。" if action != "continue" else ""
            self._say(db, row, lead + BANK_HANDOFF_QUESTION, question="handoff")
            return
        if bank and reliable and action in {"unrecognized", "clarify"} and (suggestion or {}).get("intent") == "off_topic":
            action, target = "off_topic", BANK_DETOUR
        detour = target if action == "off_topic" and reliable else ""
        if action == "handoff":
            if bank and not reliable:
                self._say(db, row, "刚才没听清，您是想转人工吗？", question="handoff")
                return
            self._invalidate(db, row, "HANDOFF")
            pending = self._pending(db, session)
            latest = db.execute("SELECT state,receipt FROM operations WHERE session=? ORDER BY rowid DESC LIMIT 1",
                                (session,)).fetchone()
            result = self.business.handoff(space, session, {
                "verified": bool(scope), "follow_up": "核对预约处理情况",
                "last_operation_status": latest["state"] if latest else None,
                "last_receipt": json.loads(latest["receipt"]) if latest and latest["receipt"] and scope else None,
                "unresolved_operation": pending["id"] if pending else None,
            })
            emit(db, session, row["turn"], "handoff.created", result)
            if bank:
                reply = BANK_HANDOFF
            else:
                detail = "预约操作结果仍待核实。" if pending else ""
                reply = f'已登记人工工单 {result["ticket"]}。{detail}'
            self._say(db, row, reply, "HANDOFF")
            return
        if not scope or reverify:
            self._invalidate(db, row, "REVERIFY")
            db.execute("UPDATE sessions SET scope=NULL,verified_until=NULL WHERE id=?", (session,))
            binding = db.execute("SELECT expected_scope FROM desk_calls WHERE session=?", (session,)).fetchone()
            intake = db.execute("SELECT * FROM customer_intakes WHERE session=?", (session,)).fetchone()
            if intake and intake_stage(intake) == "confirm":
                correction = "刚才哪项有误，是姓名还是手机号？"
                words = bank_words(text)
                unclear_correction = re.fullmatch(r"不对[啊呀呢]?|说错了|有一项不对", words)
                if question in {"identity", "identity_correction"} and unclear_correction:
                    self._say(db, row, correction, "VERIFYING", question="identity_correction")
                    return
                if question in {"identity", "identity_correction"} and not denies_spoken_identity(text):
                    suffix = r"(?:不对|有误|错了)" + ("?" if question == "identity_correction" else "")
                    if reliable and re.fullmatch(r"(?:姓名|名字|全名)" + suffix, words):
                        db.execute("UPDATE customer_intakes SET name=NULL WHERE session=?", (session,))
                        self._say(db, row, "那麻烦您再说一下正确的全名。", "VERIFYING", question="name")
                        return
                    if reliable and re.fullmatch(r"(?:手机号|手机号码|号码|电话|尾号)" + suffix, words):
                        db.execute("UPDATE customer_intakes SET phone_key=NULL,phone_tail=NULL WHERE session=?", (session,))
                        self._say(db, row, "那麻烦您再报一下完整的 11 位手机号。", "VERIFYING", question="phone")
                        return
                    if question == "identity_correction" and (not reliable or not (customer_phone(text) or customer_name(text))):
                        self._say(db, row, correction, "VERIFYING", question="identity_correction")
                        return
            identity_no = question == "identity" and re.fullmatch(r"不(?:是)?[的啊呀呢]?", bank_words(text))
            if action == "refuse" or (binding and not detour and (identity_no or denies_spoken_identity(text))):
                emit(db, session, row["turn"], "identity.rejected", {"code": "NOT_VERIFIED"})
                reply = f"明白，那我先不往下办了。{BANK_HANDOFF_QUESTION}" if bank else "已停止办理。如需协助，可以转人工。"
                self._say(db, row, reply, "VERIFYING", **({"question": "handoff"} if bank else {}))
                return
            follow_up = ""
            appointment = None
            try:
                if intake:
                    identity = self._identify_customer(db, row, intake, text, action, target, reliable)
                    if identity is None:
                        return
                    verified, appointment, follow_up = identity
                elif not reliable:
                    raise Rejection("NOT_VERIFIED")
                elif binding:
                    follow_up = spoken_identity(text)
                    if follow_up is None:
                        raise Rejection("NOT_VERIFIED")
                    verified = binding["expected_scope"]
                else:
                    verified = self.business.verify(text)
            except Rejection:
                emit(db, session, row["turn"], "identity.rejected", {"code": "NOT_VERIFIED"})
                if bank:
                    if reliable and action == "bank_faq":
                        prompt = self._bank_guidance(db, row, target) + BANK_CONFIRM
                    elif detour:
                        prompt = f"{detour}咱们先接着办卡。{BANK_CONFIRM}"
                    elif any(phrase in text for phrase in ("确认什么", "核验什么", "为什么要确认")):
                        prompt = BANK_CONFIRM_HELP
                    elif row["reply"] in {BANK_CONFIRM, BANK_CONFIRM_HELP, BANK_CONFIRM_FALLBACK}:
                        prompt = BANK_CONFIRM_FALLBACK
                    else:
                        prompt = BANK_CONFIRM
                else:
                    prompt = SPOKEN_RETRY if binding else "请先输入六位演示验证码，或选择转人工。"
                self._say(db, row, prompt, "VERIFYING", **({"question": "identity"} if bank else {}))
                return
            if appointment is None:
                appointment = BANK_APPOINTMENT if bank else next(
                    a[0] for a in self.business.fixture["appointments"] if a[1] == verified)
            db.execute("UPDATE sessions SET scope=?,verified_until=?,appointment=? WHERE id=?",
                       (verified, min(row["expires"], self.clock() + 900), appointment, session))
            emit(db, session, row["turn"], "identity.verified",
                 {"scope": verified, "method": "sandbox_spoken_confirmation" if binding else "synthetic_code"})
            if bank and not follow_up:
                follow_up = "查询可预约时段"
            if follow_up:
                db.execute("UPDATE sessions SET state='COLLECTING' WHERE id=?", (session,))
                self._apply(db, self._get(db, session), follow_up, None, reliable, False)
                return
            self._say(db, row, "身份核验通过。您可以查询预约、改期或取消预约。", "COLLECTING")
            return
        if pending and pending["state"] != "PREPARED":
            reply = BANK_CHECKING if bank else "上一项操作已发出，结果仍待核实。核对后才能办理新的变更。"
            self._say(db, row, detour + reply, "RECONCILING")
            return
        rolling = bank and scope.startswith("C")
        choice_ids = previous.get("slot_options", [])
        preference = {key: previous[key] for key in ("question", "preference_slots")
                      if key in previous and question in {"day_preference", "time_preference"}}
        if detour:
            if row["confirmation"] and self._bank_restate(db, row, f"{detour}回到刚才的预约。"):
                return
            if row["state"] == "COMPLETED":
                next_step = "刚才那笔预约已经处理好了。您还想核对办理结果吗？"
            elif row["state"] == "HANDOFF":
                next_step = "刚才已经帮您登记给人工客服了。您还有预约方面的问题吗？"
            elif question == "time_preference":
                next_step = BANK_AFTERNOON_WINDOW
            elif question == "day_preference":
                next_step = BANK_WEEKDAY_WINDOW
            elif row["state"] == "OFFERING":
                next_step = "咱们接着看办卡时间。您想选哪个时段？"
            else:
                next_step = "咱们接着说办卡预约。您想查一下时间，还是先问问需要带什么？"
            self._say(db, row, detour + next_step, slot_options=choice_ids, **preference)
            return
        if action == "continue":
            links = {key: previous[key] for key in ("operation_id", "receipt_id", "confirmation_id", "slot_options",
                                                   "question", "preference_slots")
                     if key in previous}
            self._say(db, row, row["reply"], **links)
            return
        if action == "discard":
            self._invalidate(db, row, "USER_DISCARDED")
            reply = "好，那咱们先不办，您的预约这次没有变动。" if bank else "已放弃本次办理，预约未因此变更。"
            self._say(db, row, reply, "COLLECTING", **({"question": "paused"} if bank else {}))
            return
        reference = bank_list_reference(text, choice_ids) if bank else None
        if short == "reference" and choice_ids and not previous.get("confirmation_id"):
            reference = choice_ids[0] if len(choice_ids) == 1 else ""
        if reference is not None:
            if not reference or not reliable:
                self._say(db, row, "您指的是哪个时段？说第几个或者具体几点都行。", slot_options=choice_ids)
                return
            text = reference
            action = "choose"
        if action in {"confirm", "agree"}:
            self._confirm(db, row, scope, bank_words(text) if bank else text, reliable, target, bank,
                          conversational=action == "agree")
            return
        if action == "bank_faq":
            self._say(db, row, self._bank_guidance(db, row, target), slot_options=choice_ids, **preference)
            return
        if action == "courtesy":
            self._say(db, row, "不客气。您还有什么想问的吗？")
            return
        if action == "hold":
            self._say(db, row, "好，您慢慢看。想好了，再告诉我您要约哪个时间。", slot_options=choice_ids,
                      **preference)
            return
        if rolling and not choice_ids and re.fullmatch(
                r"(?:我选|就选|就|选)?第[一二两三四1234](?:个|项)?吧?", bank_words(text)):
            prompt = BANK_AFTERNOON_WINDOW if question == "time_preference" else BANK_ASK_PREFERENCE
            self._say(db, row, prompt, "OFFERING", **preference)
            return
        if action in {"change_request", "clarify_choice"}:
            self._invalidate(db, row, "INTENT_CHANGED")
            reply = ("可以，您想换到哪天或几点？" if action == "change_request" else
                     "您可以先选定一个时间，告诉我哪天更方便，我帮您核对。")
            self._say(db, row, reply, "COLLECTING")
            return
        if action == "recap_time" and row["confirmation"] and self._bank_restate(db, row):
            return

        explicit = re.findall(r"(?<![A-Z0-9])A\d+(?!\d)", canonical_ids(text).upper())
        appt = explicit[-1] if explicit else row["appointment"]
        if not bank:
            self._invalidate(db, row, "INTENT_CHANGED")
        try:
            current = self.business.read(space, scope, appt)
            slots = self.business.slots(space, scope, appt)
        except Rejection as exc:
            if bank:
                self._invalidate(db, row, "INTENT_CHANGED")
            emit(db, session, row["turn"], "access.rejected", {"code": exc.code})
            self._say(db, row, "无法访问该预约，请核对预约编号。", "COLLECTING")
            return
        available = [s for s in slots if s["available"] and
                     not (current["status"] == "active" and s["id"] == current["slot"])]
        current_slot = next((s for s in slots if s["id"] == current["slot"]), None)
        active_slot = current_slot if current["status"] == "active" else None
        ordinal_slots = [{"id": slot_id} for slot_id in choice_ids] if choice_ids or rolling else None
        keep_day = rolling and question == "time_preference" and not has_calendar_day(text)
        preferred_ids = previous.get("preference_slots", [])
        matching_slots = [slot for slot in available if slot["id"] in preferred_ids] if keep_day else available
        action, target = (bank_intent(text, matching_slots, active_slot, today=today, ordinal_slots=ordinal_slots)
                          if bank else parse(text, slots))
        if keep_day and re.fullmatch(r"(?:偏)?[早晚](?:些|点)?[啊吧]?", bank_words(text)):
            action, target = "relative_time", -1 if "早" in text else 1
        if rolling and reliable and question in {"day_preference", "time_preference"} and re.fullmatch(
                r"(?:都行|都可以|哪天都行|哪天都可以|随便|你安排吧|您安排吧|挑个有空的)", bank_words(text)):
            action, target = ("choose", matching_slots[0]["id"]) if matching_slots else ("ask_time", ())
        if action == "off_topic":
            action, target = "unrecognized", None
        if action == "decline_times":
            if not reliable:
                self._say(db, row, "我刚才没听清。您是想换个时间吗？")
                return
            self._invalidate(db, row, "TIMES_DECLINED")
            matches = slot_matches(target, available, today) if target and not re.search(
                r"没空|没时间|不方便|不行|不了|不要|不选|不约", target) else None
            options = [slot for slot in available if matches and slot["id"] in matches]
            lead = "好，原来的预约先给您保留。" if active_slot else "行，咱们换个时间。"
            if rolling and matches is not None:
                self._bank_window(db, row, options, lead)
                return
            options = options[:4]
            reply = (BANK_ASK_PREFERENCE if matches is None else
                     bank_availability(options) if options else BANK_NO_MATCH)
            self._say(db, row, lead + reply, "OFFERING", slot_options=[slot["id"] for slot in options],
                      question="time_or_handoff" if matches is not None and not options else "time")
            return
        if action == "cancel_status" and active_slot and row["confirmation"] and self._bank_restate(
                db, row, "这笔预约还没有取消，正在等您确认。", action="cancel"):
            return
        if bank and not reliable and action in {"cancel", "stop_request", "rebook_previous"}:
            self._say(db, row, "刚才这句我没听清。您是想取消预约，还是重新约一个时间？")
            return
        if action == "stop_request":
            self._invalidate(db, row, "USER_DISCARDED")
            reply = (f"好，您的预约 {appt} 目前还保留着。如果您要取消这笔，请说‘取消预约’。" if active_slot else
                     "好，那咱们先不办。刚才聊的时间没有提交预约。")
            self._say(db, row, reply, "COLLECTING")
            return
        if action == "rebook_previous":
            if current["status"] == "canceled" and current_slot:
                action, target = "choose", current_slot["id"]
            elif active_slot:
                action = "appointment_query"
            else:
                action = "query"
        if action == "relative_time":
            offered = db.execute("SELECT params FROM confirmations WHERE id=? AND status='OFFERED' AND expires>?",
                                 (row["confirmation"], self.clock())).fetchone()
            anchor = json.loads(offered["params"]).get("target_start") if offered else None
            anchor = anchor or (active_slot["start"] if active_slot else None)
            if keep_day:
                nearest = next(iter(matching_slots if target < 0 else reversed(matching_slots)), None)
            elif not anchor:
                self._say(db, row, "您想比哪个时间早一点或晚一点？告诉我日期或第几个就行。")
                return
            elif target < 0:
                nearest = next((slot for slot in reversed(available) if slot["start"] < anchor), None)
            else:
                nearest = next((slot for slot in available if slot["start"] > anchor), None)
            action, target = "ask_time", (nearest["id"],) if nearest else ()
        if bank and row["confirmation"] and action in {"unrecognized", "clarify"} and not any(
                term in text for term in ("改", "换", "取消", "不要", "不选", "不行", "不可以", "或者", "还是")):
            if self._bank_restate(db, row, "我再跟您对一下。"):
                return
        if action in {"unrecognized", "clarify"} and suggestion and reliable:
            # A model may propose a slot, but only a fresh server-authored offer can lead to a write.
            if suggestion.get("intent") in {"query", "help"}:
                action = suggestion["intent"]
            elif suggestion.get("intent") == "choose" and not any(x in text for x in ("不", "或", "?", "？")):
                action, target = "choose", suggestion.get("slot")
        if bank and not reliable and action in {"ask_time", "lookup_time", "choose", "cancel"}:
            self._say(db, row, "刚才没听清您说的是哪个时间，麻烦您再说一遍。")
            return
        if bank:
            self._invalidate(db, row, "INTENT_CHANGED")
        asked_time = action == "ask_time"
        guidance = (self._bank_guidance(db, row, bank_topics(text))
                    if bank and action in {"ask_time", "lookup_time", "choose"} else "")
        if action == "lookup_time":
            options = [slot for slot in slots if slot["id"] in target and
                       (slot["available"] or slot == active_slot)]
            if rolling and len({slot["start"][:10] for slot in options}) > 1:
                self._bank_window(db, row, options, guidance)
                return
            options = options[:4]
            times = "、".join(map(bank_slot_time, options))
            reply = f"可以参考{times}。我先只帮您查时间。" if times else BANK_NO_MATCH
            self._say(db, row, guidance + reply, "OFFERING", slot_options=[slot["id"] for slot in options],
                      question="time" if options else "time_or_handoff")
            return
        if asked_time:
            if active_slot and target == (active_slot["id"],):
                self._say(db, row, f"{guidance}您约的是{bank_slot_time(active_slot)}。预约编号是 {appt}。", "OFFERING")
                return
            if len(target) != 1:
                options = [slot for slot in available if slot["id"] in target]
                if rolling and not (options and len({slot["start"][:10] for slot in options}) == 1
                                    and re.search(r"几点|具体时段|具体时间", text)):
                    self._bank_window(db, row, options, guidance)
                    return
                options = options[:4]
                reply = bank_availability(options, include_date=not rolling) if options else BANK_NO_MATCH
                self._say(db, row, guidance + reply, "OFFERING", slot_options=[slot["id"] for slot in options],
                          question="time" if options else "time_or_handoff")
                return
            action, target = "choose", target[0]
        listing = "、".join(map(slot_label, available)) or "暂无可用时段，可以转人工登记需求"
        config = json.loads(db.execute("SELECT config FROM policies WHERE id=?", (row["policy"],)).fetchone()[0])
        if action in {"query", "appointment_query", "recap_time", "cancel_status"}:
            status = "已取消" if current["status"] == "canceled" else "有效"
            emit(db, session, row["turn"], "appointment.read", {"appointment": current, "slots": available})
            if bank:
                if rolling and action == "query":
                    self._bank_window(db, row, available, general=True)
                    return
                if current["status"] == "active":
                    lead = "这笔预约还没有取消。" if action == "cancel_status" else ""
                    intro = f"{lead}您约的是{bank_slot_time(current_slot)}，预约编号 {appt}。"
                elif current["status"] == "canceled":
                    intro = f"预约 {appt} 已经取消了，原来的到店时段已释放。"
                else:
                    intro = "好，Visa 借记卡可以约到店办。您现在还没约时间。"
                options = available[:4] if action == "query" else []
                reply = intro if action != "query" else intro + bank_availability(options)
                self._say(db, row, reply, "OFFERING", slot_options=[slot["id"] for slot in options],
                          question=("time" if options else "handoff") if action == "query" else None)
            else:
                self._say(db, row, f"预约 {appt} 的时段是 {slot_label(current_slot)}，状态{status}。可选时段：{listing}。", "OFFERING")
        elif action == "help":
            self._say(db, row, "改期需选择有空位的时段并明确确认。取消预约会释放原时段，恢复需由人工核实。", "COLLECTING")
        elif (action == "cancel" and current["status"] == "active" or
              action == "choose" and (bank or current["status"] == "active")) and reliable:
            chosen = next((s for s in available if s["id"] == target), None)
            if action == "choose" and not chosen:
                reply = ("那个时间现在约不上了。" + BANK_ASK_PREFERENCE if bank else
                         f"该时段当前不可办理。可选时段：{listing}。")
                self._say(db, row, reply, "OFFERING")
                return
            operation = "cancel" if not chosen else "change" if current["status"] == "active" else "book"
            params = {"action": operation, "appointment_id": appt,
                      "target_slot": chosen["id"] if chosen else None,
                      "target_start": chosen["start"] if chosen else None, "expected_version": current["version"]}
            revision = row["revision"] + 1
            cf = ident("cfm")
            expiry = min(row["verified_until"], self.clock() + self.confirmation_ttl)
            db.execute("INSERT INTO confirmations VALUES(?,?,?,?,?,?,?,'OFFERED')",
                       (cf, session, scope, revision, encode(params), digest(params), expiry))
            db.execute("UPDATE sessions SET confirmation=? WHERE id=?", (cf, session))
            emit(db, session, row["turn"], "intent.updated", {"intent_revision": revision, **params})
            emit(db, session, row["turn"], "confirmation.offered", {
                "confirmation_id": cf, "params": params, "before": current,
                "digest": digest(params), "expires": expiry})
            if bank and chosen:
                lead = "行，我再跟您对一下。" if row["confirmation"] else "好，我跟您对一下。"
                reply = guidance + bank_slot_offer(params, "" if asked_time else lead, asked_time=asked_time)
            elif bank:
                reply = (f"我跟您确认一下，您要取消{bank_slot_time(current_slot)}的预约 {appt}，对吗？"
                         "确定的话请说‘确认取消’。")
            else:
                offer = f"改至 {slot_label(chosen)}" if chosen else f"取消 {slot_label(current_slot)} 的预约并释放原时段"
                reply = f"请确认是否将预约 {appt} {offer}？同意请说“确认”。"
            evidence = {"slot_options": choice_ids} if bank and choice_ids else {}
            self._say(db, row, reply, "CONFIRMING", confirmation_id=cf, **evidence)
        elif bank and action == "cancel":
            reply = (f"预约 {appt} 已经取消了。您想再约个时间吗？" if current["status"] == "canceled" else
                     "我这边没查到有效预约。刚才选的时间也没有提交。")
            self._say(db, row, reply, "COLLECTING")
        else:
            if bank and action == "unrecognized":
                reply = "我刚才没听明白。您是想问到店要带什么，还是想查一下预约时间？"
            elif bank:
                reply = "您可以告诉我想约哪天，或者说想选第几个时间。"
            else:
                reply = f'{config["clarification"]} 可选时段：{listing}。'
            self._say(db, row, reply, "OFFERING")

    def _confirm(self, db, row, scope, text, reliable, target, bank, *, conversational=False):
        cf = db.execute("SELECT * FROM confirmations WHERE id=?", (row["confirmation"],)).fetchone()
        if not cf or cf["status"] != "OFFERED" or cf["expires"] <= self.clock() or not reliable:
            if cf and cf["expires"] <= self.clock():
                self._invalidate(db, row, "CONFIRMATION_EXPIRED")
            reply = "请先选择办理事项，并在我复述后明确确认。" if not cf or reliable else "我没有听清确认，请再说一次。"
            if bank:
                reply = ("您先说想办什么，我跟您对一下，再请您确认。" if not cf or reliable else
                         "刚才这句我没听清，麻烦您再说一遍。")
                if conversational and (not cf or cf["status"] != "OFFERED"):
                    if row["state"] == "COMPLETED":
                        self._say(db, row, "好，您还有什么想问的吗？")
                        return
                    reply = "您想约哪一天？说日期或第几个都行。"
            self._say(db, row, reply, "COLLECTING" if reliable else row["state"])
            return
        params = json.loads(cf["params"])
        if (bank and params["action"] == "cancel" and "取消" not in text) or (
                conversational and self._last_reply(db, row["id"]).get("confirmation_id") != cf["id"]):
            self._say(db, row, bank_slot_offer(params), "CONFIRMING", confirmation_id=cf["id"])
            return
        if (target is not None and target != params["target_slot"]) or (
                "取消" in text and params["action"] != "cancel") or (
                (any(word in text for word in ("改期", "修改")) or
                 bank and any(word in text for word in ("改到", "改至", "改为"))) and params["action"] != "change") or (
                text.strip().strip("。！!，,") in {"确认预约", "我确认预约", "同意预约", "我同意预约"}
                and params["action"] != "book"):
            self._invalidate(db, row, "CONFIRMATION_MISMATCH")
            reply = ("您刚才确认的和我复述的不一样。您再说一下想怎么办，我跟您对一下。" if bank else
                     "确认内容与刚才复述的事项不同，请重新选择。")
            self._say(db, row, reply, "COLLECTING")
            return
        operation = ident("op")
        request = {**params, "operation_id": operation, "session_id": row["id"], "scope": scope,
                   "intent_revision": cf["revision"], "confirmation_id": cf["id"]}
        db.execute("UPDATE confirmations SET status='ACCEPTED' WHERE id=?", (cf["id"],))
        emit(db, row["id"], row["turn"], "confirmation.accepted", {"confirmation_id": cf["id"], "digest": cf["digest"]})
        db.execute("INSERT INTO operations(id,session,confirmation,request,state,fault) VALUES(?,?,?,?,'PREPARED',?)",
                   (operation, row["id"], cf["id"], encode(request), json.loads(row["fault"])["mode"]))
        emit(db, row["id"], row["turn"], "operation.prepared", request)
        reply = "好，您稍等一下，我这边帮您办。" if bank else "已收到您的确认，正在办理。"
        self._say(db, row, reply, "EXECUTING", operation_id=operation)

    def _finish(self, db, row, op, receipt, speak=True):
        status = receipt["status"]
        db.execute("UPDATE operations SET state=?,receipt=? WHERE id=?", (status.upper(), encode(receipt), op["id"]))
        result = {"session_id": row["id"], "policy_version": row["policy"],
                  "confirmation_id": op["confirmation"], **receipt}
        db.execute("INSERT OR IGNORE INTO results VALUES(?,?,?)", (op["id"], row["id"], encode(result)))
        emit(db, row["id"], row["turn"], "operation.receipt", receipt)
        emit(db, row["id"], row["turn"], "result.written", result)
        db.execute("DELETE FROM reports WHERE session=?", (row["id"],))
        handoff = db.execute("SELECT 1 FROM events WHERE session=? AND type='handoff.created' LIMIT 1",
                             (row["id"],)).fetchone()
        if handoff and speak:
            ticket = self.business.handoff(self._space(row), row["id"], {
                "last_operation_status": status.upper(), "last_receipt": receipt, "unresolved_operation": None,
            })
            emit(db, row["id"], row["turn"], "handoff.updated", ticket)
        if row["closed"]:
            self._write_session_result(db, row)
        if not speak or row["closed"] or row["state"] == "HANDOFF":
            return
        if status == "succeeded":
            appt = receipt["result"]
            bank = is_bank_space(self._space(row))
            if appt["status"] == "canceled":
                reply = (f'好，您的到店预约已取消。预约编号是 {appt["id"]}。' if bank else
                         f'预约 {appt["id"]} 已取消。')
            else:
                slot = {"id": appt["slot"], "start": receipt["slot_start"]}
                operation = json.loads(op["request"])["action"]
                if operation == "book":
                    reply = (f'好，预约成功了。预约编号 {appt["id"]}，您可以记一下。'
                             f'{bank_slot_time(slot)}到{BANK_BRANCH}办卡就行，记得带上本人有效身份证件原件。')
                elif bank:
                    reply = (f'好，改期成功了。您现在是{bank_slot_time(slot)}到{BANK_BRANCH}，'
                             f'预约编号还是 {appt["id"]}。')
                else:
                    reply = f'改期成功。预约 {appt["id"]} 已改至 {slot_label(slot)}。'
            self._say(db, row, reply, "COMPLETED", operation_id=op["id"], receipt_id=receipt["receipt_id"])
        else:
            self._say(db, row, FAILURES.get(receipt.get("code"), "此次操作未办理，请重新选择并确认。"),
                      "COLLECTING", operation_id=op["id"], receipt_id=receipt.get("receipt_id"))

    def advance(self, session, force=False, barrier=None):
        # The claim transaction is the last point at which a newer intent can supersede PREPARED.
        with self.store.tx(write=True) as db:
            row = self._get(db, session)
            op = self._pending(db, session)
            if not op or (not force and op["next_at"] > self.clock()):
                return
            request = json.loads(op["request"])
            space = self._space(row)
            send = op["state"] == "PREPARED"
            attempt = op["attempts"]
            if send:
                cf = db.execute("SELECT * FROM confirmations WHERE id=?", (op["confirmation"],)).fetchone()
                params = {k: request[k] for k in
                          ("action", "appointment_id", "target_slot", "target_start", "expected_version")}
                if (not cf or cf["status"] != "ACCEPTED" or row["closed"] or cf["expires"] <= self.clock()
                        or cf["session"] != session or cf["scope"] != row["scope"] or cf["scope"] != request["scope"]
                        or cf["revision"] != row["revision"] or cf["revision"] != request["intent_revision"]
                        or cf["digest"] != digest(params)):
                    self._finish(db, row, op, {"operation_id": op["id"], "status": "rejected",
                                             "code": "CONFIRMATION_EXPIRED" if cf and cf["expires"] <= self.clock()
                                             else "CONFIRMATION_MISMATCH"})
                    return
                db.execute("UPDATE confirmations SET status='CONSUMED' WHERE id=?", (cf["id"],))
                db.execute("UPDATE operations SET state='SENT',attempts=1,next_at=? WHERE id=?",
                           (self.clock() + 2, op["id"]))
                emit(db, session, row["turn"], "operation.sent", {"operation_id": op["id"], "attempt": 1})
                attempt = 1
        if barrier:
            barrier("claimed")
        try:
            if send:
                receipt = self.business.execute(space, request, op["fault"], attempt)
                if barrier:
                    barrier("committed")
            else:
                receipt = self.business.operation(space, op["id"], op["fault"])
                if receipt["status"] == "not_found" and self.business.authoritative_absence and attempt < 3:
                    with self.store.tx(write=True) as db:
                        changed = db.execute("""UPDATE operations SET state='SENT',attempts=attempts+1,next_at=?
                                              WHERE id=? AND attempts=? AND state IN ('SENT','UNKNOWN')""",
                                             (self.clock() + 2, op["id"], attempt)).rowcount
                        if not changed:
                            return
                        emit(db, session, self._get(db, session)["turn"], "operation.reconciled",
                             {"operation_id": op["id"], "lookup": "not_found", "retry_same_key": True})
                    receipt = self.business.execute(space, request, op["fault"], attempt + 1)
                elif receipt["status"] in {"not_found", "pending"}:
                    raise Unknown(receipt["status"])
        except Unknown as exc:
            with self.store.tx(write=True) as db:
                row = self._get(db, session)
                changed = db.execute("""UPDATE operations SET state='UNKNOWN',next_at=? WHERE id=?
                                      AND state IN ('SENT','UNKNOWN')""", (self.clock() + 2, op["id"])).rowcount
                if changed:
                    emit(db, session, row["turn"], "operation.unknown", {"operation_id": op["id"], "reason": str(exc)})
                    db.execute("DELETE FROM reports WHERE session=?", (session,))
                    if not row["closed"] and row["state"] != "HANDOFF":
                        reply = BANK_CHECKING if is_bank_space(space) else "预约操作结果仍待核实。您可以等待核对或转人工。"
                        self._say(db, row, reply, "RECONCILING",
                                  operation_id=op["id"])
            return
        with self.store.tx(write=True) as db:
            live = self._pending(db, session)
            if live and live["id"] == op["id"]:
                row = self._get(db, session)
                if not send:
                    emit(db, session, row["turn"], "operation.reconciled",
                         {"operation_id": op["id"], "lookup": receipt["status"]})
                if barrier:
                    barrier("before_writeback")
                self._finish(db, row, live, receipt)

    def recover(self, active_sessions=()):
        active_sessions = set(active_sessions)
        now = self.clock()
        threshold = now + self.session_ttl / 2 if active_sessions else now
        with self.store.tx() as db:
            nearing_expiry = db.execute("SELECT id,expires FROM sessions WHERE closed=0 AND expires<=?", (threshold,)).fetchall()
            due = [row[0] for row in db.execute(f"SELECT session FROM operations WHERE {PENDING} AND next_at<=?", (now,))]
        renewals = [(now + self.session_ttl, row["id"]) for row in nearing_expiry if row["id"] in active_sessions]
        if renewals:
            with self.store.tx(write=True) as db:
                db.executemany("UPDATE sessions SET expires=MAX(expires,?) WHERE id=? AND closed=0", renewals)
        for row in nearing_expiry:
            if row["id"] not in active_sessions and row["expires"] <= now:
                self.close(row["id"])
        for session in due:
            self.advance(session)

    def close(self, session):
        with self.store.tx(write=True) as db:
            row = self._get(db, session)
            if row["closed"]:
                return
            self._invalidate(db, row, "SESSION_CLOSED")
            db.execute("UPDATE sessions SET closed=1,state='CLOSED' WHERE id=?", (session,))
            emit(db, session, row["turn"], "session.closed", {})
            self._write_session_result(db, row)
            db.execute("DELETE FROM reports WHERE session=?", (session,))

    def _write_session_result(self, db, row):
        operations = db.execute("SELECT id,state,receipt FROM operations WHERE session=? ORDER BY rowid",
                                (row["id"],)).fetchall()
        handoff = db.execute("""SELECT payload FROM events WHERE session=? AND type IN ('handoff.created','handoff.updated')
                               ORDER BY sequence DESC LIMIT 1""", (row["id"],)).fetchone()
        ticket = json.loads(handoff[0])["ticket"] if handoff else None
        pending = [op["id"] for op in operations if op["state"] in {"PREPARED", "SENT", "UNKNOWN"}]
        outcomes = [json.loads(op["receipt"]) for op in operations if op["receipt"]]
        if pending:
            status = "pending"
        elif ticket:
            status = "handed_off"
        elif any(item["status"] == "succeeded" for item in outcomes):
            status = "completed"
        elif outcomes:
            status = "not_completed"
        else:
            status = "consulted" if row["scope"] else "unverified"
        result = {"session_id": row["id"], "policy_version": row["policy"], "status": status,
                  "handoff_ticket": ticket, "pending_operation_ids": pending, "operations": outcomes}
        db.execute("""INSERT INTO session_results VALUES(?,?) ON CONFLICT(session)
                      DO UPDATE SET data=excluded.data""", (row["id"], encode(result)))
        emit(db, row["id"], row["turn"], "session.written", result)

    def result(self, session):
        with self.store.tx() as db:
            result = db.execute("SELECT data FROM session_results WHERE session=?", (session,)).fetchone()
            return json.loads(result[0]) if result else None

    def evidence(self, session, after=0):
        with self.store.tx() as db:
            self._get(db, session)
            return events(db, session, after)
