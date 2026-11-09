"""Browser microphone and playback smoke test using synthetic media and mock speech."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8767")
    parser.add_argument("--out", type=Path, default=Path("artifacts/browser-qa"))
    args = parser.parse_args()
    errors = []
    with sync_playwright() as runner:
        browser = runner.chromium.launch(args=["--use-fake-device-for-media-stream",
                                               "--use-fake-ui-for-media-stream"])
        context = browser.new_context(permissions=["microphone"])
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("""
            window.audioCounts = {starts: 0, stoppedEarly: 0};
            const start = AudioBufferSourceNode.prototype.start;
            const stop = AudioBufferSourceNode.prototype.stop;
            AudioBufferSourceNode.prototype.start = function(at) {
                window.audioCounts.starts++;
                this.endsAt = at + this.buffer.duration;
                return start.call(this, at);
            };
            AudioBufferSourceNode.prototype.stop = function() {
                if (this.endsAt > this.context.currentTime) window.audioCounts.stoppedEarly++;
                return stop.call(this);
            };
        """)
        page.goto(args.url, wait_until="networkidle")
        page.select_option("#fault", "after")
        page.locator("#start").click()
        expect(page.locator("#mic-status")).to_contain_text("麦克风已连接", timeout=15000)
        expect(page.locator("#call-state")).to_have_text("已接通")
        page.locator("#open-current-record").click()
        expect(page.locator("#messages")).to_contain_text("我还在查这笔预约的结果", timeout=20000)
        expect(page.locator("#messages")).to_contain_text("预约成功", timeout=20000)
        counts = page.evaluate("window.audioCounts")
        assert counts["starts"] > 0 and counts["stoppedEarly"] > 0, counts
        credential = page.evaluate("JSON.parse(sessionStorage.getItem('zhiyu'))")
        events = page.request.get(args.url+"/api/sessions/"+credential["session_id"]+"/events",
                                  headers={"Authorization":"Bearer "+credential["token"]}).json()
        assert sum(e["event_type"] == "audio.asr_final" for e in events) == 6
        assert sum(e["event_type"] == "operation.sent" for e in events) == 1
        receipt = next(e["payload"] for e in events if e["event_type"]=="operation.receipt")
        assert receipt["result"]["slot"] == "S2" and receipt["result"]["version"] == 1
        browser.close()
    assert not errors, errors
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out/"voice-loopback-result.json").write_text(json.dumps({
        "mode":"mock", "status":"pass", "media":"Chromium WebRTC RTP input and data-channel PCM",
        "audio_counts":counts, "events":events, "mocked":["ASR transcript","TTS waveform"],
    },ensure_ascii=False,indent=2),encoding="utf-8")


if __name__ == "__main__":
    main()
