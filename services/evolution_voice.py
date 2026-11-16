"""Replay the same privately recorded PCM fixtures through the real voice runtime."""

import asyncio
import hashlib
import json
import math
import time
import wave
from array import array
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from services.evolution_eval import isolated_conversation, score
from services.media import VoiceCall


def validation_cases(manifest):
    manifest = Path(manifest).resolve()
    root = manifest.parent
    cases = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("EMPTY_VOICE_VALIDATION")
    ids, audio_hashes = set(), set()
    for case in cases:
        if case["id"] in ids or case["source"] in {"synthetic-generator", "development-author"}:
            raise ValueError("NON_INDEPENDENT_VOICE_VALIDATION")
        ids.add(case["id"])
        repeats = case["recordings"]
        if len(repeats) != 3 or any(len(turns) != len(case["turns"]) for turns in repeats):
            raise ValueError("VOICE_VALIDATION_REQUIRES_THREE_PAIRED_RECORDINGS")
        if type(case.get("interrupt_turn")) is not int or not 1 <= case["interrupt_turn"] < len(case["turns"]):
            raise ValueError("MISSING_VOICE_INTERRUPTION_TURN")
        case["audio_hashes"] = []
        for index, turns in enumerate(repeats):
            for turn, relative in enumerate(turns):
                path = (root / relative).resolve()
                if not path.is_relative_to(root) or path.suffix.lower() != ".wav" or path.stat().st_size > 2_000_000:
                    raise ValueError("INVALID_VOICE_FIXTURE_PATH")
                content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                if content_hash in audio_hashes:
                    raise ValueError("DUPLICATE_VOICE_RECORDING")
                audio_hashes.add(content_hash)
                case["audio_hashes"].append(content_hash)
                case["recordings"][index][turn] = str(path)
    return cases


class VoiceProbe(VoiceCall):
    def __init__(self, *args):
        super().__init__(*args)
        self.connected = asyncio.Event()
        self.started = asyncio.Event()
        self.ended = asyncio.Event()
        self.first_sample = None
        self.playing = False
        self.previous_turn = -1
        self.playing_turn = -1
        self.interrupted_at = None

    def send(self, value):
        kind = value["type"]
        if kind == "ready":
            self.connected.set()
        elif kind == "cut" and value["reason"] == "speech" and self.playing:
            self.interrupted_at = time.perf_counter()
        elif kind == "speech":
            self.playing_turn = value["turn"]
        elif kind == "speech_end":
            self.ended.set()

    async def play(self, frame, generation, sequence):
        if generation != self.generation:
            return False
        samples = array("h", frame)
        if (self.playing_turn > self.previous_turn and self.first_sample is None
                and max(map(abs, samples), default=0) >= 160):
            self.first_sample = time.perf_counter()
            self.started.set()
        self.playing = True
        try:
            await asyncio.sleep(len(frame) / 48000)
        finally:
            self.playing = False
        return generation == self.generation

    async def utterance(self, filename, *, interrupt=False, leave_playing=False):
        if interrupt and not self.playing:
            raise ValueError("VOICE_INTERRUPTION_NOT_EXERCISED")
        self.interrupted_at = None
        self.previous_turn = self.conversation.snapshot(self.session)["turn"]
        self.started.clear()
        self.ended.clear()
        self.first_sample = None
        voiced_start = None
        voiced_end = None
        with wave.open(filename, "rb") as recording:
            if (recording.getnchannels(), recording.getsampwidth(), recording.getframerate()) != (1, 2, 16000):
                raise ValueError("VOICE_FIXTURE_NEEDS_MONO_PCM_16KHZ")
            while frame := recording.readframes(320):
                if max(map(abs, array("h", frame)), default=0) >= 160:
                    voiced_start = voiced_start or time.perf_counter()
                    voiced_end = time.perf_counter() + len(frame) / 32000
                self.input_audio(frame)
                await asyncio.sleep(len(frame) / 32000)
        if voiced_end is None:
            raise ValueError("VOICE_FIXTURE_IS_SILENT")
        async def silence():
            for _ in range(100):
                self.input_audio(b"\0" * 640)
                await asyncio.sleep(0.02)

        padding = asyncio.create_task(silence())
        try:
            await asyncio.wait_for(self.started.wait(), 12)
            if not leave_playing:
                await asyncio.wait_for(self.ended.wait(), 25)
        except TimeoutError:
            return None, None
        finally:
            padding.cancel()
            await asyncio.gather(padding, return_exceptions=True)
        interruption = ((self.interrupted_at - voiced_start) * 1000
                        if interrupt and self.interrupted_at and self.interrupted_at >= voiced_start else None)
        return max(0, (self.first_sample - voiced_end) * 1000), interruption


async def replay(case, config, language, settings, repeat):
    with TemporaryDirectory(prefix="zhiyu-voice-eval-") as folder:
        conversation = isolated_conversation(folder, config)
        session, token = conversation.open(desk="desk_" + uuid4().hex + uuid4().hex,
                                           scenario="bank_card", customer_intake=True)
        probe = VoiceProbe(conversation, language, settings, session, token, False)
        probe.browser_ready = True
        probe.spawn(probe.asr())
        timings, interruptions = [], []
        try:
            await asyncio.wait_for(probe.connected.wait(), 12)
            for _ in range(25):
                probe.input_audio(b"\0" * 640)
                await asyncio.sleep(0.02)
            for index, audio in enumerate(case["recordings"][repeat]):
                latency, stopped = await probe.utterance(audio, interrupt=index == case["interrupt_turn"],
                                                         leave_playing=index + 1 == case["interrupt_turn"])
                timings.append(latency)
                if stopped is not None:
                    interruptions.append(stopped)
                if latency is None:
                    break
            outcome = score(conversation, session, case)
            outcome.update({"timings": timings, "interruptions": interruptions,
                            "audio_events": [{"event": e["event_type"], "turn": e["turn_id"], **e["payload"]}
                                             for e in conversation.evidence(session) if e["event_type"] in
                                             {"audio.asr_final", "audio.tts_first", "provider.failed"}]})
            outcome["success"] &= len(timings) == len(case["turns"]) and None not in timings
            return outcome
        finally:
            await probe.close()


async def paired_voice(cases, baseline, candidate, language, settings):
    left, right = [], []
    for index, case in enumerate(cases):
        left.append([])
        right.append([])
        for repeat in range(3):
            order = ((baseline, left), (candidate, right))
            if (index + repeat) % 2:
                order = order[::-1]
            for config, rows in order:
                rows[-1].append(await replay(case, config, language, settings, repeat))
    return left, right


def voice_metrics(groups):
    rows = [outcome for group in groups for outcome in group]
    times = sorted(latency for outcome in rows for latency in outcome["timings"] if latency is not None)
    stops = sorted(stop for outcome in rows for stop in outcome["interruptions"])

    def percentile(values, q):
        return values[math.ceil(q * len(values)) - 1] if values else None

    receipt_count = sum(outcome["writes"] for outcome in rows)
    coverage = (sum((outcome["coverage"] or 0) * outcome["writes"] for outcome in rows) / receipt_count
                if receipt_count else None)
    return {"success": sum(row["success"] for row in rows) / len(rows), "safe": all(row["safe"] for row in rows),
            "p50": percentile(times, 0.50), "p95": percentile(times, 0.95),
            "barge_p95": percentile(stops, 0.95) if len(stops) == len(rows) else None,
            "coverage": coverage, "writeback": coverage,
            "no_response": sum(latency is None for outcome in rows for latency in outcome["timings"])}
