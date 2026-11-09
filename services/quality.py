import json
from collections import defaultdict

from services.storage import encode, events


def audit(trail, semantic=None):
    groups = defaultdict(list)
    for event in trail:
        groups[event["event_type"]].append(event)
    checks = []

    def add(rule, status, severity, reason, evidence):
        checks.append({"rule_id": rule, "status": status, "severity": severity, "reason": reason,
                       "event_ids": [e["event_id"] for e in evidence],
                       "turn_ids": sorted({e["turn_id"] for e in evidence})})

    opening = groups["session.disclosed"]
    disclosed = any("AI 客服" in e["payload"].get("reply", "") and "预约服务" in e["payload"]["reply"] for e in opening)
    add("disclosure", "PASS" if disclosed else "FAIL", "critical", "说明 AI 客服身份及预约来意", opening)
    replies = groups["assistant.replied"]
    writes = groups["operation.sent"]
    for sent in writes:
        op = sent["payload"]["operation_id"]
        seq = sent["authoritative_sequence"]
        prepared = [e for e in groups["operation.prepared"] if e["payload"]["operation_id"] == op
                    and e["authoritative_sequence"] < seq]
        cf = prepared[0]["payload"]["confirmation_id"] if prepared else None
        accepted = [e for e in groups["confirmation.accepted"] if e["payload"]["confirmation_id"] == cf
                    and e["authoritative_sequence"] < seq]
        offered = [e for e in groups["confirmation.offered"] if e["payload"]["confirmation_id"] == cf
                   and accepted and e["authoritative_sequence"] < accepted[0]["authoritative_sequence"]
                   and e["payload"]["digest"] == accepted[0]["payload"]["digest"]]
        verified = [e for e in groups["identity.verified"] if prepared
                    and e["authoritative_sequence"] < prepared[0]["authoritative_sequence"]
                    and e["payload"]["scope"] == prepared[0]["payload"]["scope"]]
        revoked = [e for e in groups["confirmation.revoked"] if e["payload"]["confirmation_id"] == cf
                   and e["authoritative_sequence"] < seq]
        add("write_authorized", "PASS" if offered and accepted and verified and not revoked else "FAIL",
            "critical", "核验、复述、确认与提交的事件顺序", verified[-1:] + offered + accepted + revoked + [sent])
        receipt = [e for e in groups["operation.receipt"] if e["payload"]["operation_id"] == op]
        result = [e for e in groups["result.written"] if e["payload"]["operation_id"] == op]
        unknown = [e for e in groups["operation.unknown"] if e["payload"]["operation_id"] == op]
        equal = len(receipt) == len(result) == 1 and all(
            result[0]["payload"].get(k) == v for k, v in receipt[0]["payload"].items())
        status = "PASS" if equal else "REVIEW" if unknown and not receipt and not result else "FAIL"
        add("receipt_writeback", status, "critical", "业务回执与结构化回写一致性", [sent] + receipt + result + unknown)
    if not writes:
        add("no_dispatched_write", "PASS", "info", "该通话没有发出预约写请求", [])
    success_by_receipt = {e["payload"].get("receipt_id"): e for e in groups["operation.receipt"]
                          if e["payload"]["status"] == "succeeded"}
    for reply in replies:
        msg = reply["payload"]["text"]
        if "改期成功" in msg or "预约成功" in msg or "已取消。" in msg:
            receipt = success_by_receipt.get(reply["payload"].get("receipt_id"))
            confirmed = (receipt and receipt["payload"]["operation_id"] == reply["payload"].get("operation_id")
                         and receipt["authoritative_sequence"] < reply["authoritative_sequence"])
            add("truthful_success", "PASS" if confirmed else "FAIL", "critical",
                "成功告知须引用已落库的成功回执", ([receipt] if receipt else []) + [reply])
    semantic = semantic or {"status": "REVIEW", "reason": "语义评审待执行"}
    status = "FAIL" if any(c["status"] == "FAIL" for c in checks) else (
        "REVIEW" if semantic["status"] != "PASS" or any(c["status"] == "REVIEW" for c in checks) else "PASS")
    return {"status": status, "rules": checks, "semantic": semantic,
            "coverage": (sum(c["status"] == "PASS" for c in checks if c["rule_id"] == "receipt_writeback")
                         / len(writes)) if writes else None}


def save_report(conversation, session, semantic=None, through=None):
    with conversation.store.tx(write=True) as db:
        trail = events(db, session)
        if semantic and (not trail or trail[-1]["authoritative_sequence"] != through):
            semantic = {"status": "REVIEW", "reason": "评审期间证据已更新，请重新评审"}
        report = audit(trail, semantic)
        db.execute("INSERT OR REPLACE INTO reports VALUES(?,?)", (session, encode(report)))
        return report


def read_report(conversation, session):
    with conversation.store.tx() as db:
        row = db.execute("SELECT data FROM reports WHERE session=?", (session,)).fetchone()
    return json.loads(row[0]) if row else save_report(conversation, session)
