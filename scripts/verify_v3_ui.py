"""Real browser flow; uses public Esri fixture from output/v3-demo/source.json."""
import json
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1]
output = root / "output" / "ui-review"
output.mkdir(parents=True, exist_ok=True)
errors, results = [], {}
with sync_playwright() as pw:
    browser = pw.chromium.launch(channel="msedge", headless=True)
    context = browser.new_context(viewport={"width": 1600, "height": 1000})
    page = context.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("response", lambda r: errors.append(f"HTTP {r.status}: {r.url.split('?')[0]}") if r.status >= 400 and "/api/v3/" in r.url else None)
    page.goto("http://127.0.0.1:8000/", wait_until="domcontentloaded")
    page.get_by_label("遥感影像问题").wait_for()
    page.locator('input[type="file"]').set_input_files(root / "output/v3-demo/botlek.jpg")
    page.get_by_label("附件名称").wait_for(timeout=20000)
    results["upload_attachment_visible"] = True
    page.get_by_label("附件名称").fill("Botlek 港区")
    page.get_by_label("遥感影像问题").fill("这幅港区影像中，码头和仓储区分别在哪里？请查看局部，标出两个位置，并解释它们的空间关系。")
    start = time.monotonic()
    page.get_by_label("发送问题").click()
    page.locator(".message.user").wait_for(timeout=15000)
    results["send_ack_ms"] = round((time.monotonic() - start) * 1000)
    page.screenshot(path=str(output / "v3-live-running.png"))
    print("Uploaded real imagery, message acknowledged", flush=True)
    try:
        page.locator(".message.assistant").wait_for(timeout=360000)
        results["answer_received"] = True
        results["answer_text"] = page.locator(".message.assistant .message-content").last.inner_text()
        evidence = page.locator(".message.assistant .evidence-link")
        results["evidence_count"] = evidence.count()
        if evidence.count():
            evidence.first.click()
            page.locator(".pixelCanvas canvas").first.wait_for(timeout=15000)
            results["evidence_canvas_opened"] = True
        page.screenshot(path=str(output / "v3-live-answer.png"))
        page.reload(wait_until="domcontentloaded")
        page.locator(".message.assistant").wait_for(timeout=20000)
        results["refresh_restores_conversation"] = True
        page.get_by_label("遥感影像问题").fill("刚才标出的仓储区与水面是什么位置关系？只用图像方位简洁回答，并引用刚才的位置。")
        page.get_by_label("发送问题").click()
        page.locator(".message.assistant").nth(1).wait_for(timeout=240000)
        results["followup_answer_received"] = True
        results["followup_text"] = page.locator(".message.assistant .message-content").last.inner_text()
        page.screenshot(path=str(output / "v3-live-followup.png"))
    except Exception as e:
        results["answer_received"] = False
        results["answer_wait_error"] = type(e).__name__
        page.screenshot(path=str(output / "v3-live-timeout.png"))
    (output / "v3-ui-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    page.set_viewport_size({"width":390,"height":844})
    page.get_by_role("button", name="对话", exact=True).click()
    page.screenshot(path=str(output / "v3-mobile-chat.png"))
    page.get_by_role("button", name="画布", exact=True).click()
    page.screenshot(path=str(output / "v3-mobile-canvas.png"))
    results["mobile_tabs"] = True
    results["horizontal_overflow"] = page.evaluate("document.documentElement.scrollWidth > innerWidth")
    results["browser_errors"] = errors
    browser.close()
(output / "v3-ui-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({k:v for k,v in results.items() if k != "answer_text"}, ensure_ascii=False), flush=True)
