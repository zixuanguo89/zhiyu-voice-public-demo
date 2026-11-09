import argparse
import hashlib
import html
import json
import platform
import sys
import time
import unittest
from pathlib import Path

from services.storage import ROOT, encode, ident, iso
from tests.test_acceptance import Cases, DESCRIPTIONS, PENDING

SUITES = {
    "unit": ["T06", "T19", "T31", "T35", "T37", "T42"],
    "contract": ["schema_event", "schema_operation", "T12", "T13", "T26", "T29", "T30"],
    "integration": [f"T{i:02}" for i in range(1, 20)] + ["T25", "T28", "T29", "T30", "T31", "T32"],
    "speech": ["T20", "T21", "T22", "T23", "T24", "T25", "T26", "T27"],
    "rsi": [f"T{i:02}" for i in range(34, 43)],
    "regression": list(DESCRIPTIONS) + ["schema_event", "schema_operation"],
    "acceptance": list(DESCRIPTIONS) + ["schema_event", "schema_operation"],
}


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hash():
    files = [path for directory in ("services", "apps", "tests", "contracts", "migrations", "fixtures", "evals")
             for path in (ROOT / directory).rglob("*") if path.is_file() and "__pycache__" not in path.parts]
    hashes = {path.relative_to(ROOT).as_posix(): file_hash(path) for path in sorted(files)}
    return hashlib.sha256(encode(hashes).encode()).hexdigest()


def render(path, title, metrics, cases, manifest):
    rows = []
    for case in cases:
        suffix = (f' <a href="evidence/{case["id"]}.json">原始证据</a>' if case["evidence"] else "")
        rows.append("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in
                    (case["id"], case["label"], case["status"])) +
                    f'<td>{html.escape(case.get("remaining", ""))}{suffix}</td></tr>')
    path.write_text(f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>body{{font:15px/1.6 system-ui,sans-serif;margin:2rem auto;max-width:1080px;padding:0 1rem;color:#19352e}}
table{{width:100%;border-collapse:collapse}}td,th{{text-align:left;border-bottom:1px solid #d7e3dd;padding:.45rem}}
th{{background:#e8f3ee}}code{{overflow-wrap:anywhere}}a{{color:#067464}}</style>
<h1>{title}</h1><p>模式 {manifest["mode"]} · 套件 {manifest["suite"]} · {manifest["finished_at"]}</p>
<p>自动检查 {metrics["automated_passed"]} 通过，{metrics["automated_failed"]} 失败。
仍含 {metrics["external_pending"]} 项需要补齐外部证据。本报告未计入真实云通话或赛事成绩。</p>
<p>源代码摘要 <code>{manifest["source_sha256"]}</code></p>
<p><a href="run_manifest.json">运行清单</a> · <a href="metrics.json">指标 JSON</a> · <a href="cases.jsonl">用例结果</a></p>
<table><thead><tr><th>编号</th><th>验收内容</th><th>自动检查</th><th>待测事项与证据</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></html>""", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="智语外呼可复现本地评测")
    parser.add_argument("--suite", choices=SUITES, default="regression")
    parser.add_argument("--dataset", choices=("R", "H"), default="R")
    parser.add_argument("--policy", choices=("baseline",), default="baseline")
    parser.add_argument("--seed", type=int, choices=(42,), default=42)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    started = time.time()
    out = args.out or ROOT / "artifacts" / "runs" / ident("run")
    out.mkdir(parents=True, exist_ok=False)
    evidence_dir = out / "evidence"
    evidence_dir.mkdir()
    requested = SUITES[args.suite]
    methods = [name for name in requested if hasattr(Cases, "test_" + name) and args.dataset != "H"]
    suite = unittest.TestSuite(Cases("test_" + name) for name in methods)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    errors = {test._testMethodName.removeprefix("test_"): reason.splitlines()[-1]
              for test, reason in [*result.failures, *result.errors]}
    cases = []
    for name in requested:
        path = Cases.evidence.get(name)
        case = {"id": name, "label": DESCRIPTIONS.get(name, "契约校验"),
                "status": "FAIL" if name in errors else "PASS" if name in methods else "PENDING",
                "evidence": bool(path)}
        if args.dataset == "H":
            case["remaining"] = "未提供独立封存集及官方验收环境"
        elif name in PENDING:
            case["remaining"] = PENDING[name]
        if name in errors:
            case["error"] = errors[name]
        if path:
            save(evidence_dir / (name + ".json"), path)
        cases.append(case)
    metrics = {"automated_passed": sum(c["status"] == "PASS" for c in cases),
               "automated_failed": len(errors), "automated_pending": sum(c["status"] == "PENDING" for c in cases),
               "external_pending": sum("remaining" in c for c in cases),
               "real_voice_p50_ms": None, "real_voice_p95_ms": None, "official_acceptance": "pending"}
    manifest = {
        "mode": "mock", "suite": args.suite, "dataset": args.dataset, "policy": args.policy,
        "seed": args.seed, "started_at": iso(started), "finished_at": iso(time.time()),
        "source_sha256": source_hash(), "dependency_lock_sha256": file_hash(ROOT/"requirements.lock"),
        "dataset_sha256": {p.stem: file_hash(p) for p in sorted((ROOT/"fixtures/datasets").glob("*.json"))},
        "policy_sha256": hashlib.sha256(encode({"policy":"baseline","source":"services/dialogue.py",
                                               "file_sha256":file_hash(ROOT/"services/dialogue.py")}).encode()).hexdigest(),
        "config": {"business":"mock","voice":"disabled","model":None,"concurrency":"case-specific"},
        "runtime": {"python":platform.python_version(),"os":platform.system(),"machine":platform.machine()},
        "network": "no cloud requests", "device": "no microphone",
    }
    (out / "cases.jsonl").write_text("".join(encode(c) + "\n" for c in cases), encoding="utf-8")
    save(out / "run_manifest.json", manifest)
    save(out / "metrics.json", metrics)
    render(out / "report.html", "智语外呼本地验收报告", metrics, cases, manifest)
    print(out.resolve())
    return int(bool(errors or args.dataset == "H" or
                    (args.suite in {"speech", "acceptance"} and metrics["external_pending"])))


if __name__ == "__main__":
    sys.exit(main())
