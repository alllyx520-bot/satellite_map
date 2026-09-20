"""Exercise the live local-Python agent through the workbench and verify its files."""
import argparse
import hashlib
import json
from pathlib import Path
import time

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--image", type=Path, default=ROOT / "output/v3-demo/botlek.jpg")
    parser.add_argument("--timeout", type=int, default=360)
    options = parser.parse_args()
    folder = ROOT / "output/ui-review"
    folder.mkdir(parents=True, exist_ok=True)
    report = {"mode": "live_model_local_python", "checks": {}, "errors": []}
    deadline = time.monotonic() + options.timeout
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()
        page.on("pageerror", lambda error: report["errors"].append(str(error)))
        try:
            page.goto(options.base_url, wait_until="domcontentloaded")
            page.get_by_label("遥感影像问题").wait_for(timeout=20000)
            capability = context.request.get(options.base_url + "/api/v3/capabilities").json()
            report["execution"] = capability["execution"]
            assert capability["execution"]["runtime"] == "local"
            assert capability["execution"]["available"] is True
            report["checks"]["local_runtime_ready"] = True
            page.locator('input[type="file"]').set_input_files(options.image)
            page.get_by_label("附件名称").wait_for(timeout=30000)
            question = ("请使用 Python 读取这张影像的实际像元，计算宽高、各波段均值，"
                        "生成一个 statistics.json 文件（使用 width、height、band_means 字段）"
                        "和一张波段均值柱状图。然后结合图像简短解释结果。请直接完成。")
            page.get_by_label("遥感影像问题").fill(question)
            started = time.monotonic()
            with page.expect_response(lambda response: "/messages" in response.url and response.request.method == "POST") as sent:
                page.get_by_label("发送问题").click()
            acknowledgement = sent.value.json()
            report["ack_ms"] = round((time.monotonic() - started) * 1000)
            run_id = acknowledgement["run"]["id"]
            report["run_id"] = run_id
            message_url = sent.value.url
            detail_url = message_url.removesuffix("/messages").removesuffix("/messages/")
            run_url = options.base_url + f"/api/v3/runs/{run_id}"
            while time.monotonic() < deadline:
                response = context.request.get(run_url)
                assert response.ok, response.status
                run_data = response.json()
                if run_data["run"]["status"] not in {"queued", "running", "cancelling"}:
                    break
                page.wait_for_timeout(1500)
            else:
                raise TimeoutError("Live local agent did not finish within the test deadline")
            report["run_status"] = run_data["run"]["status"]
            report["run_error"] = run_data["run"].get("error")
            report["tools"] = [{"name": tool["name"], "status": tool["status"],
                                "runtime": tool["result"].get("runtime"), "error": tool["result"].get("error")}
                               for tool in run_data["tools"]]
            assert report["run_status"] == "completed", report["run_error"]
            successful = [tool for tool in report["tools"] if tool["name"] == "python_analysis"
                          and tool["runtime"] == "local" and not tool["error"]]
            assert successful, "No successful local Python tool call"
            report["checks"]["model_used_local_python"] = True
            detail = context.request.get(detail_url).json()
            executed = [e for e in detail.get("evidence", []) if e["run_id"] == run_id and e["metric"] == "python_analysis"]
            assert executed, "Python execution did not register real evidence"
            evidence_ids = {item["id"] for item in executed}
            answers = [message for message in detail["messages"] if message["role"] == "assistant" and message["run_id"] == run_id]
            assert any(part.get("type") == "evidence_ref" and part.get("id") in evidence_ids
                       for answer in answers for part in answer["parts"]), "Answer did not cite its executed calculation"
            report["checks"]["answer_cites_python_evidence"] = True
            artifacts = [artifact for artifact in detail["artifacts"] if artifact["run_id"] == run_id]
            assert len(artifacts) >= 2, "Expected statistics and chart artifacts"
            report["artifacts"] = []
            statistics = None
            for artifact in artifacts:
                downloaded = context.request.get(options.base_url + artifact["download_url"])
                assert downloaded.ok, f"Artifact download failed: {downloaded.status}"
                data = downloaded.body()
                digest = hashlib.sha256(data).hexdigest()
                assert digest == artifact["metadata"]["sha256"]
                report["artifacts"].append({"title": artifact["title"], "sha256": digest, "bytes": len(data)})
                if artifact["title"] == "statistics.json":
                    statistics = json.loads(data)
                if artifact["mime_type"] == "image/png":
                    assert data.startswith(b"\x89PNG\r\n\x1a\n"), "Chart is not a PNG"
                    report["checks"]["chart_downloaded"] = True
            assert statistics is not None
            import numpy as np
            from PIL import Image
            with Image.open(options.image) as image:
                pixels = np.asarray(image.convert("RGB"))
                assert statistics["width"] == image.width and statistics["height"] == image.height
            means = statistics["band_means"]
            if isinstance(means, dict):
                means = list(means.values())
            assert np.allclose(means[:3], pixels.mean(axis=(0, 1)), atol=.01), "Published means do not match actual source pixels"
            report["statistics"] = statistics
            report["checks"]["statistics_verified_against_source"] = True
            report["checks"]["artifact_hashes_verified"] = True
            page.locator(".message.assistant").wait_for(timeout=20000)
            page.get_by_label("Python 分析成果").wait_for(timeout=20000)
            assert page.locator(".python-artifacts a[download]").count() >= 2
            page.locator(".python-artifacts summary").first.click()
            page.locator(".python-artifacts img").first.scroll_into_view_if_needed()
            page.wait_for_function("Array.from(document.querySelectorAll('.python-artifacts img')).some(img => img.complete && img.naturalWidth > 0)")
            report["checks"]["chat_artifact_links_and_chart_preview"] = True
            page.screenshot(path=str(folder / "v3-local-agent-desktop.png"))
            page.reload(wait_until="domcontentloaded")
            page.locator(".message.assistant").wait_for(timeout=20000)
            report["checks"]["refresh_restores_result"] = True
            page.set_viewport_size({"width": 390, "height": 844})
            page.get_by_role("button", name="对话", exact=True).click()
            page.screenshot(path=str(folder / "v3-local-agent-mobile.png"))
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
            report["checks"]["mobile_no_overflow"] = True
            assert not report["errors"]
            report["status"] = "passed"
        except Exception as error:
            report["status"] = "failed"
            report["failure"] = {"type": type(error).__name__, "message": str(error)}
            page.screenshot(path=str(folder / "v3-local-agent-failure.png"))
        finally:
            browser.close()
            (folder / "v3-local-agent-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
