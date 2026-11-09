"""Exercise the real cloud providers through a Chromium WebRTC session.

The microphone source is a synthetic WAV of Mandarin utterances.
This is an integration check and does not measure real acoustic performance.
"""
import argparse
import asyncio
import json
import tempfile
import wave
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from time import time
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright

from services.business import ZONE
from services.cloud_setup import validate_targets
from services.config import Settings
from services.providers import synthesize
from services.storage import iso

def conversation_script(scenario):
    intake = ("我叫林小满。", "手机号是幺九九零零零零零九九幺。", "嗯，是的是的。")
    today = datetime.now(ZONE).date()
    day = today + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    spoken_date = f"{day.month}月{day.day}号"
    slot = f"S{day:%Y%m%d}1530"
    date_request = f"{spoken_date}下午三点半有空吗？"
    if scenario == "cancel":
        return slot, "下午3点半", intake + ("我不想办了，你帮我把这个预约取消掉吧。", "取消了吗？", "确认取消。")
    scripts = {
        "date": (slot, "下午3点半", ("我选第一个可用时段。", "这些时间我都没空。", date_request,
                                   "对了，明天会下雨吗？", "嗯，可以可以可以。")),
        "relative": (f"S{day:%Y%m%d}1430", "下午2点半", (f"{spoken_date}下午两点可以吗？",
                     "刚才那个时间能不能晚一点？", "可以，麻烦您帮我约一下。")),
        "filtered": (slot, "下午3点半", ("工作日下午有空吗？", "我选第二个。", date_request, "好，就这个时间。")),
    }
    slot, time_label, turns = scripts[scenario]
    return slot, time_label, intake + ("到店要带什么材料？",) + turns


def seed_appointment(page, url, slot):
    desk = page.evaluate("localStorage.getItem('zhiyu.desk')")
    opened = page.request.post(url + "/api/sessions", headers={"X-Desk-Token": desk}, data={
        "scenario": "bank_card", "customer_intake": True,
    })
    assert opened.ok
    data = opened.json()
    path = url + "/api/sessions/" + data["session_id"]
    auth = {"Authorization": "Bearer " + data["token"]}
    for turn, text in enumerate(("我叫林小满，手机号是19900000991", "我是本人，我要办卡", slot, "确认预约")):
        response = page.request.post(path + "/turns", headers=auth, data={
            "text": text, "event_id": "evt_" + uuid4().hex, "expected_turn": turn,
        })
        assert response.ok
    assert response.json()["snapshot"]["appointment"]["slot"] == slot
    assert page.request.post(path + "/close", headers=auth).ok


async def create_audio(path, settings, utterances):
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        output.writeframes(b"\0" * (48000 * 5))
        async with asyncio.timeout(55):
            for index, utterance in enumerate(utterances):
                audio = bytearray()
                async for chunk in synthesize(settings.providers["TTS"], settings.voice, utterance):
                    audio.extend(chunk)
                if not audio or len(audio) % 2:
                    raise RuntimeError("Invalid PCM from TTS")
                output.writeframes(audio)
                pause_seconds = 8 if "下雨" in utterance else 6 if index == len(utterances) - 2 else 3
                output.writeframes(b"\0" * (48000 * pause_seconds))


def main():
    parser = argparse.ArgumentParser(description="真实云 API 加 Chromium WebRTC 合成输入验收")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--out", type=Path, default=Path("artifacts/cloud-browser-live.json"))
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--scenario", choices=("date", "relative", "filtered", "cancel"), default="date")
    args = parser.parse_args()
    settings = Settings()
    validate_targets(settings)
    response = httpx.get(args.url + "/health", timeout=5)
    response.raise_for_status()
    health = response.json()
    if not health["microphone"] or any(
        health["providers"][name]["mode"] != "dashscope"
        or health["providers"][name]["model"] != settings.providers[name][2]
        for name in ("ASR", "LLM", "TTS")
    ):
        raise RuntimeError("请先配置并重启云语音版，服务模型应与本地设置相同。")
    errors = []
    cancel = args.scenario == "cancel"
    expected_slot, time_label, utterances = conversation_script(args.scenario)
    with tempfile.TemporaryDirectory(prefix="zhiyu-live-") as folder, ExitStack() as stack:
        wav = Path(folder) / "speech.wav"
        asyncio.run(create_audio(wav, settings, utterances))
        runner = stack.enter_context(sync_playwright())
        browser = runner.chromium.launch(args=[
            "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
            f"--use-file-for-fake-audio-capture={wav}%noloop",
        ])
        try:
            context = browser.new_context(permissions=["microphone"], viewport={"width":1440,"height":940})
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(args.url, wait_until="networkidle")
            if cancel:
                seed_appointment(page, args.url, expected_slot)
            if health["demo_faults"]:
                page.select_option("#fault", "after")
            page.locator("#start").click()
            expect(page.locator("#mic-status")).to_contain_text("麦克风已连接", timeout=15000)
            expect(page.locator("#call-state")).to_have_text("已接通")
            expect(page.locator("#mic-input-hint")).to_have_text("已检测到麦克风输入。", timeout=15000)
            page.locator("#open-current-record").click()
            expect(page.locator("#record-status")).to_have_text("通话中")
            expect(page.locator("#messages")).to_contain_text("到店预约已取消" if cancel else "预约成功", timeout=60000)
            expect(page.locator("#messages")).to_contain_text("预约编号 A301", timeout=8000)
            page.wait_for_function("""() => {
                const credential = JSON.parse(sessionStorage.getItem('zhiyu'));
                return fetch('/api/sessions/' + credential.session_id + '/events', {
                    headers: {Authorization:'Bearer ' + credential.token},
                }).then(r => r.json()).then(events => {
                    const receipt = events.find(e => e.event_type === 'operation.receipt');
                    return receipt && events.some(e => e.event_type === 'audio.tts_first' &&
                        e.turn_id === receipt.turn_id && e.authoritative_sequence > receipt.authoritative_sequence);
                });
            }""", timeout=12000)
            credential = page.evaluate("JSON.parse(sessionStorage.getItem('zhiyu'))")
            trail = page.request.get(args.url + "/api/sessions/" + credential["session_id"] + "/events",
                                     headers={"Authorization":"Bearer " + credential["token"]}).json()
            assert sum(e["event_type"] == "audio.asr_final" for e in trail) >= len(utterances)
            assert any(e["event_type"] == "identity.verified" and
                       e["turn_id"] == 3 and
                       e["payload"].get("method") == "sandbox_spoken_confirmation" for e in trail)
            offers = [e for e in trail if e["event_type"] == "confirmation.offered" and
                      e["payload"]["params"]["target_slot"] == (None if cancel else expected_slot)]
            assert offers and any(e["event_type"] == "assistant.replied" and e["turn_id"] == offers[-1]["turn_id"]
                                  and time_label in e["payload"]["text"] for e in trail)
            if args.scenario == "date":
                assert any(e["event_type"] == "assistant.replied" and "哪天下午方便" in e["payload"]["text"] for e in trail)
                assert any(e["event_type"] == "assistant.replied" and "天气这边查不了" in e["payload"]["text"]
                           and time_label in e["payload"]["text"] for e in trail)
            assert sum(e["event_type"] == "operation.sent" for e in trail) == 1
            receipt = next(e["payload"] for e in trail if e["event_type"] == "operation.receipt")
            assert (receipt["result"]["slot"], receipt["result"]["version"]) == (expected_slot, 2 if cancel else 1)
            if cancel:
                assert receipt["result"]["status"] == "canceled"
                if health["demo_faults"]:
                    assert any(e["event_type"] == "operation.unknown" for e in trail)
                assert any(e["event_type"] == "assistant.replied" and "还没有取消" in e["payload"]["text"] for e in trail)
            else:
                assert any(e["event_type"] == "confirmation.revoked" for e in trail)
                assert any(e["event_type"] == "bank.guidance" and e["payload"]["topic"] == "documents" for e in trail)
            page.locator(".nav-item[data-view=booking]").click()
            expect(page.locator("#booking-table")).to_contain_text("Visa 借记卡到店办理")
            expect(page.locator("#booking-table")).to_contain_text("林小满 · 尾号 0991")
            expect(page.locator("#booking-operations")).to_contain_text("取消预约" if cancel else "新建预约")
            if cancel:
                expect(page.locator("#booking-table")).to_contain_text("已取消")
            page.locator(".nav-item[data-view=call]").click()
            expect(page.locator("#call-clock")).not_to_have_text("00:00")
            if args.screenshot:
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=args.screenshot, full_page=True)
            page.locator("#mic").click()
            expect(page.locator("#call-state")).to_have_text("已接通 · 静音")
            expect(page.locator("#mic-input-hint")).to_contain_text("已静音")
            page.locator("#mic").click()
            expect(page.locator("#call-state")).to_have_text("已接通")
            page.locator("#close").click()
            expect(page.locator("#stage")).to_have_text("通话已结束")
            expect(page.locator("#call-state")).to_have_text("通话已结束")
            version = browser.version
        finally:
            browser.close()
    assert not errors, errors
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "status":"pass", "mode":"real_cloud_synthetic_microphone", "checked_at":iso(time()),
        "browser":version, "scenario":args.scenario, "models":health["providers"], "events":trail,
        "official_acoustics_measured":False,
    },ensure_ascii=False,indent=2) + "\n",encoding="utf-8")


if __name__ == "__main__":
    main()
