import asyncio
import json
import math
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from websockets.asyncio.server import serve

from services.cloud_setup import SAMPLE, check, cloud_settings, resample, validate_targets, write_config
from services.dialogue import parse
from services.providers import ProviderError, decode_event

TEST_KEY = "sk-configuration-test"


class Cloud(unittest.TestCase):
    def test_speech_error_uses_only_safe_code(self):
        event = {"header":{"event":"task-failed","error_code":"AccessDenied.Unpurchased",
                           "error_message":"response may contain private detail"}}
        with self.assertRaisesRegex(ProviderError, "^AccessDenied\\.Unpurchased$"):
            decode_event(json.dumps(event))

    def test_spoken_slot_codes(self):
        self.assertEqual(parse("不要S二，改到艾斯三。"), ("choose", "S3"))
        self.assertEqual(parse("不要S二改到艾斯三。"), ("choose", "S3"))
        self.assertNotEqual(parse("改到S二或者改到S三。")[0], "choose")
        self.assertEqual(parse("我确认改到 S 三。"), ("confirm", "S3"))
        self.assertNotEqual(parse("我不确认改到 S 三。")[0], "confirm")

    def test_beijing_workspace_and_validation(self):
        values = cloud_settings(TEST_KEY, "llm-project")
        self.assertEqual(values["ASR_ENDPOINT"],
                         "wss://llm-project.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference")
        self.assertTrue(values["LLM_ENDPOINT"].endswith("/compatible-mode/v1/chat/completions"))
        workspace_key = "sk-ws-" + "test.header.payload_signature"
        self.assertEqual(cloud_settings(workspace_key, "")["DASHSCOPE_API_KEY"], workspace_key)
        with self.assertRaises(ValueError):
            cloud_settings(TEST_KEY, "llm-foo.example.org")
        with self.assertRaises(ValueError):
            cloud_settings("invalid-key", "")

    def test_atomic_config_preserves_project_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("PORT=8765\n# keep\nASR_PROVIDER=mock\n", encoding="utf-8")
            values = cloud_settings(TEST_KEY, "")
            write_config(path, values)
            updated = path.read_text(encoding="utf-8")
            self.assertIn("PORT=8765", updated)
            self.assertIn("# keep", updated)
            self.assertNotIn("ASR_PROVIDER=mock", updated)
            self.assertEqual(updated.count("ASR_PROVIDER="), 1)
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_reject_wrong_region(self):
        settings = SimpleNamespace(providers={"ASR":("dashscope","wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference",
                                                     "paraformer-realtime-v2", TEST_KEY)})
        with self.assertRaisesRegex(ValueError, "不属于百炼北京"):
            validate_targets(settings)

    def test_resample_pcm(self):
        pcm = struct.pack("<24000h", *[int(800 * math.sin(n / 20)) for n in range(24000)])
        out = resample(pcm)
        self.assertEqual(len(out), 32000)
        self.assertTrue(any(out))

    def test_preflight_local_protocol(self):
        async def exercise():
            seen = []
            tone = struct.pack("<7200h", *[int(800 * math.sin(n / 20)) for n in range(7200)])

            async def speech(ws):
                run = json.loads(await ws.recv())
                task_id = run["header"]["task_id"]
                assert run["header"]["action"] == "run-task"
                assert ws.request.headers["Authorization"] == "Bearer " + TEST_KEY
                stage = run["payload"]["task"]
                seen.append(stage)

                async def send(event, payload=None):
                    await ws.send(json.dumps({"header":{"task_id":task_id,"event":event}, "payload":payload or {}}))

                await send("task-started")
                if stage == "tts":
                    continued = json.loads(await ws.recv())
                    finished = json.loads(await ws.recv())
                    assert continued["header"]["action"] == "continue-task"
                    assert continued["payload"]["input"]["text"] == SAMPLE
                    assert finished["header"]["action"] == "finish-task"
                    assert run["payload"]["parameters"]["sample_rate"] == 24000
                    await ws.send(tone)
                else:
                    assert run["payload"]["parameters"]["sample_rate"] == 16000
                    frames = []
                    while isinstance(frame := await ws.recv(), bytes):
                        frames.append(frame)
                    assert len(frames[0]) == 3200 and all(len(chunk) <= 3200 for chunk in frames)
                    assert json.loads(frame)["header"]["action"] == "finish-task"
                    await send("result-generated", {"output":{"sentence":{"text":"错误的中间结果","sentence_end":False}}})
                    await send("result-generated", {"output":{"sentence":{"text":SAMPLE,"sentence_end":True}}})
                await send("task-finished")

            def respond(request):
                seen.append("llm")
                assert request.headers["Authorization"] == "Bearer " + TEST_KEY
                assert json.loads(request.content)["response_format"] == {"type":"json_object"}
                return httpx.Response(200, json={"choices":[{"message":{"content":'{"intent":"query"}'}}]})

            client = httpx.AsyncClient
            async with serve(speech, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                endpoint = f"ws://127.0.0.1:{port}"
                settings = SimpleNamespace(voice="longxiaochun_v2", providers={
                    "ASR":("dashscope",endpoint,"paraformer-realtime-v2",TEST_KEY),
                    "TTS":("dashscope",endpoint,"cosyvoice-v2",TEST_KEY),
                    "LLM":("dashscope","https://mock.invalid/chat/completions","qwen-plus",TEST_KEY),
                })
                with patch("services.providers.httpx.AsyncClient",
                           side_effect=lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs)):
                    report = await check(settings)
            self.assertEqual(report["status"], "pass", report)
            self.assertEqual(seen, ["tts","asr","llm"])
            self.assertEqual(report["stages"]["ASR"]["transcript"], SAMPLE)
            self.assertFalse(report["browser_latency_measured"])
            self.assertNotIn(TEST_KEY, json.dumps(report))

        asyncio.run(exercise())
