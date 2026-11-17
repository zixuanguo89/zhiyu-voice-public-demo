"""Send real PCM audio to the configured ASR. Never creates calls or appointments."""

import argparse
import asyncio
import hashlib
import json
import math
import random
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from array import array
from collections import defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory

import av

from services.config import Settings
from services.providers import ProviderError, recognize

ROOT = Path(__file__).resolve().parents[1]
RATE = 16000
FRAME = 3200  # 100 ms
DEFAULT_VOICES = ("Microsoft Huihui Desktop", "Microsoft Kangkang")
CONDITIONS = ("clean", "fast", "pause", "narrowband", "noise15", "noise5")


def normalized(value):
    return "".join(ch for ch in unicodedata.normalize("NFKC", value).casefold() if ch.isalnum())


def edits(expected, produced):
    prior = list(range(len(produced) + 1))
    for row, char in enumerate(expected, 1):
        line = [row]
        for column, other in enumerate(produced, 1):
            line.append(min(line[-1] + 1, prior[column] + 1, prior[column - 1] + (char != other)))
        prior = line
    return prior[-1]


def score(case, transcript):
    ref, heard = normalized(case["text"]), normalized(transcript)
    fields = []
    cursor = 0
    canonical_heard = heard
    for item in case.get("critical_fields", ()):
        def match(form, text, start=0):
            token = normalized(form)
            if not token:
                return None
            digits = r"0-9零〇洞一幺二两三四五六七八九"
            pattern = rf"(?<![{digits}]){re.escape(token)}(?![{digits}])" if token.isdecimal() or re.fullmatch(
                rf"[{digits}]+", token) else re.escape(token)
            return re.compile(pattern).search(text, start)

        positions = [(form, place) for form in item["accepted_texts"] if (place := match(form, heard, cursor))]
        hits = [form for form, _ in positions]
        forbidden = [form for form in item.get("forbidden_texts", ()) if normalized(form) and normalized(form) in heard]
        fields.append({"name": item["name"], "expected": item["value"], "matched": hits, "forbidden": forbidden,
                       "passed": bool(hits) and not forbidden})
        if positions and case["category"] == "correction":
            cursor = min(place.end() for _, place in positions)
        if positions and not forbidden:
            _, heard_form = min(positions, key=lambda item: (item[1].start(), -len(item[1][0])))
            ref_forms = [place for form in item["accepted_texts"] if (place := match(form, ref))]
            if ref_forms:
                ref_form = min(ref_forms, key=lambda item: (item.start(), -len(item[0])))
                current = match(heard_form[0], canonical_heard)
                if current:
                    canonical_heard = canonical_heard[:current.start()] + ref_form[0] + canonical_heard[current.end():]
    return {"edits": edits(ref, heard), "reference_characters": len(ref), "critical_fields": fields,
            "field_normalized_edits": edits(ref, canonical_heard),
            "field_normalized_characters": len(ref),
            "critical_pass": bool(heard) and all(item["passed"] for item in fields)}


def pcm_wav(path):
    def audio_bytes(frames):
        return b"".join(bytes(frame.planes[0])[:frame.samples * 2] for frame in frames)
    with av.open(str(path)) as source:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        return b"".join(audio_bytes(resampler.resample(frame)) for frame in source.decode(audio=0)) + audio_bytes(
            resampler.resample(None))


def augment(pcm, condition, identity):
    samples = array("h", pcm)
    if condition == "narrowband":
        down = av.AudioResampler(format="s16", layout="mono", rate=8000)
        up = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        frame = av.AudioFrame(format="s16", layout="mono", samples=len(samples))
        frame.sample_rate = RATE
        frame.planes[0].update(pcm.ljust(frame.planes[0].buffer_size, b"\0"))
        converted = [f for reduced in down.resample(frame) + down.resample(None) for f in up.resample(reduced)]
        return b"".join(bytes(f.planes[0])[:f.samples * 2] for f in converted + up.resample(None))
    if not condition.startswith("noise"):
        return pcm
    voiced = [value for value in samples if abs(value) > 50]
    rms = math.sqrt(sum(value * value for value in voiced) / max(1, len(voiced)))
    amplitude = rms / (10 ** (int(condition[5:]) / 20))
    generator = random.Random(identity)
    return array("h", [max(-32768, min(32767, round(value + generator.gauss(0, amplitude))))
                       for value in samples]).tobytes()


def voiced_input(args, cases, directory):
    jobs, samples = [], []
    conditions = args.conditions
    if args.wav_dir:
        for case in cases:
            path = args.wav_dir / f'{case["id"]}.wav'
            if not path.is_file():
                raise FileNotFoundError(path)
            clean = pcm_wav(path)
            for condition in conditions:
                samples.append((case, "recorded", condition, augment(clean, condition, case["id"])))
        return samples
    if sys.platform != "win32":
        raise ValueError("自动合成需要 Windows；其他系统请提供 --wav-dir 录音")
    sources = {}
    for voice_idx, voice in enumerate(args.voice or DEFAULT_VOICES):
        for case in cases:
            for condition in conditions:
                style = condition if condition in {"fast", "pause"} else "clean"
                key = voice_idx, case["id"], style
                if key not in sources:
                    pieces = case.get("pause_parts", ()) if style == "pause" else ()
                    if style == "pause" and not pieces:
                        pieces = [part for part in re.split(r"[，,。；;]+", case["text"]) if part]
                    paths = []
                    for offset, part in enumerate(pieces or [case.get("spoken_text", case["text"])]):
                        path = directory / f"{key[0]}-{key[1]}-{key[2]}-{offset}.wav"
                        jobs.append({"voice": voice, "text": part, "rate": 4 if style == "fast" else 0, "output": str(path)})
                        paths.append(path)
                    sources[key] = paths
    job_file = directory / "jobs.json"
    job_file.write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
    shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    if not shell:
        raise RuntimeError("找不到 PowerShell；请使用 --wav-dir 提供音频")
    subprocess.run([shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(Path(__file__).with_name("generate_asr_audio.ps1")), str(job_file)], check=True)
    decoded = {}
    for voice_idx, voice in enumerate(args.voice or DEFAULT_VOICES):
        for case in cases:
            for condition in conditions:
                style = condition if condition in {"fast", "pause"} else "clean"
                key = voice_idx, case["id"], style
                if key not in decoded:
                    decoded[key] = (b"\0" * (RATE * 2 * 7 // 10)).join(pcm_wav(path) for path in sources[key])
                if style == "pause" and len(sources[key]) < 2:
                    continue
                samples.append((case, voice, condition, augment(decoded[key], condition, f"{voice}:{case['id']}")))
    return samples


async def transcribe(provider, pcm):
    buffer = asyncio.Queue(maxsize=12)
    ready = asyncio.Event()
    origin = None
    first_text = None
    finals = {}
    unstable = 0

    async def on_text(text, stable, reliable, *, sentence_id=None):
        nonlocal first_text, unstable
        if text and first_text is None and origin is not None:
            first_text = round((time.perf_counter() - origin) * 1000)
        if stable:
            key = sentence_id if sentence_id is not None else f"undated-{len(finals)}"
            finals[key] = {"text": text, "reliable": reliable, "sentence_id": sentence_id}
        else:
            unstable += 1

    async def send():
        nonlocal origin
        await ready.wait()
        origin = time.perf_counter()
        leading = bytes(RATE // 5 * 2)
        trailing = bytes(RATE // 2 * 2)
        stream = leading + pcm + trailing
        for offset in range(0, len(stream), FRAME):
            await buffer.put(stream[offset:offset + FRAME])
            await asyncio.sleep(0.1)
        await buffer.put(None)

    active = asyncio.create_task(send())
    try:
        await asyncio.wait_for(recognize(provider, buffer, on_text, ready.set, with_sentence=True), 55)
    finally:
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)
    segments = list(finals.values())
    transcript = "".join(segment["text"] for segment in segments)
    return {"transcript": transcript, "segments": segments, "interim_count": unstable, "first_text_ms": first_text}


def summarize(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped["all"].append(row)
        grouped[f"condition:{row['condition']}"].append(row)
        grouped[f"category:{row['category']}"].append(row)
        grouped[f"voice:{row['voice']}"].append(row)
    result = {}
    for name, items in grouped.items():
        successful = [item for item in items if "error" not in item]
        fields = [field for item in successful for field in item["critical_fields"]]
        count = sum(item["reference_characters"] for item in successful)
        field_count = sum(item["field_normalized_characters"] for item in successful)
        result[name] = {"clips": len(items), "provider_errors": len(items) - len(successful),
                        "empty_finals": sum(not normalized(item["transcript"]) for item in successful),
                        "critical_clips_passed": sum(item["critical_pass"] for item in successful),
                        "critical_fields_passed": sum(field["passed"] for field in fields), "critical_fields_total": len(fields),
                        "cer": round(sum(item["edits"] for item in successful) / count, 4) if count else None,
                        "field_normalized_cer": round(sum(item["field_normalized_edits"] for item in successful) /
                                                      field_count, 4) if field_count else None}
    return result


async def evaluate(provider, samples, concurrency):
    limit = asyncio.Semaphore(concurrency)
    completed = 0

    async def evaluate_clip(case, voice, condition, pcm):
        nonlocal completed
        identity = {"id": case["id"], "category": case["category"], "voice": voice, "condition": condition,
                    "audio_seconds": round(len(pcm) / (RATE * 2), 3), "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
                    "reference": case["text"]}
        async with limit:
            try:
                recognized = await transcribe(provider, pcm)
            except (ProviderError, TimeoutError) as error:
                return {**identity, "error": str(error) or type(error).__name__}
            finally:
                completed += 1
                if completed % 16 == 0 or completed == len(samples):
                    print(f"语音识别：{completed}/{len(samples)}", file=sys.stderr, flush=True)
        return {**identity, **recognized, **score(case, recognized["transcript"])}

    return await asyncio.gather(*(evaluate_clip(*sample) for sample in samples))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=ROOT / "fixtures" / "asr_accuracy_cases.json")
    parser.add_argument("--out", type=Path, default=ROOT / "artifacts" / "asr-accuracy.json")
    parser.add_argument("--wav-dir", type=Path, help="Golden WAVs named <fixture id>.wav in a directory.")
    parser.add_argument("--voice", action="append", help="Windows voice name; repeat for multiple voices.")
    parser.add_argument("--conditions", nargs="+", choices=CONDITIONS)
    parser.add_argument("--concurrency", type=int, choices=range(1, 7), default=4)
    parser.add_argument("--ids", nargs="+", help="Run only the listed fixture IDs.")
    parser.add_argument("--min-critical-accuracy", type=float, help="Optional 0–1 acceptance floor across all clips.")
    args = parser.parse_args()
    args.conditions = args.conditions or (["clean"] if args.wav_dir else ["clean", "fast", "pause", "noise15"])
    if args.wav_dir and set(args.conditions).intersection({"fast", "pause"}):
        parser.error("快读/停顿用于自动合成；对真人快读或停顿，请单独录制 WAV，再按 clean 评测")
    if args.min_critical_accuracy is not None and not 0 <= args.min_critical_accuracy <= 1:
        parser.error("--min-critical-accuracy 必须在 0 和 1 之间")
    fixture_bytes = args.fixture.read_bytes()
    fixture = json.loads(fixture_bytes)
    cases = [case for case in fixture["cases"] if not args.ids or case["id"] in args.ids]
    if not cases or (args.ids and len(cases) != len(set(args.ids))):
        parser.error("用例 ID 不存在或清单为空")
    if any(not case["id"].isascii() or not case["id"].replace("-", "").replace("_", "").isalnum() for case in cases):
        parser.error("测试用例 ID 只能使用 ASCII 字母、数字、短横线和下划线")
    provider = Settings().providers["ASR"]
    if provider[0] != "dashscope":
        parser.error("请配置项目的真实云语音识别；此工具不以模拟转写代替实际识别")
    with TemporaryDirectory(prefix="zhiyu-asr-accuracy-") as folder:
        samples = voiced_input(args, cases, Path(folder))
        if not samples:
            parser.error("没有符合所选条件的音频")
        rows = asyncio.run(evaluate(provider, samples, args.concurrency))
    result = {"audio_source": "supplied_wav" if args.wav_dir else "windows_sapi_synthetic", "model": provider[2],
              "sample_rate": RATE, "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
              "limitations": ["合成录音反映受控条件，不代表真人、不同手机或真实通话的识别准确率。",
                              "每条只走 ASR，不提交任何预约。"],
              "summary": summarize(rows), "clips": rows}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    if all("error" in row for row in rows) or (args.min_critical_accuracy is not None and
            result["summary"]["all"]["critical_clips_passed"] / len(rows) < args.min_critical_accuracy):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
