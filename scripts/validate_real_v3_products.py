"""Run a disposable, real-source acceptance check for V3 raster products.

Uses a dedicated validation owner and a temporary MEDIA_ROOT.  It neither reads
nor modifies user attachments, and prints summaries only (never signed URLs).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
import argparse
from contextlib import nullcontext
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "satellite_map.settings")

import django

django.setup()

from django.test import override_settings

from map_api.models import AgentRun, Conversation
from map_api.v3.data_adapter import execute


OWNER = "validation-real-cog-20260913"
BBOX = {"min_lng": 116.300, "min_lat": 39.900, "max_lng": 116.305, "max_lat": 39.905}


def _result(response):
    if "error" in response:
        raise RuntimeError(response["error"]["code"] + ": " + response["error"]["message"])
    return response["result"]


def _retrieve_and_compute(context, collection, product, start=None, end=None):
    args = {"collection": collection, "bbox": BBOX}
    if start:
        args["date_start"] = start
    if end:
        args["date_end"] = end
    excluded = []
    for attempt in range(4):
        args["exclude_item_ids"] = excluded
        attachment = _result(execute("retrieve_imagery", args, context))["attachment"]
        context["attachment_ids"].append(attachment["id"])
        computed = execute("compute_product", {"attachment_id": attachment["id"], "product": product}, context)
        if "error" not in computed:
            product_result = computed["result"]
            break
        if "QA" not in computed["error"]["message"] or attempt == 3:
            _result(computed)
        excluded.append(attachment["metadata"]["item_id"])
    else:
        raise RuntimeError("候选均不满足 QA")
    summary = product_result["summary"]
    return attachment["id"], {"product": product, "summary": summary["summary"], "measurement_grade": summary["measurement_grade"], "rejected_qa_candidates": excluded, "item_id": attachment["metadata"].get("item_id"), "acquired_at": attachment["metadata"].get("acquired_at")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-assets", action="store_true", help="保存本次真实来源影像与产物到 output/v3-cases，供复核和重放")
    options = parser.parse_args()
    report = {"owner": OWNER, "bbox": BBOX, "checks": {}}
    if options.keep_assets:
        case_root = Path(__file__).resolve().parents[1] / "output" / "v3-cases"
        case_root.mkdir(parents=True, exist_ok=True)
        folder = tempfile.mkdtemp(prefix="beijing-change-", dir=case_root)
        storage = nullcontext(folder)
        report["asset_directory"] = str(Path(folder).relative_to(Path(__file__).resolve().parents[1]))
    else:
        storage = tempfile.TemporaryDirectory(prefix="satellite-v3-real-")
    with storage as media_root, override_settings(MEDIA_ROOT=media_root):
        conversation = Conversation.objects.create(owner_session_key=OWNER, title="Disposable real COG validation")
        run = AgentRun.objects.create(goal="real COG validation", conversation=conversation,
            run_key="real-cog-" + uuid.uuid4().hex, provider="validation", model="validation-owner", execution_engine="v3")
        context = {"conversation": conversation, "run": run, "attachment_ids": []}
        try:
            before, report["checks"]["sentinel2_ndvi"] = _retrieve_and_compute(context, "sentinel-2-l2a", "ndvi", "2025-06-01", "2025-06-30")
            _, report["checks"]["dem_terrain"] = _retrieve_and_compute(context, "cop-dem-glo-30", "dem_terrain")
            _, report["checks"]["landsat_st_b10"] = _retrieve_and_compute(context, "landsat-c2-l2", "landsat_surface_temperature", "2025-06-01", "2025-08-30")
            report["checks"]["sentinel2_before"] = report["checks"]["sentinel2_ndvi"]
            after, report["checks"]["sentinel2_after"] = _retrieve_and_compute(context, "sentinel-2-l2a", "ndvi", "2025-08-01", "2025-08-30")
            context["attachment_ids"] = [before, after]
            changed = _result(execute("compare_two_date_change", {"reference_attachment_id": before,
                "comparison_attachment_id": after, "product": "ndvi"}, context))
            report["checks"]["sentinel2_change"] = {"summary": changed["summary"], "measurement_grade": changed["measurement_grade"]}
            report["status"] = "passed"
        except Exception as exc:
            report["status"] = "failed"
            report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        finally:
            if options.keep_assets:
                from map_api.v3.assets import attachment_payload, observation_payload
                from map_api.v3.common import evidence_json, artifact_json
                def relative(path):
                    return str(Path(path).relative_to(media_root)).replace('\\', '/') if path else None
                pack = {"schema": 1, "mode": "real_data_tool_validation", "report": report,
                    "attachments": [{**attachment_payload(a), "file": relative(a.file_path), "preview": relative(a.preview_path)} for a in conversation.attachments.all()],
                    "observations": [{**observation_payload(o), "preview": relative(o.preview_path)} for o in conversation.observations.all()],
                    "evidence": [evidence_json(e) for e in run.evidence_v2.all()],
                    "artifacts": [artifact_json(a) for a in run.artifacts_v2.all()]}
                Path(media_root, "manifest.json").write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")
            # Cascade only the records owned by this disposable validation run.
            run.delete()
            conversation.delete()
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    report_path = Path(__file__).resolve().parents[1] / "output" / "real_cog_acceptance_20260913.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
