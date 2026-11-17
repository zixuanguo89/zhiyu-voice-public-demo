"""Run an autonomous evolution cycle in an isolated appointment sandbox."""

import argparse
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from services.config import Settings
from services.dialogue import BASELINE
from services.evolution import Evolution, dataset, verify_partitions
from services.evolution_eval import isolated_conversation
from services.evolution_voice import replay, validation_cases, voice_metrics
from services.providers import Language


def load_development_cases(path):
    if path is None:
        return []
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not 1 <= len(cases) <= 30:
        raise ValueError("开发用例需要 1 至 30 条记录")
    ids = set()
    fields = {"status", "slot", "writes", "topics", "handoff", "verified", "intake_stage", "phone_tail",
              "reply_contains", "action", "target_slot", "no_offer", "state", "reply_checks"}
    for case in cases:
        if (not isinstance(case, dict) or any(not isinstance(case.get(key), str) or not case[key].strip()
                                              for key in ("id", "family", "entity", "source"))
                or not case["id"].startswith("blind-") or case["id"] in ids):
            raise ValueError("外部开发用例必须有唯一 blind- 编号和来源")
        ids.add(case["id"])
        turns, expected = case.get("turns"), case.get("expected")
        if not isinstance(turns, list) or not 1 <= len(turns) <= 20 or any(
                not isinstance(turn, str) or not turn.strip() or len(turn) > 500 for turn in turns):
            raise ValueError("外部开发用例含无效发言")
        if not isinstance(expected, dict) or set(expected) - fields or type(expected.get("writes")) is not int or (
                not 0 <= expected["writes"] <= len(turns)):
            raise ValueError("外部开发用例需要明确的办理结果和操作次数")
        checks = expected.get("reply_checks", [])
        if not isinstance(checks, list) or any(
                not isinstance(check, dict) or type(check.get("turn")) is not int
                or not 1 <= check["turn"] <= len(turns)
                or any(not isinstance(check.get(key, []), list) or
                       any(not isinstance(term, str) or not term for term in check.get(key, []))
                       for key in ("contains", "excludes")) for check in checks):
            raise ValueError("外部开发用例含无效的逐轮预期")
    return cases


async def run(args):
    overrides = {"EVOLUTION_VOICE_MANIFEST": str(args.validation) if args.validation else ""}
    if args.mock:
        overrides.update({f"{name}_PROVIDER": "mock" for name in ("ASR", "LLM", "TTS")})
    settings = Settings(overrides)
    language = Language(settings)
    if args.holdout:
        if not settings.audio or not args.candidate:
            raise SystemExit("封存评测需要云语音和已冻结的候选证据文件")
        entry = json.loads(args.candidate.read_text(encoding="utf-8"))
        if not args.validation:
            raise SystemExit("请提供已用于候选的独立验证清单，便于核对和封存集没有重叠")
        holdout = validation_cases(args.holdout)
        validation = validation_cases(args.validation)
        verify_partitions({"D": dataset("D") + entry["evidence"]["generated_cases"],
                           "R": dataset("R"), "V": validation, "H": holdout})
        if {h for c in validation for h in c["audio_hashes"]} & {h for c in holdout for h in c["audio_hashes"]}:
            raise SystemExit("封存评测和验证评测共享音频")
        outcomes = [[await replay(case, entry["config"], language, settings, index) for index in range(3)]
                    for case in holdout]
        return {"kind": "sealed_holdout", "policy": entry["id"], "scenarios": len(holdout),
                "metrics": voice_metrics(outcomes), "outcomes": outcomes}
    with TemporaryDirectory(prefix="zhiyu-rsi-") as folder:
        conversation = isolated_conversation(folder, BASELINE)
        evolution = Evolution(conversation, language, settings=settings)
        version = await evolution.generate(lambda stage: print(stage, flush=True),
                                           development_cases=load_development_cases(args.dev_cases))
        return evolution.evidence(version)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--validation", type=Path)
    parser.add_argument("--holdout", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--dev-cases", type=Path, help="脱敏的黑盒测试开发用例 JSON")
    args = parser.parse_args()
    evidence = asyncio.run(run(args))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(evidence.get("status", evidence.get("kind")), args.out.resolve())


if __name__ == "__main__":
    main()
