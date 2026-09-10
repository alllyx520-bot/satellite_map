"""UI review screenshots for visual iteration.
Usage: python scripts/shot_ui.py [tag] [home,workbench,design]
Output: output/ui-review/<tag>-<page>.png
"""
import sys
from playwright.sync_api import sync_playwright

PAGES = {
    "home": "http://127.0.0.1:8000/",
    "workbench": "http://127.0.0.1:8000/workbench/",
    "design": "http://127.0.0.1:8000/design/",
}

def main() -> None:
    tag = sys.argv[1] if len(sys.argv) > 1 else "shot"
    only = [p for p in (sys.argv[2].split(",") if len(sys.argv) > 2 else []) if p]
    names = only or list(PAGES)
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, channel="msedge")
        context = browser.new_context(viewport={"width": 1600, "height": 1000}, device_scale_factor=1)
        page = context.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        for name in names:
            try:
                page.goto(PAGES[name], wait_until="networkidle", timeout=45000)
            except Exception:
                pass
            page.wait_for_timeout(2500)
            page.screenshot(path=f"output/ui-review/{tag}-{name}.png")
            print(f"captured {name}")
        browser.close()
    if errors:
        print("PAGE ERRORS:\n" + "\n".join(dict.fromkeys(errors)))

if __name__ == "__main__":
    main()
