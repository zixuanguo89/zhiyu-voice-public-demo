import json
import math
import random
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from services.business import Rejection, Sandbox
from services.dialogue import BASELINE, redacted
from services.orchestrator import Conversation
from services.providers import ProviderError
from services.quality import audit
from services.storage import ROOT, digest, encode, ident


def paired_delta(baseline, candidate, seed=42):
    if len(baseline) != len(candidate) or not baseline:
        raise ValueError("Unpaired evaluation")
    differences = []
    for left, right in zip(baseline, candidate):
        if len(left) != len(right) or not left or any(x not in (0, 1) for x in [*left, *right]):
            raise ValueError("Invalid scenario outcomes")
        differences.append((sum(right) - sum(left)) / len(left))
    rng = random.Random(seed)
    n = len(differences)
    samples = sorted(sum(rng.choices(differences, k=n)) / n for _ in range(10000))
    return {"delta": sum(differences) / n, "lower": samples[249], "upper": samples[9749], "scenarios": n}


def gate(report):
    reasons = []
    if report.get("mode") != "real" or report.get("partition") != "V":
        reasons.append("需要真实语音的独立 V 配对评测")
    for field in ("baseline_safe", "candidate_safe", "regression_pass", "isolation_verified"):
        if report.get(field) is not True:
            reasons.append(f"缺少通过证据 {field}")
    if type(report.get("scenarios")) is not int or report["scenarios"] < 200 or report.get("repeats") != 3:
        reasons.append("独立场景或重复次数不足")
    thresholds = {
        "delta": (0.03, 1), "lower": (1e-15, 1), "baseline_p50": (0, 2000), "baseline_p95": (0, 4000),
        "p50": (0, 2000), "p95": (0, 4000), "barge_p95": (0, 300), "baseline_success": (0, 1),
        "baseline_barge_p95": (0, 300), "coverage": (1, 1),
        "baseline_coverage": (1, 1), "no_response": (0, 0), "baseline_no_response": (0, 0),
        "writeback": (0, 1), "baseline_writeback": (0, 1),
    }
    for key, (minimum, maximum) in thresholds.items():
        value = report.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
            reasons.append(f"指标缺失或超出门槛 {key}")
    if not reasons:
        if report["writeback"] < report["baseline_writeback"]:
            reasons.append("回写准确率下降")
        if report["p95"] > 1.10 * report["baseline_p95"]:
            reasons.append("P95 相对基线恶化超过 10%")
    return reasons


def verify_partitions(partitions):
    seen = {}
    for partition, cases in partitions.items():
        for case in cases:
            for key in ("family", "entity", "source"):
                value = (key, case[key])
                if value in seen and seen[value] != partition:
                    raise ValueError(f"Data leakage: {key}")
                seen[value] = partition


def check_config(config):
    return (isinstance(config, dict) and set(config) == set(BASELINE)
            and all(isinstance(v, str) and len(v) <= 240 and v == redacted(v) for v in config.values())
            and config["clarification"].startswith("请")
            and not any(x in config["clarification"] for x in
                        ("成功", "已取消", "已改期", "验证码", "保证", "承诺", "密码", "加微信", "付款", "转账")))


def dataset(partition):
    if partition not in {"D", "R", "V"}:
        raise Rejection("FORBIDDEN")
    return json.loads((ROOT / "fixtures/datasets" / f"{partition}.json").read_text(encoding="utf-8"))


def exercise(cases, config):
    outcomes = []
    with TemporaryDirectory(prefix="zhiyu-eval-") as folder:
        root = Path(folder)
        conversation = Conversation(root / "app", Sandbox(root / "business"))
        with conversation.store.tx(write=True) as db:
            db.execute("UPDATE policies SET config=? WHERE id='baseline'", (encode(config),))
        for case in cases:
            session, _ = conversation.open()
            conversation.submit(session, conversation.business.fixture["customers"]["U100"])
            for text in case["turns"]:
                conversation.submit(session, text)
            snapshot = conversation.snapshot(session)
            report = audit(conversation.evidence(session))
            safe = report["status"] != "FAIL"
            appt = snapshot["appointment"]
            success = (safe and snapshot["state"] == case["state"] and appt["slot"] == case["slot"]
                       and appt["version"] == case.get("version", appt["version"])
                       and appt["status"] == case.get("status", "active")
                       and case.get("expected_phrase", "") in snapshot["reply"])
            outcomes.append({"id": case["id"], "success": success, "safe": safe,
                             "failure": None if success else "clarification" if snapshot["state"] == "OFFERING"
                             else "outcome_mismatch"})
    return outcomes


class Evolution:
    def __init__(self, conversation, language, clock=time.time):
        self.store, self.language, self.clock = conversation.store, language, clock

    def _audit(self, db, policy, action, evidence):
        db.execute("INSERT INTO policy_audit(policy,action,evidence,at) VALUES(?,?,?,?)",
                   (policy, action, encode(evidence), self.clock()))

    def status(self):
        with self.store.tx() as db:
            policies = [{**dict(p), "config": json.loads(p["config"]), "evidence": json.loads(p["evidence"])}
                        for p in db.execute("SELECT * FROM policies ORDER BY created DESC")]
            history = [{**dict(p), "evidence": json.loads(p["evidence"])}
                       for p in db.execute("SELECT * FROM policy_audit ORDER BY id DESC LIMIT 100")]
            return {"active": db.execute("SELECT policy FROM active").fetchone()[0],
                    "policies": policies, "history": history}

    def reserve(self, batch, candidate):
        with self.store.tx(write=True) as db:
            used = db.execute("SELECT COUNT(*) FROM validation_queries WHERE batch=?", (batch,)).fetchone()[0]
            if used >= 5:
                raise Rejection("VALIDATION_BUDGET_EXHAUSTED")
            db.execute("INSERT INTO validation_queries VALUES(?,?,?)", (batch, candidate, self.clock()))

    async def generate(self):
        current = self.status()
        parent = current["active"]
        baseline = next(p["config"] for p in current["policies"] if p["id"] == parent)
        cases = {name: dataset(name) for name in ("D", "R", "V")}
        verify_partitions(cases)
        dev_results = exercise(cases["D"], baseline)
        failed = [r for r in dev_results if not r["success"]]
        policy = ident("policy")
        if not failed:
            raise Rejection("NO_DEVELOPMENT_FAILURES")
        evidence = {"failures": failed, "data_hashes": {k: digest(v) for k, v in cases.items()},
                    "generation": self.language.kind, "model": self.language.model}
        if self.language.kind == "dashscope":
            system = ("根据开发集失败改进客服策略。输出 JSON 对象，只有 clarification 和 intent_prompt 两个字符串。"
                      "clarification 必须以 请 开头，只能提示补充时间，不可承诺结果。禁止变更安全规则。")
            request = {"baseline": baseline, "failures": failed,
                       "cases": [{"id": c["id"], "turns": c["turns"]} for c in cases["D"]
                                 if any(c["id"] == r["id"] for r in failed)]}
            evidence["request"] = {"system": system, "data": request}
            try:
                candidate = await self.language.json(system, encode(request))
            except ProviderError as exc:
                self._save(policy, parent, baseline, "rejected", {**evidence, "reasons": [str(exc)]})
                return policy
        else:
            candidate = {**baseline, "clarification": "请告诉我具体日期和时间，也可以直接选择一个可用时段。"}
        if not check_config(candidate):
            self._save(policy, parent, baseline, "rejected",
                       {**evidence, "invalid_output_hash": digest(candidate), "reasons": ["候选内容超出允许范围"]})
            return policy
        evidence["diff"] = {k: {"from": baseline[k], "to": v} for k, v in candidate.items() if v != baseline[k]}
        self._save(policy, parent, candidate, "evaluating", evidence)
        try:
            self.reserve(digest(cases["V"]), policy)
        except Rejection as exc:
            self._save(policy, parent, candidate, "rejected", {**evidence, "reasons": [exc.code]})
            return policy
        regression = exercise(cases["R"], candidate)
        left, right = exercise(cases["V"], baseline), exercise(cases["V"], candidate)
        comparison = paired_delta([[int(r["success"])] for r in left], [[int(r["success"])] for r in right])
        report = {"mode": "mock", "partition": "V", "repeats": 1, "isolation_verified": True,
                  "baseline_safe": all(r["safe"] for r in left), "candidate_safe": all(r["safe"] for r in right),
                  "regression_pass": all(r["success"] for r in regression), **comparison}
        self._save(policy, parent, candidate, "rejected", {**evidence, "report": report, "reasons": gate(report)})
        return policy

    def _save(self, policy, parent, config, status, evidence):
        with self.store.tx(write=True) as db:
            db.execute("""INSERT INTO policies VALUES(?,?,?,?,?,?)
                          ON CONFLICT(id) DO UPDATE SET status=excluded.status,evidence=excluded.evidence""",
                       (policy, parent, encode(config), status, self.clock(), encode(evidence)))
            self._audit(db, policy, status, evidence)

    def publish(self, policy):
        # Only trusted evaluation code may create an eligible row. HTTP clients cannot submit reports.
        with self.store.tx(write=True) as db:
            row = db.execute("SELECT * FROM policies WHERE id=?", (policy,)).fetchone()
            if not row or row["status"] != "eligible" or not check_config(json.loads(row["config"])):
                raise Rejection("NOT_ELIGIBLE")
            report = json.loads(row["evidence"]).get("report", {})
            if gate(report):
                raise Rejection("NOT_ELIGIBLE")
            previous = db.execute("SELECT status FROM policies WHERE id=?", (row["parent"],)).fetchone()
            if not previous or previous["status"] not in {"stable", "baseline"}:
                raise Rejection("PARENT_NOT_STABLE")
            changed = db.execute("UPDATE active SET policy=? WHERE singleton=1 AND policy=?",
                                 (policy, row["parent"])).rowcount
            if changed != 1:
                raise Rejection("STALE_PARENT")
            db.execute("UPDATE policies SET status='observing' WHERE id=?", (policy,))
            self._audit(db, policy, "published", {"parent": row["parent"], "report_hash": digest(report)})

    def _rollback(self, db, policy, reason):
        row = db.execute("SELECT parent FROM policies WHERE id=?", (policy,)).fetchone()
        if not row or not row["parent"]:
            raise Rejection("NO_ROLLBACK_TARGET")
        if db.execute("UPDATE active SET policy=? WHERE singleton=1 AND policy=?", (row["parent"], policy)).rowcount != 1:
            raise Rejection("STALE_PARENT")
        db.execute("UPDATE policies SET status='rolled_back' WHERE id=?", (policy,))
        self._audit(db, policy, "rolled_back", {"parent": row["parent"], "reason": reason})

    def rollback(self, policy):
        with self.store.tx(write=True) as db:
            self._rollback(db, policy, "admin")

    def observe(self, policy, session, success, critical):
        with self.store.tx(write=True) as db:
            row = db.execute("""SELECT policies.* FROM policies JOIN active ON active.policy=policies.id
                                WHERE policies.id=? AND policies.status='observing'""", (policy,)).fetchone()
            if not row:
                return
            db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?)", (policy, session, int(success), int(critical)))
            observations = db.execute("SELECT success,critical FROM observations WHERE policy=? ORDER BY rowid", (policy,)).fetchall()
            baseline = json.loads(row["evidence"])["report"]["baseline_success"]
            count = len(observations)
            degraded = count >= 40 and all(
                sum(x["success"] for x in observations[start:start+20]) / 20 <= baseline - 0.05
                for start in (count-40, count-20))
            if critical or degraded:
                self._rollback(db, policy, "critical" if critical else "two_degraded_windows")
            elif count >= 50:
                db.execute("UPDATE policies SET status='stable' WHERE id=?", (policy,))
                self._audit(db, policy, "stable", {"observations": count})
