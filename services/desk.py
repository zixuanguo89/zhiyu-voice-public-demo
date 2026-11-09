import hashlib
import json
import re

from services.business import Rejection
from services.bank import BANK_BRANCH, BANK_SERVICE, bank_space, is_bank_space
from services.dialogue import SPOKEN_RETRY
from services.storage import events, iso

BUSINESS_EVENTS = (
    "appointment.read", "bank.guidance", "identity.verified", "access.rejected", "intent.updated",
    "confirmation.offered", "confirmation.revoked", "confirmation.accepted",
    "operation.sent", "operation.unknown", "operation.reconciled", "operation.receipt",
    "result.written", "session.written", "handoff.created", "handoff.updated",
)


def desk_key(token):
    if not token or not re.fullmatch(r"desk_[a-f0-9]{64}", token):
        raise Rejection("FORBIDDEN")
    return "desk_" + hashlib.sha256(token.encode()).hexdigest()


def summary(row):
    return {"session_id": row["id"], "state": row["state"], "closed": bool(row["closed"]),
            "customer_name": row["name"], "phone_tail": row["phone_tail"],
            "turns": row["turn"], "created_at": iso(row["created"]), "result": json.loads(row["result"])
            if row["result"] else None}


class CallDesk:
    def __init__(self, conversation):
        self.conversation = conversation
        self.store = conversation.store

    def link(self, desk, session):
        """Adopt a previous call after the API has checked its original call credential."""
        with self.store.tx(write=True) as db:
            row = self.conversation._get(db, session)
            linked = db.execute("SELECT desk FROM desk_calls WHERE session=?", (session,)).fetchone()
            if linked:
                if linked["desk"] != desk:
                    raise Rejection("FORBIDDEN")
                return
            opened = db.execute("SELECT at FROM events WHERE session=? AND type='session.disclosed'",
                                (session,)).fetchone()
            db.execute("INSERT INTO desk_calls VALUES(?,?,?,?)",
                       (session, desk, row["scope"] or "U100", opened[0] if opened else self.conversation.clock()))
            if not row["closed"] and not row["scope"]:
                self.conversation._say(db, row, SPOKEN_RETRY, "VERIFYING")

    def calls(self, desk, before):
        with self.store.tx() as db:
            rows = db.execute("""
                SELECT d.rowid AS cursor,d.created,s.id,s.state,s.turn,s.closed,r.data AS result,i.name,i.phone_tail
                FROM desk_calls d JOIN sessions s ON s.id=d.session
                LEFT JOIN session_results r ON r.session=s.id
                LEFT JOIN customer_intakes i ON i.session=s.id AND i.phone_tail IS NOT NULL
                WHERE d.desk=? AND (? IS NULL OR d.rowid<?) ORDER BY d.rowid DESC LIMIT 41
            """, (desk, before, before)).fetchall()
        return {"items": [summary(row) for row in rows[:40]],
                "next_before": rows[39]["cursor"] if len(rows) > 40 else None}

    def detail(self, desk, session, after):
        with self.store.tx() as db:
            row = db.execute("""
                SELECT d.created,s.id,s.state,s.turn,s.closed,r.data AS result,i.name,i.phone_tail
                FROM desk_calls d JOIN sessions s ON s.id=d.session
                LEFT JOIN session_results r ON r.session=s.id
                LEFT JOIN customer_intakes i ON i.session=s.id AND i.phone_tail IS NOT NULL
                WHERE d.desk=? AND d.session=?
            """, (desk, session)).fetchone()
            if row is None:
                raise Rejection("FORBIDDEN")
            return {"call": summary(row), "events": events(db, session, after)}

    def booking(self, desk):
        branch_space = bank_space(desk)
        self.conversation.business.refresh_bank_schedule(branch_space)
        with self.conversation.business.store.tx() as db:
            appointments = []
            for row in db.execute("""
                SELECT a.id,a.owner,a.slot,a.status,a.version,s.start,a.space,c.name AS customer_name,c.phone_tail
                FROM appointments a JOIN slots s ON s.space=a.space AND s.id=a.slot
                LEFT JOIN bank_customers c ON c.space=a.space AND 'C'||c.number=a.owner
                WHERE a.space IN (?,?)
                ORDER BY a.space=?,a.id
            """, (desk, branch_space, desk)):
                record = dict(row)
                bank = is_bank_space(record.pop("space"))
                appointments.append({**record, "service": BANK_SERVICE if bank else "服务预约",
                                     "branch": BANK_BRANCH if bank else None})
            slots = [
                {**slot, "branch": branch} for space, branch in ((branch_space, BANK_BRANCH), (desk, None))
                for slot in self.conversation.business._slots(db, space)
            ]
            handoffs = [{"session_id": row["session"], **json.loads(row["data"])} for row in db.execute(
                "SELECT session,data FROM handoffs WHERE space IN (?,?) ORDER BY rowid DESC LIMIT 20",
                (desk, branch_space))]
        with self.store.tx() as db:
            operations = [{"operation_id": row["id"], "session_id": row["session"], "state": row["state"],
                           "attempts": row["attempts"], "request": json.loads(row["request"]),
                           "receipt": json.loads(row["receipt"]) if row["receipt"] else None}
                          for row in db.execute("""
                SELECT o.* FROM operations o JOIN desk_calls d ON d.session=o.session
                WHERE d.desk=? ORDER BY o.rowid DESC LIMIT 30
            """, (desk,))]
            placeholders = ",".join("?" for _ in BUSINESS_EVENTS)
            activity = [{"event_type": row["type"], "session_id": row["session"], "turn_id": row["turn"],
                         "occurred_at": iso(row["at"]), "payload": json.loads(row["payload"])}
                        for row in db.execute(f"""
                SELECT e.type,e.session,e.turn,e.at,e.payload FROM events e JOIN desk_calls d ON d.session=e.session
                WHERE d.desk=? AND e.type IN ({placeholders}) ORDER BY e.sequence DESC LIMIT 30
            """, (desk, *BUSINESS_EVENTS))]
        return {"appointments": appointments, "slots": slots, "handoffs": handoffs,
                "operations": operations, "activity": activity}
