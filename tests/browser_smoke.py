"""Run against a local instance with ENABLE_DEMO_FAULTS=1 and an admin token."""
import argparse
import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--out", type=Path, default=Path("artifacts/browser-qa"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    secret = os.environ["ADMIN_TOKEN"]
    errors = []
    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page(viewport={"width":1440, "height":900})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.url, wait_until="networkidle")
        expect(page.locator("#environment")).to_have_text("文本模式 · 业务沙箱")
        page.select_option("#fault","after")
        page.locator("#text-start").click()
        expect(page.locator("#call-state")).to_have_text("文字通话中")
        def say(words):
            page.fill("#utterance", words)
            page.locator("#send").click()

        def identify(name, phone, answer):
            expect(page.locator("#call-response")).to_contain_text("您的全名")
            say(f"我叫{name}，手机号是{phone}")
            expect(page.locator("#call-response")).to_contain_text("是您本人办理吗")
            say(answer)

        expect(page.locator("#scenario-title")).to_have_text("Visa 借记卡到店预约")
        identify("林小满", "19900000991", "我是本人，我想预约办理 Visa 借记卡")
        expect(page.locator("#call-response")).to_contain_text("您看哪个方便")
        say("我选第一个可用时段")
        expect(page.locator("#call-response")).to_contain_text("要我帮您约这个时间")
        say("等一下，改成第二个时段")
        expect(page.locator("#call-response")).to_contain_text("再跟您对一下")
        page.locator("#open-current-record").click()
        expect(page.locator("#record-status")).to_have_text("通话中")
        credential = page.evaluate("JSON.parse(sessionStorage.getItem('zhiyu'))")
        headers = {"Authorization":"Bearer "+credential["token"]}
        session_url = args.url+"/api/sessions/"+credential["session_id"]
        current = page.request.get(session_url, headers=headers).json()
        from uuid import uuid4
        sent = page.request.post(session_url+"/turns", headers=headers, data={
            "text":"确认预约","expected_turn":current["turn"],"event_id":"evt_"+uuid4().hex,
        })
        assert sent.ok and sent.json()["processed"]
        expect(page.locator("#messages")).to_contain_text("我还在查这笔预约的结果")
        expect(page.locator("#messages")).to_contain_text("预约成功", timeout=10000)
        page.screenshot(path=args.out / "records.png", full_page=True)
        page.locator("#record-evidence summary").first.click()
        expect(page.locator("#events")).to_contain_text("原确认已失效")
        expect(page.locator("#events")).to_contain_text("查询原操作回执")
        expect(page.locator("#quality")).to_contain_text("回执覆盖率 100%")
        expect(page.locator("#events summary").filter(has_text="提交预约操作")).to_have_count(1)
        page.locator(".nav-item[data-view=call]").click()
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=args.out / "call.png", full_page=True)
        page.locator("#close").click()
        expect(page.locator("#call-state")).to_have_text("通话已结束")
        page.reload(wait_until="networkidle")
        page.locator(".nav-item[data-view=records]").click()
        expect(page.locator("#record-status")).to_have_text("已办结")
        expect(page.locator("#messages")).to_contain_text("预约成功")
        page.locator(".nav-item[data-view=booking]").click()
        expect(page.locator("#booking-table")).to_contain_text("Visa 借记卡到店办理")
        expect(page.locator("#booking-table")).to_contain_text("林小满 · 尾号 0991")
        expect(page.locator("#booking-operations")).to_contain_text("新建预约")
        expect(page.locator("#booking-activity")).to_contain_text("结构化回写")
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=args.out / "booking.png", full_page=True)
        page.locator(".nav-item[data-view=call]").click()
        page.select_option("#fault", "")
        page.locator("#text-start").click()
        expect(page.locator("#call-state")).to_have_text("文字通话中")
        identify("林小雨", "19900000992", "我是本人")
        expect(page.locator("#call-response")).to_contain_text("您看哪个方便")
        say("我选第一个")
        expect(page.locator("#call-response")).to_contain_text("要我帮您约这个时间")
        say("确认预约")
        expect(page.locator("#call-response")).to_contain_text("预约成功")
        page.locator("#close").click()
        expect(page.locator("#call-state")).to_have_text("通话已结束")
        page.locator("#text-start").click()
        identify("林小满", "19900000991", "是本人，我想取消预约")
        expect(page.locator("#cancel-actions")).to_be_visible()
        page.locator("#keep-appointment").click()
        expect(page.locator("#call-response")).to_contain_text("没有变动")
        expect(page.locator("#cancel-actions")).to_be_hidden()
        say("我不想办了，你帮我把这个预约取消掉吧")
        expect(page.locator("#cancel-actions")).to_be_visible()
        page.locator("#confirm-cancel").click()
        expect(page.locator("#call-response")).to_contain_text("已取消")
        expect(page.locator("#cancel-actions")).to_be_hidden()
        page.locator(".nav-item[data-view=booking]").click()
        expect(page.locator("#booking-table tr").filter(has_text="林小满")).to_contain_text("已取消")
        expect(page.locator("#booking-table tr").filter(has_text="林小雨")).to_contain_text("有效")
        expect(page.locator("#booking-operations")).to_contain_text("取消预约")
        page.locator(".strategy-panel summary").first.click()
        page.fill("#admin-key",secret)
        page.get_by_role("button", name="解锁").click()
        expect(page.locator("#active-policy")).to_have_text("当前策略 baseline")
        page.locator("#evolve").click()
        expect(page.locator("#job-status")).to_contain_text("任务结束", timeout=20000)
        expect(page.locator("#policies")).to_contain_text("rejected")
        expect(page.locator("#active-policy")).to_have_text("当前策略 baseline")
        page.screenshot(path=args.out / "strategy.png", full_page=True)
        mobile = browser.new_page(viewport={"width":390,"height":844}, is_mobile=True, has_touch=True)
        mobile.goto(args.url,wait_until="networkidle")
        expect(mobile.get_by_role("heading",name="打电话",exact=True)).to_be_visible()
        assert mobile.evaluate("document.documentElement.scrollWidth === innerWidth")
        mobile.screenshot(path=args.out / "mobile.png", full_page=True)
        version = browser.version
        browser.close()
    assert not errors, errors
    (args.out/"browser-result.json").write_text(json.dumps({
        "mode":"mock", "status":"pass", "browser":version,
        "checks":["spoken-style verification","live transcript","archive reload","booking and activity",
                  "pre-confirm revision","lost receipt","RSI rejection","mobile layout"],
    },ensure_ascii=False,indent=2),encoding="utf-8")


if __name__ == "__main__":
    main()
