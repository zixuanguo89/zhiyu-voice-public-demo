import json
import re
import secrets
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from services.bank import BANK_APPOINTMENT, BANK_BRANCH, BANK_SERVICE, is_bank_space
from services.storage import ROOT, Store, digest, encode, ident, iso

ZONE = ZoneInfo("Asia/Shanghai")
BANK_HORIZON_DAYS = 14
BANK_FIRST_HOUR = 14
BANK_END_HOUR = 17


class Rejection(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Unknown(Exception):
    """Transport failed. The caller must establish the business outcome separately."""


class Sandbox:
    # A read after transact() completes is authoritative in this mock.
    authoritative_absence = True

    def __init__(self, path, clock=time.time):
        self.store = Store(path, "business")
        self.clock = clock
        self.fixture = json.loads((ROOT / "fixtures/sandbox.json").read_text(encoding="utf-8"))

    def seed(self, space, *, rolling=False):
        now = datetime.fromtimestamp(self.clock(), ZONE)
        appointments = [["A201", "U200", "S4"]] if is_bank_space(space) else self.fixture["appointments"]
        with self.store.tx(write=True) as db:
            if rolling:
                db.execute("INSERT OR IGNORE INTO bank_rolling_schedule VALUES(?)", (space,))
            if self._rolling(db, space):
                existing = {row["start"] for row in db.execute("SELECT start FROM slots WHERE space=?", (space,))}
                for days in range(BANK_HORIZON_DAYS):
                    day = now + timedelta(days=days)
                    if day.weekday() >= 5:
                        continue
                    for hour in range(BANK_FIRST_HOUR, BANK_END_HOUR):
                        for minute in (0, 30):
                            start = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
                            if start > now and start.isoformat() not in existing:
                                db.execute("INSERT INTO slots VALUES(?,?,?,1)",
                                           (space, f"S{start:%Y%m%d%H%M}", start.isoformat()))
                return
            for appt, owner, slot in appointments:
                db.execute("INSERT OR IGNORE INTO appointments VALUES(?,?,?,?,1,'active')",
                           (space, appt, owner, slot))
            for slot, days, hour in self.fixture["slots"]:
                start = (now + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
                db.execute("INSERT OR IGNORE INTO slots VALUES(?,?,?,1)", (space, slot, start.isoformat()))

    def verify(self, proof):
        proof = proof.translate(str.maketrans("零〇一二两三四五六七八九幺０１２３４５６７８９",
                                              "00122345678910123456789"))
        match = re.fullmatch(r"\s*(?:验证码[是为：:]?\s*)?([0-9](?:\s*[0-9]){5})[。！!]?\s*", proof)
        if match:
            for scope, expected in self.fixture["customers"].items():
                if secrets.compare_digest(re.sub(r"\s+", "", match[1]), expected):
                    return scope
        raise Rejection("NOT_VERIFIED")

    def bank_customer(self, space, name, phone_key, phone_tail):
        with self.store.tx(write=True) as db:
            customer = db.execute("SELECT number FROM bank_customers WHERE space=? AND name=? AND phone_key=?",
                                  (space, name, phone_key)).fetchone()
            if customer:
                number = customer["number"]
            else:
                number = db.execute("SELECT COALESCE(MAX(number),300)+1 FROM bank_customers WHERE space=?",
                                    (space,)).fetchone()[0]
                db.execute("INSERT INTO bank_customers VALUES(?,?,?,?,?)", (space, number, name, phone_key, phone_tail))
        return f"C{number}", f"A{number}"

    @staticmethod
    def _appointment(db, space, scope, appointment):
        if not scope:
            raise Rejection("NOT_VERIFIED")
        row = db.execute(
            "SELECT id,slot,version,status FROM appointments WHERE space=? AND id=? AND owner=?",
            (space, appointment, scope)).fetchone()
        if is_bank_space(space):
            if row:
                return {**dict(row), "branch": BANK_BRANCH, "service": BANK_SERVICE}
            customer = db.execute("""SELECT 1 FROM bank_customers
                                     WHERE space=? AND 'C'||number=? AND 'A'||number=?""",
                                  (space, scope, appointment)).fetchone()
            if customer or (appointment == BANK_APPOINTMENT and scope == "U100"):
                return {"id": appointment, "slot": None, "version": 0, "status": "unbooked",
                        "branch": BANK_BRANCH, "service": BANK_SERVICE}
        if row:
            return dict(row)
        raise Rejection("FORBIDDEN")

    def read(self, space, scope, appointment):
        with self.store.tx() as db:
            return self._appointment(db, space, scope, appointment)

    @staticmethod
    def _rolling(db, space):
        return db.execute("SELECT 1 FROM bank_rolling_schedule WHERE space=?", (space,)).fetchone() is not None

    def refresh_bank_schedule(self, space):
        day = datetime.fromtimestamp(self.clock(), ZONE).date() + timedelta(days=BANK_HORIZON_DAYS - 1)
        if day.weekday() >= 5:
            day -= timedelta(days=day.weekday() - 4)
        with self.store.tx() as db:
            if not self._rolling(db, space):
                return
            latest = db.execute("SELECT MAX(start) FROM slots WHERE space=?", (space,)).fetchone()[0]
        if not latest or latest[:10] < day.isoformat():
            self.seed(space)

    def _slots(self, db, space, include_slot=None):
        slots = [dict(row) for row in db.execute("""
            SELECT s.id,s.start, s.capacity>COUNT(a.id) AS available FROM slots s
            LEFT JOIN appointments a ON s.space=a.space AND s.id=a.slot AND a.status='active'
            WHERE s.space=? GROUP BY s.id ORDER BY s.start
        """, (space,))]
        if not self._rolling(db, space):
            return slots
        now = datetime.fromtimestamp(self.clock(), ZONE)
        end = now.date() + timedelta(days=BANK_HORIZON_DAYS)
        visible = []
        for slot in slots:
            start = datetime.fromisoformat(slot["start"])
            if now < start and start.date() < end and start.weekday() < 5 and (
                    BANK_FIRST_HOUR <= start.hour < BANK_END_HOUR and start.minute in (0, 30)):
                visible.append(slot)
            elif slot["id"] == include_slot:
                visible.append({**slot, "available": 0})
        return visible

    def slots(self, space, scope, appointment):
        with self.store.tx() as db:
            current = self._appointment(db, space, scope, appointment)
            return self._slots(db, space, current["slot"])

    def execute(self, space, request, fault=None, attempt=1):
        if fault == "before" and attempt == 1:
            raise Unknown("before_commit")
        operation = request["operation_id"]
        fingerprint = digest(request)
        with self.store.tx(write=True) as db:
            prior = db.execute("SELECT digest,data FROM receipts WHERE space=? AND operation=?",
                               (space, operation)).fetchone()
            if prior:
                if prior["digest"] != fingerprint:
                    raise Rejection("IDEMPOTENCY_CONFLICT")
                return json.loads(prior["data"])
            try:
                current = self._appointment(db, space, request["scope"], request["appointment_id"])
                if request["expected_version"] != current["version"]:
                    raise Rejection("VERSION_CONFLICT")
                action, slot = request["action"], request["target_slot"]
                if action in {"book", "change"}:
                    eligible = (current["status"] in {"unbooked", "canceled"} if action == "book"
                                else current["status"] == "active" and slot != current["slot"])
                    if not eligible or (action == "book" and not is_bank_space(space)):
                        raise Rejection("INVALID_ARGUMENT")
                    if not any(s["id"] == slot and s["available"] and s["start"] == request["target_start"]
                               for s in self._slots(db, space)):
                        raise Rejection("SLOT_UNAVAILABLE")
                    status = "active"
                elif (action == "cancel" and current["status"] == "active"
                      and slot is None and request["target_start"] is None):
                    slot, status = current["slot"], "canceled"
                else:
                    raise Rejection("INVALID_ARGUMENT")
                if current["status"] == "unbooked":
                    db.execute("INSERT INTO appointments VALUES(?,?,?,?,1,?)",
                               (space, current["id"], request["scope"], slot, status))
                else:
                    db.execute("UPDATE appointments SET slot=?,status=?,version=version+1 WHERE space=? AND id=?",
                               (slot, status, space, current["id"]))
                outcome = {"status": "succeeded", "result": self._appointment(
                    db, space, request["scope"], current["id"]), "committed_at": iso(self.clock()),
                    "slot_start": request["target_start"] if action in {"book", "change"} else None}
            except Rejection as exc:
                outcome = {"status": "rejected", "code": exc.code}
            receipt = {"operation_id": operation, "receipt_id": ident("rcpt"), **outcome}
            db.execute("INSERT INTO receipts VALUES(?,?,?,?)",
                       (space, operation, fingerprint, encode(receipt)))
        if fault in {"after", "unknown"} and attempt == 1:
            raise Unknown("after_commit")
        return receipt

    def operation(self, space, operation, fault=None):
        if fault == "unknown":
            raise Unknown("query_unavailable")
        with self.store.tx() as db:
            row = db.execute("SELECT data FROM receipts WHERE space=? AND operation=?",
                             (space, operation)).fetchone()
            return json.loads(row["data"]) if row else {"status": "not_found"}

    def handoff(self, space, session, summary):
        with self.store.tx(write=True) as db:
            db.execute("INSERT OR IGNORE INTO handoffs VALUES(?,?,?)",
                       (space, session, encode({"ticket": ident("case")})))
            row = db.execute("SELECT data FROM handoffs WHERE space=? AND session=?", (space, session)).fetchone()
            data = {**json.loads(row["data"]), **summary}
            db.execute("UPDATE handoffs SET data=? WHERE space=? AND session=?", (encode(data), space, session))
            return data
