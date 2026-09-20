"""Exercise the V3 workbench against the running local server.

This accepts an image only after successful tile responses and an OpenLayers
pixel canvas. A loading placeholder is never treated as a loaded image.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "ui-review"
OUTPUT.mkdir(parents=True, exist_ok=True)
RESULT = OUTPUT / "v3-interactions-results.json"
ASSET = ROOT / "output" / "v3-demo" / "botlek.jpg"


def is_selected(page: Page, name: str, scope: str = ".canvas-tabs") -> bool:
    button = page.locator(scope).get_by_role("button", name=name, exact=True)
    return "selected" in (button.get_attribute("class") or "")


def wait_for_loaded_image(page: Page, tile_responses: list[str]) -> None:
    page.locator(".pixelCanvas canvas").first.wait_for(state="visible", timeout=60000)
    page.wait_for_function(
        """() => Array.from(document.querySelectorAll('.pixelCanvas canvas')).some(
          canvas => canvas.width > 0 && canvas.height > 0
        )""", timeout=60000)
    page.wait_for_function("() => !document.querySelector('.canvas-empty')", timeout=60000)
    # Rendering has started at this point. Give asynchronous tile requests a
    # bounded chance to settle, then require a successful response recorded by
    # the browser event handler.
    for _ in range(60):
        if tile_responses:
            break
        page.wait_for_timeout(100)
    if not tile_responses:
        raise AssertionError("影像查看器出现了 canvas，但没有收到成功的影像瓦片响应")
    page.wait_for_function(
        """() => Array.from(document.querySelectorAll('.pixelCanvas canvas')).some(canvas => {
          try {
            const context = canvas.getContext('2d');
            if (!context || !canvas.width || !canvas.height) return false;
            const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
            const colors = new Set();
            for (let i = 0; i < pixels.length; i += 400) {
              if (pixels[i + 3]) colors.add(`${pixels[i]},${pixels[i + 1]},${pixels[i + 2]}`);
              if (colors.size > 12) return true;
            }
            return false;
          } catch { return false; }
        })""", timeout=60000)


def drag(page: Page, selector: str, start: tuple[float, float], end: tuple[float, float], modifiers: list[str] | None = None) -> None:
    box = page.locator(selector).bounding_box()
    if not box:
        raise AssertionError(f"无法取得 {selector} 的可操作区域")
    sx, sy = box["x"] + box["width"] * start[0], box["y"] + box["height"] * start[1]
    ex, ey = box["x"] + box["width"] * end[0], box["y"] + box["height"] * end[1]
    modifiers = modifiers or []
    for modifier in modifiers:
        page.keyboard.down(modifier)
    try:
        page.mouse.move(sx, sy)
        page.mouse.down()
        page.mouse.move(ex, ey, steps=12)
        page.mouse.up()
    finally:
        for modifier in reversed(modifiers):
            page.keyboard.up(modifier)


def main() -> None:
    if not ASSET.is_file():
        raise SystemExit(f"验收素材不存在：{ASSET}")
    results: dict[str, object] = {}
    errors: list[str] = []
    tile_responses: list[str] = []
    message_posts = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 920})
            page = context.new_page()
            page.set_default_timeout(30000)
            page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))

            def response(response) -> None:
                nonlocal message_posts
                url = response.url.split("?")[0]
                if response.status >= 400 and "/api/v3/" in url:
                    errors.append(f"HTTP {response.status}: {url}")
                if response.status == 200 and "/api/v3/attachments/" in url and "/tiles/" in url:
                    tile_responses.append(url)
                if response.request.method == "POST" and "/messages" in url:
                    message_posts += 1

            page.on("response", response)
            print("open", flush=True)
            page.goto("http://127.0.0.1:8000/", wait_until="domcontentloaded")
            page.get_by_label("遥感影像问题").wait_for()
            page.get_by_role("button", name="在地图上开始", exact=False).click()
            page.get_by_label("卫星影像地图").wait_for(timeout=30000)
            results["new_conversation_map"] = True
            coordinate = page.get_by_label("搜索地点或经纬度")
            coordinate.fill("121.48, 31.23")
            coordinate.press("Enter")
            page.wait_for_timeout(400)
            results["longitude_latitude_input"] = page.locator(".canvas-error").count() == 0
            page.get_by_label("框选区域").click()
            results["region_select_toggle"] = is_selected(page, "框选区域", ".map-tools")
            drag(page, ".basemap", (0.40, 0.42), (0.58, 0.59))
            page.get_by_label("附件名称").wait_for(timeout=60000)
            results["map_drag_creates_attachment"] = page.locator(".attachment-chip").count() == 1

            print("upload", flush=True)
            page.locator('input[type="file"]').set_input_files([ASSET, ASSET])
            page.locator(".attachment-chip").nth(2).get_by_label("附件名称").wait_for(timeout=60000)
            results["two_uploaded_attachments"] = page.locator(".attachment-chip").count() >= 3
            page.get_by_label("发送问题").click()
            page.locator(".asset-selector select").first.wait_for(timeout=60000)
            page.locator(".canvas-tabs").get_by_role("button", name="影像", exact=True).click()
            wait_for_loaded_image(page, tile_responses)
            results["uploaded_image_mode_with_real_tiles"] = is_selected(page, "影像")

            before_windows = page.locator(".observation-panel").count()
            drag(page, ".pixelCanvas", (0.34, 0.36), (0.60, 0.62), ["Shift"])
            page.locator(".observation-panel").wait_for(timeout=60000)
            results["original_image_shift_drag_roi"] = page.locator(".observation-panel").count() > before_windows
            page.locator(".pixelCanvas").hover()
            page.mouse.wheel(0, -280)
            page.wait_for_timeout(700)

            page.locator(".canvas-tabs").get_by_role("button", name="对比", exact=True).click()
            page.locator(".image-panes.compare .pixelCanvas canvas").nth(1).wait_for(state="visible", timeout=60000)
            results["two_image_compare"] = page.locator(".image-panes.compare .pixelCanvas").count() == 2
            page.get_by_role("button", name="滑杆", exact=True).click()
            slider = page.get_by_label("调整影像对比滑杆")
            slider.wait_for(timeout=30000)
            slider.fill("67")
            results["compare_slider"] = slider.input_value() == "67"
            wait_for_loaded_image(page, tile_responses)
            page.screenshot(path=str(OUTPUT / "v3-interactions-desktop.png"), full_page=True)

            textarea = page.get_by_label("遥感影像问题")
            textarea.fill("输入法组合测试")
            sent_before_ime = message_posts
            textarea.evaluate("""node => node.dispatchEvent(new KeyboardEvent('keydown', {
              key: 'Enter', bubbles: true, cancelable: true, isComposing: true
            }))""")
            page.wait_for_timeout(300)
            results["ime_enter_does_not_send"] = message_posts == sent_before_ime and textarea.input_value() == "输入法组合测试"

            print("refresh", flush=True)
            tile_responses.clear()
            page.reload(wait_until="domcontentloaded")
            page.locator(".canvas-tabs").get_by_role("button", name="对比", exact=True).wait_for(timeout=30000)
            results["refresh_restores_canvas_mode"] = is_selected(page, "对比")
            page.locator(".asset-selector select").first.wait_for(timeout=30000)
            results["refresh_restores_attachment"] = page.locator(".asset-selector select").first.input_value() != ""
            results["refresh_restores_roi"] = page.locator(".observation-panel, .evidence-link").count() > 0

            page.set_viewport_size({"width": 390, "height": 844})
            page.get_by_role("button", name="画布", exact=True).click()
            results["narrow_canvas_tab"] = "selected" in (page.get_by_role("button", name="画布", exact=True).get_attribute("class") or "")
            tile_responses.clear()
            page.reload(wait_until="domcontentloaded")
            page.get_by_role("button", name="画布", exact=True).wait_for(timeout=30000)
            results["narrow_refresh_restores_pane"] = "selected" in (page.get_by_role("button", name="画布", exact=True).get_attribute("class") or "")
            wait_for_loaded_image(page, tile_responses)
            results["horizontal_overflow"] = page.evaluate("document.documentElement.scrollWidth > innerWidth")
            page.screenshot(path=str(OUTPUT / "v3-interactions-mobile.png"), full_page=True)
        except Exception as error:
            results["verification_error"] = f"{type(error).__name__}: {error}"
        finally:
            browser.close()
    results["browser_errors"] = errors
    RESULT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    required = ["new_conversation_map", "map_drag_creates_attachment", "two_uploaded_attachments", "uploaded_image_mode_with_real_tiles", "original_image_shift_drag_roi", "two_image_compare", "compare_slider", "ime_enter_does_not_send", "refresh_restores_canvas_mode", "refresh_restores_attachment", "narrow_canvas_tab", "narrow_refresh_restores_pane"]
    if errors or "verification_error" in results or any(results.get(key) is not True for key in required):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
