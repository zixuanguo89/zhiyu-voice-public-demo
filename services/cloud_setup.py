"""Configure and verify a Beijing Model Studio connection without printing credentials."""
import argparse
import asyncio
import json
import os
import re
import tempfile
import time
import warnings
from getpass import GetPassWarning, getpass
from urllib.parse import urlsplit

from av import AudioFrame, AudioResampler

from services.config import Settings
from services.providers import Language, ProviderError, recognize, synthesize
from services.storage import ROOT, iso

SAMPLE = "您好，我想查询服务预约。"
MODELS = {"ASR":"paraformer-realtime-v2", "LLM":"qwen-plus", "TTS":"cosyvoice-v2"}


def cloud_settings(key, workspace):
    if not re.fullmatch(r"sk-[A-Za-z0-9._-]{8,240}", key):
        raise ValueError("密钥格式无效，请复制百炼 API Key。")
    if workspace and not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,79}", workspace):
        raise ValueError("业务空间 ID 格式不正确，请填写 ID 而非完整网址。")
    host = f"{workspace}.cn-beijing.maas.aliyuncs.com" if workspace else "dashscope.aliyuncs.com"
    ws = f"wss://{host}/api-ws/v1/inference"
    return {"DASHSCOPE_API_KEY": key, "ASR_PROVIDER":"dashscope", "LLM_PROVIDER":"dashscope",
            "TTS_PROVIDER":"dashscope", "ASR_ENDPOINT":ws, "TTS_ENDPOINT":ws,
            "LLM_ENDPOINT":f"https://{host}/compatible-mode/v1/chat/completions",
            "ASR_MODEL":MODELS["ASR"], "LLM_MODEL":MODELS["LLM"], "TTS_MODEL":MODELS["TTS"],
            "TTS_VOICE":"longxiaochun_v2", "ASR_API_KEY":"", "LLM_API_KEY":"", "TTS_API_KEY":""}


def write_config(path, values):
    original = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    keep = [line for line in original if line.partition("=")[0].strip() not in values]
    lines = [*keep, "", "# Model Studio Beijing", *(f"{key}={value}" for key, value in values.items())]
    handle, temporary = tempfile.mkstemp(prefix=".env.cloud-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            file.write("\n".join(lines).lstrip("\n") + "\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def resample(pcm):
    frame = AudioFrame(format="s16", layout="mono", samples=len(pcm) // 2)
    frame.sample_rate = 24000
    frame.planes[0].update(pcm)
    converter = AudioResampler(format="s16", layout="mono", rate=16000)
    return b"".join(bytes(f.planes[0])[: f.samples * 2] for f in
                    [*converter.resample(frame), *converter.resample(None)])


async def check(settings):
    stages = {}

    async def step(name, operation):
        start = time.perf_counter()
        try:
            async with asyncio.timeout(18):
                result, details = await operation()
            stages[name] = {"status":"pass", "elapsed_ms":round((time.perf_counter()-start)*1000), **details}
            return result
        except (ProviderError, TimeoutError) as exc:
            stages[name] = {"status":"failed", "code":str(exc) if isinstance(exc, ProviderError) else "TIMEOUT"}
            return None

    async def tts():
        samples = bytearray()
        first = None
        start = time.perf_counter()
        async for chunk in synthesize(settings.providers["TTS"], settings.voice, SAMPLE):
            if first is None:
                first = round((time.perf_counter()-start)*1000)
            samples.extend(chunk)
            if len(samples) > 480000:
                raise ProviderError("TTS_SAMPLE_TOO_LARGE")
        if len(samples) < 4800 or len(samples) % 2 or not any(samples):
            raise ProviderError("TTS_INVALID_PCM")
        return bytes(samples), {"first_pcm_ms":first, "audio_seconds":round(len(samples)/48000, 3)}

    async def asr(pcm):
        stable = []
        audio = asyncio.Queue()

        async def on_text(text, final, reliable):
            if final and reliable:
                stable.append(text)

        async def feed():
            resampled = b"\0" * 3200 + resample(pcm) + b"\0" * 19200
            for offset in range(0, len(resampled), 3200):
                await audio.put(resampled[offset:offset+3200])
                await asyncio.sleep(0.1)
            await audio.put(None)

        producer = asyncio.create_task(feed())
        try:
            await recognize(settings.providers["ASR"], audio, on_text)
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
        transcript = "".join(stable)
        if "查询" not in transcript or "预约" not in transcript:
            raise ProviderError("ASR_EXPECTED_TRANSCRIPT_MISSING")
        return transcript, {"transcript":transcript}

    async def llm():
        result = await Language(settings).intent(SAMPLE, [], "")
        if result != {"intent":"query"}:
            raise ProviderError("LLM_INTENT_MISMATCH")
        return result, result

    async def probe_asr():
        queue = asyncio.Queue()
        queue.put_nowait(b"\0" * 3200)
        queue.put_nowait(None)

        async def ignore_transcript(*_):
            return

        await recognize(settings.providers["ASR"], queue, ignore_transcript)
        return True, {}

    audio = await step("TTS", tts)
    if audio:
        await step("ASR", lambda: asr(audio))
    else:
        if await step("ASR", probe_asr):
            stages["ASR"] = {"status":"blocked", "code":"TTS_TEST_AUDIO_UNAVAILABLE", "task_started":True}
    await step("LLM", llm)
    return {
        "checked_at":iso(time.time()), "kind":"cloud_api_preflight", "region":"cn-beijing",
        "status":"pass" if all(item["status"] == "pass" for item in stages.values()) else "failed",
        "models":{name:settings.providers[name][2] for name in MODELS}, "stages":stages,
        "browser_latency_measured":False,
    }


def validate_targets(settings):
    for name, (kind, url, _, _) in settings.providers.items():
        if kind != "dashscope":
            raise ValueError(f"{name} 尚未配置为百炼。")
        host = urlsplit(url).hostname or ""
        if host != "dashscope.aliyuncs.com" and not host.endswith(".cn-beijing.maas.aliyuncs.com"):
            raise ValueError(f"{name} 不属于百炼北京域名。请核对地域及业务空间。")


def main():
    parser = argparse.ArgumentParser(description="智语外呼百炼北京配置与联通检测")
    parser.add_argument("--check-only", action="store_true", help="仅验证已保存的配置")
    args = parser.parse_args()
    try:
        values = None
        if not args.check_only:
            print("百炼华北2（北京）配置。将执行一次短句语音合成、识别和意图测试，计入账户额度。")
            print("密钥仅在本机输入，验证通过后保存到项目 .env。")
            with warnings.catch_warnings():
                warnings.simplefilter("error", GetPassWarning)
                key = getpass("粘贴 API Key（输入不可见）: ").strip()
            workspace = input("业务空间 ID（可选，直接回车使用兼容域名）: ").strip().lower()
            values = cloud_settings(key, workspace)
            conflicts = [name for name, value in values.items() if name in os.environ and os.environ[name] != value]
            if conflicts:
                raise ValueError("系统环境变量会覆盖文件配置，请先核对：" + "、".join(conflicts))
        settings = Settings(overrides=values)
        validate_targets(settings)
        report = asyncio.run(check(settings))
        settings.artifacts.mkdir(parents=True, exist_ok=True)
        path = settings.artifacts / "cloud-preflight.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for stage, result in report["stages"].items():
            print(f'{stage}  {result["status"]}  {result.get("code", "")}')
        print(f"联通记录 {path}")
        if report["status"] != "pass":
            print("请核对北京地域、模型授权、额度和网络。项目配置未写入。")
            return 1
        if values is not None:
            write_config(ROOT / ".env", values)
            print("配置已保存。启动项目后打开 http://127.0.0.1:8765/，在模拟通话中开启麦克风。")
        return 0
    except (ValueError, GetPassWarning, OSError, EOFError) as exc:
        # Configuration I/O errors and noninteractive getpass must never dump the input.
        detail = str(exc) if isinstance(exc, ValueError) else "请在本机窗口运行，确认项目目录可写。"
        print(f"配置未完成。{detail}")
        return 1
    except KeyboardInterrupt:
        print("\n已退出配置。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
