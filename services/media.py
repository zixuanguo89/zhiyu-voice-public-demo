import asyncio
import json
import struct
import time
from contextlib import aclosing
from datetime import datetime

from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from av import AudioResampler

from services.business import Rejection, ZONE
from services.bank import BANK_INTENT_HINT, BANK_SCENARIO, bank_intent
from services.dialogue import parse, redacted
from services.providers import ProviderError, recognize, synthesize
from services.storage import emit


async def submit_text(conversation, language, session, text, event_id=None, expected_turn=None, reliable=True):
    snapshot = conversation.snapshot(session)
    turn = snapshot["turn"] if expected_turn is None else expected_turn
    state, config, verified = conversation.context(session)
    slots, hint = snapshot["slots"], config["intent_prompt"]
    bank = snapshot["scenario"] == BANK_SCENARIO
    if bank:
        current = snapshot["appointment"] or {}
        slots = [slot for slot in slots if slot["available"] and
                 not (current.get("status") == "active" and slot["id"] == current.get("slot"))]
        today = datetime.fromtimestamp(conversation.clock(), ZONE).date()
        action, _ = bank_intent(text, slots, today=today)
        hint = f"{BANK_INTENT_HINT} {hint}"
        if snapshot["offer"]:
            hint += " 正在等客户确认上一个时段。没听清时返回 clarify，不能猜测客户改选。"
    else:
        action, _ = parse(text, slots)
    suggestion = None
    if (verified and reliable and language.kind == "dashscope" and action in {"unrecognized", "clarify"}
            and not snapshot["pending"] and state != "HANDOFF"):
        try:
            suggestion = await language.intent(text, slots, hint, allow_detour=bank)
        except ProviderError as exc:
            with conversation.store.tx(write=True) as db:
                emit(db, session, turn, "provider.failed", {"stage": "LLM", "code": str(exc)})
    applied = conversation.submit(session, text, event_id, turn, suggestion, reliable, auto=False)
    if applied:
        await asyncio.sleep(0)
        conversation.advance(session, force=True)
    return applied


class VoiceCall:
    def __init__(self, conversation, language, settings, session, token, greet):
        self.conversation, self.language, self.settings = conversation, language, settings
        self.session, self.token, self.greet = session, token, greet
        self.audio = asyncio.Queue(maxsize=64)
        self.tasks = set()
        self.reply_task = None
        self.generation = 0
        self.partial = False
        self.pending_reply = None
        self.browser_ready = False
        self.asr_ready = False
        self.closed = False
        self.segment = 0

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def control(self, value):
        if value == "barge":
            self.cut()
        elif value == "ready":
            self.browser_ready = True
            self.ready()
        elif len(value) < 256 and value.startswith("{"):
            try:
                info = json.loads(value)
                if info.get("type") == "timing" and info.get("kind") in {"first_play", "barge_stop"}:
                    milliseconds = info.get("ms")
                    if type(milliseconds) in (float, int) and 0 <= milliseconds <= 60000:
                        self.record("audio.client_timing", {"kind": info["kind"], "ms": milliseconds,
                                                           "source": "browser_estimate"})
            except (ValueError, AttributeError):
                return

    def input_audio(self, chunk):
        if self.audio.full():
            self.audio.get_nowait()
        self.audio.put_nowait(chunk)

    def record(self, kind, payload, turn=None):
        if turn is None:
            turn = self.conversation.snapshot(self.session)["turn"]
        with self.conversation.store.tx(write=True) as db:
            emit(db, self.session, turn, kind, payload)

    def cut(self, cancel=True):
        self.generation += 1
        self.pending_reply = None
        if cancel and self.reply_task and not self.reply_task.done():
            self.reply_task.cancel()
        self.send({"type": "cut", "generation": self.generation})

    async def asr(self):
        try:
            async with asyncio.timeout(self.settings.voice_max_seconds):
                await recognize(self.settings.providers["ASR"], self.audio, self.on_text, self.mark_asr_ready)
        except TimeoutError:
            self.send({"type": "failure", "message": "已达到单次语音时长上限，可重新连接或用文本继续。"})
        except ProviderError as exc:
            self.record("provider.failed", {"stage": "ASR", "code": str(exc)})
            self.send({"type": "failure", "message": "语音识别已断开。请重连或使用文本办理。"})
        finally:
            self.send({"type": "asr_ended"})

    def mark_asr_ready(self):
        self.asr_ready = True
        self.ready()

    def ready(self):
        if not (self.browser_ready and self.asr_ready):
            return
        self.send({"type": "ready"})
        if self.greet:
            self.greet = False
            snap = self.conversation.snapshot(self.session)
            if snap["turn"] == 0:
                self.reply_task = self.spawn(self.speak(snap["reply"], 0, self.generation))

    async def on_text(self, text, stable, reliable):
        if self.closed:
            return
        try:
            self.conversation.auth(self.session, self.token)
        except Rejection:
            await self.close()
            return
        if not self.partial:
            self.cut()
            self.partial = True
            self.segment += 1
            self.partial_intake = self.conversation.snapshot(self.session)["intake_stage"]
        if not stable:
            self.send({"type": "partial", "text": "正在收集预约资料…" if self.partial_intake else redacted(text)})
            return
        self.partial = False
        turn = self.conversation.snapshot(self.session)["turn"]
        self.record("audio.asr_final", {"reliable": reliable, "segment": self.segment}, turn)
        self.send({"type": "partial", "text": ""})
        self.reply_task = self.spawn(self.answer(text, reliable, turn, self.generation))

    async def answer(self, text, reliable, turn, generation):
        try:
            if getattr(self.settings, "public_demo", False) and turn >= 40:
                self.send({"type": "failure", "message": "这通演示已达到对话轮次上限。"})
                return
            processed = await submit_text(self.conversation, self.language, self.session, text,
                                          expected_turn=turn, reliable=reliable)
            if processed and generation == self.generation:
                snap = self.conversation.snapshot(self.session)
                if snap["pending"]:
                    self.pending_reply = (snap["turn"], generation)
                self.send({"type": "state"})
                await self.speak(snap["reply"], snap["turn"], generation)
        except Rejection:
            self.send({"type": "failure", "message": "会话已关闭或失效，请开始新的模拟通话。"})

    def follow_up(self, snapshot):
        if self.pending_reply == (snapshot["turn"], self.generation) and not snapshot["pending"]:
            self.cut()
            self.send({"type": "state"})
            self.reply_task = self.spawn(self.speak(snapshot["reply"], snapshot["turn"], self.generation))

    async def speak(self, text, turn, generation):
        sequence = 0
        begin = time.perf_counter()
        self.send({"type": "speech", "generation": generation, "turn": turn})
        buffer = bytearray()
        try:
            async with aclosing(synthesize(self.settings.providers["TTS"], self.settings.voice, text)) as audio:
                async for chunk in audio:
                    buffer.extend(chunk)
                    while len(buffer) >= 960:
                        if not await self.play(buffer[:960], generation, sequence):
                            return
                        del buffer[:960]
                        if sequence == 0:
                            self.record("audio.tts_first", {"ms": round((time.perf_counter()-begin)*1000, 2)}, turn)
                        sequence += 1
            if buffer and len(buffer) % 2 == 0:
                await self.play(buffer, generation, sequence)
            self.send({"type": "speech_end", "generation": generation})
        except ProviderError as exc:
            self.record("provider.failed", {"stage": "TTS", "code": str(exc)}, turn)
            self.send({"type": "failure", "message": "播报暂不可用，请查看文字回复。"})

    async def close(self):
        if self.closed:
            return
        self.closed = True
        tasks = self.tasks - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class MediaCall(VoiceCall):
    def __init__(self, conversation, language, settings, session, token, greet):
        super().__init__(conversation, language, settings, session, token, greet)
        self.peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.channel = None

        @self.peer.on("datachannel")
        def connected(channel):
            if channel.label != "voice" or self.channel is not None:
                channel.close()
                return
            self.channel = channel

            @channel.on("message")
            def message(value):
                if isinstance(value, str):
                    self.control(value)

        @self.peer.on("track")
        def track_added(track):
            if track.kind == "audio":
                self.spawn(self.pump(track))
                self.spawn(self.asr())

        @self.peer.on("connectionstatechange")
        async def state():
            if self.peer.connectionState in {"failed", "closed"} and not self.closed:
                await self.close()

    def send(self, value):
        if self.channel and self.channel.readyState == "open":
            self.channel.send(json.dumps(value, ensure_ascii=False))

    async def offer(self, sdp):
        await self.peer.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
        await self.peer.setLocalDescription(await self.peer.createAnswer())
        return self.peer.localDescription.sdp

    async def pump(self, track):
        resample = AudioResampler(format="s16", layout="mono", rate=16000)
        try:
            while not self.closed:
                frame = await track.recv()
                for converted in resample.resample(frame):
                    chunk = bytes(converted.planes[0])[: converted.samples * 2]
                    self.input_audio(chunk)
        except MediaStreamError:
            self.input_audio(None)

    async def play(self, chunk, generation, sequence):
        if generation != self.generation or not self.channel or self.channel.readyState != "open":
            return False
        # Keep at most a small amount of unsent audio in SCTP. Every packet is generation-tagged.
        while self.channel.bufferedAmount > 4800:
            await asyncio.sleep(0.01)
            if generation != self.generation or self.channel.readyState != "open":
                return False
        self.channel.send(struct.pack("<II", generation, sequence) + chunk)
        return True

    async def close(self):
        if not self.closed:
            await super().close()
            await self.peer.close()


class SocketCall(VoiceCall):
    def __init__(self, conversation, language, settings, session, token, greet, socket):
        super().__init__(conversation, language, settings, session, token, greet)
        self.socket = socket
        self.output = asyncio.Queue(maxsize=64)

    def send(self, value):
        if not self.closed and not self.output.full():
            self.output.put_nowait(json.dumps(value, ensure_ascii=False))

    async def write(self):
        while not self.closed:
            value = await self.output.get()
            if isinstance(value, str):
                await self.socket.send_text(value)
            else:
                await self.socket.send_bytes(value)

    async def play(self, chunk, generation, sequence):
        while not self.closed and generation == self.generation:
            if self.output.qsize() < 12:
                self.output.put_nowait(struct.pack("<II", generation, sequence) + chunk)
                return True
            await asyncio.sleep(0.01)
        return False
