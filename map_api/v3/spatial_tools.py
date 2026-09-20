"""Model-facing operations accept scoped IDs, never fabricated numerical arrays."""
import base64
import json
import re
from pathlib import Path
from django.conf import settings
from ..models import SpatialObservation
from ..run_journal import retry_sqlite_write
from . import assets
from .answer_validation import NUMERIC_CLAIMS_SCHEMA, validate_answer

def get_attachment(ctx, id):
    a = ctx["run"].conversation.attachments.filter(pk=id).first()
    if not a or str(a.id) not in ctx["attachment_ids"]:
        raise ValueError("影像不在当前任务已发送的附件范围内")
    if a.status != "ready":
        raise ValueError("影像尚未就绪: " + a.status)
    return a

@retry_sqlite_write
def read_window(args, ctx):
    a = get_attachment(ctx, args["attachment_id"])
    old = SpatialObservation.objects.filter(run=ctx["run"], metadata__call_key=ctx["call_key"]).first()
    if old:
        return {"observation": assets.observation_payload(old), "image_refs": [str(old.id)]}
    item = assets.read_window(a, args["x"], args["y"], args["width"], args["height"], args.get("max_size", 1024),
        observation_context={"run": ctx["run"], "context_version": ctx["version"],
            "label": args.get("label") or f'窗口 {args["x"]},{args["y"]}',
            "metadata": {"call_key": ctx["call_key"], "asset_sha256": a.sha256}})
    o = item["observation"]
    return {"observation": assets.observation_payload(o), "image_refs": [str(o.id)]}

def overview(args, ctx):
    a = get_attachment(ctx, args["attachment_id"])
    return read_window({"attachment_id": str(a.id), "x": 0, "y": 0, "width": a.width,
                        "height": a.height, "max_size": 1536, "label": args.get("label", "全图概览")}, ctx)

def plan(args, ctx):
    return {"plan": args["steps"], "facts": args.get("facts", []), "hypotheses": args.get("hypotheses", []),
            "open_questions": args.get("open_questions", [])}

def annotate(args, ctx):
    from django.core.exceptions import ValidationError
    try:
        o = ctx["run"].conversation.observations.filter(pk=args["observation_id"]).first()
    except (ValidationError, ValueError):
        o = None
    if o is None:
        raise ValueError("observation_id 不存在。请先调用 view_overview 或 read_image_window，使用结果中的 observation.id；附件 ID 不能用于标注。")
    if str(o.attachment_id) not in ctx["attachment_ids"]:
        raise ValueError("观察不在当前问题范围内")
    old = SpatialObservation.objects.filter(run=ctx["run"], metadata__call_key=ctx["call_key"]).first()
    if old:
        return {"observation": assets.observation_payload(old)}
    target = SpatialObservation.objects.create(conversation=o.conversation, attachment=o.attachment, run=ctx["run"],
        label=args["label"], kind="finding", window=o.window, geometry=o.geometry, summary=args["summary"],
        confidence=args.get("confidence"), preview_path=o.preview_path, evidence_refs=[str(o.id)],
        context_version=ctx["version"], metadata={**o.metadata, "call_key": ctx["call_key"], "source_observation": str(o.id)})
    return {"observation": assets.observation_payload(target)}

def finish(args, ctx):
    c = ctx["run"].conversation
    ids = args.get("observation_ids", [])
    observations = list(c.observations.select_related("attachment").filter(pk__in=ids))
    if len(observations) != len(set(ids)) or any(str(o.attachment_id) not in ctx["attachment_ids"] for o in observations):
        raise ValueError("回答包含不存在或不在问题范围内的空间引用")
    if set(args.get("evidence_ids", [])) - set(c.runs.values_list("evidence_v2__evidence_id", flat=True)):
        raise ValueError("回答包含不存在的数据证据")
    if ctx["attachment_ids"] and not ids and not args.get("evidence_ids") and not args.get("limitations"):
        raise ValueError("影像回答需要真实位置或数据引用；若无法分析，请在 limitations 中说明具体缺口")
    if args.get("numeric_claims"):
        try:
            validate_answer(args, ctx, observations)
        except ValueError as error:
            return {"error": {"code": "answer_validation_failed", "message": str(error), "retryable": True,
                              "next_step": "纠正正文数值或 numeric_claims 后重新调用 finish_answer；只重试不改数值不会通过核算"}}
    # These expressions catch directional assertions, not arbitrary names
    # containing 东/西. A pixel-only image cannot substantiate compass bearings.
    compass = r"(?:东北|西北|东南|西南|[东西南北])(?:侧|部|边|面|方|向)|\b(?:north|south|east|west)(?:ern|ward|wards)?\b"
    if observations and all(o.attachment.coordinate_space == "image_pixels" for o in observations):
        if re.search(compass, args["answer"], flags=re.I):
            raise ValueError("引用影像没有可靠地理方向。请用图像左/右/上/下描述位置，修改后再次交付")
    return {"final": args}

def relation(args, ctx):
    a, b = [ctx["run"].conversation.observations.get(pk=x) for x in (args["first"], args["second"])]
    if any(str(o.attachment_id) not in ctx["attachment_ids"] for o in (a, b)):
        raise ValueError("观察不在当前问题已发送的附件范围内")
    if a.attachment_id != b.attachment_id:
        raise ValueError("不同影像必须先验证配准，不能直接比较像素位置")
    from shapely.geometry import shape
    ga, gb = shape(a.geometry), shape(b.geometry)
    dx, dy = gb.centroid.x - ga.centroid.x, gb.centroid.y - ga.centroid.y
    return {"first": str(a.id), "second": str(b.id), "intersects": ga.intersects(gb),
            "distance_pixels": ga.distance(gb), "center_offset_pixels": [dx, dy],
            "relative_position": ("右" if dx > 0 else "左" if dx < 0 else "同列") + ("下" if dy > 0 else "上" if dy < 0 else "同排"),
            "coordinate_space": "image_pixels", "measurement_basis": "observation_window_boundaries",
            "limitation": "距离基于观察窗口边界，不等于地物边界或实际地面距离"}

def image_data(path):
    p = Path(path)
    if not p.is_file() or p.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("模型观察图不可用或超过输入上限")
    return "data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode("ascii")

def vision(args, ctx):
    from .provider import tool_call
    observations = list(ctx["run"].conversation.observations.filter(pk__in=args["observation_ids"]))
    if len(observations) != len(set(args["observation_ids"])) or any(str(o.attachment_id) not in ctx["attachment_ids"] for o in observations):
        raise ValueError("视觉复核引用无效")
    answer = tool_call([{"role": "system", "content": "你是遥感局部观察员。只回答图像可见内容，区分事实和推测；不编造日期、分辨率和计数。"},
                       {"role": "user", "content": args["question"] + "\n窗口索引: " + json.dumps([assets.observation_payload(o) for o in observations], ensure_ascii=False)}], [], role="vision", images=[image_data(o.preview_path) for o in observations])
    return {"observation_ids": [str(o.id) for o in observations], "review": answer["content"],
            "model": answer["model"], "usage": answer["usage"], "latency_ms": answer["latency_ms"]}

def history(args, ctx):
    messages = ctx["run"].conversation.messages.filter(sequence__gte=args.get("after_sequence", 0),
        status__in=["adopted", "completed"]).order_by("sequence")[:30]
    remaining, items = args.get("max_characters", 24000), []
    offset = args.get("character_offset", 0)
    next_page = None
    for m in messages:
        excerpt = m.content[offset:offset + remaining]
        end = offset + len(excerpt)
        items.append({"sequence": m.sequence, "role": m.role, "content": excerpt,
                      "parts": m.parts, "character_offset": offset, "total_characters": len(m.content)})
        remaining -= len(excerpt)
        next_page = {"after_sequence": m.sequence, "character_offset": end} if end < len(m.content) else {"after_sequence": m.sequence + 1, "character_offset": 0}
        offset = 0
        if remaining <= 0:
            break
    return {"items": items, "next_page": next_page}

ANALYSIS_RESULT_MAX_BYTES = 1024 * 1024


def _analysis_result(result):
    """Read the explicitly named structured result without treating arbitrary output as facts."""
    output = next((item for item in result["outputs"] if item["name"] == "analysis_result.json"), None)
    if not output or output["size_bytes"] > ANALYSIS_RESULT_MAX_BYTES:
        return None
    root = (Path(settings.MEDIA_ROOT).resolve() / "v3").resolve()
    path = (root / result["relative_output_dir"] / output["name"]).resolve()
    if not path.is_relative_to(root):
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, (dict, list)) else None


def python(args, ctx):
    from .sandbox import run_python
    from .common import artifact_json, evidence_json
    import mimetypes
    import hashlib
    from ..models import RunArtifact, RunEvidence
    attachments = [get_attachment(ctx, id) for id in args["attachment_ids"]]
    paths = [attachment.file_path for attachment in attachments]
    timeout = args.get("timeout_seconds", 300)
    result = run_python(args["code"], paths, f"run-{ctx['run'].id}-{ctx['call_key'][:24]}", timeout=timeout)
    artifacts = []
    for output in result["outputs"]:
        if output["name"] == "analysis_result.json":
            # Control-plane metadata: its values are merged into evidence below
            # and readable via read_saved_result, not a downloadable artifact.
            continue
        suffix = Path(output["name"]).suffix.lower()
        mime_type = {".csv": "text/csv", ".json": "application/json", ".tif": "image/tiff",
                     ".tiff": "image/tiff"}.get(suffix) or mimetypes.guess_type(output["name"])[0] or "application/octet-stream"
        name_key = hashlib.sha256(output["name"].encode("utf-8")).hexdigest()[:24]
        artifact, _ = RunArtifact.objects.get_or_create(run=ctx["run"], artifact_id="python:" + ctx["call_key"] + ":" + name_key,
            defaults={"kind": "python_output", "title": output["name"][:240], "uri": "", "mime_type": mime_type,
                      "metadata": {**output, "relative_path": result["relative_output_dir"] + "/" + output["name"],
                                   "code_sha256": result["code_sha256"], "image_id": result["image_id"], "inputs": result["inputs"],
                                   "runtime": result["runtime"], "execution_status": result.get("status"),
                                   "timeout_seconds": timeout, "python_version": result.get("python_version"),
                                   "packages": result.get("packages", {})}})
        artifacts.append(artifact)
    response = {**result, "artifact_ids": [artifact.id for artifact in artifacts],
                "artifacts": [artifact_json(artifact) for artifact in artifacts], "evidence_ids": []}
    if not result.get("error"):
        evidence, _ = RunEvidence.objects.get_or_create(run=ctx["run"], evidence_id=f"python-{ctx['run'].id}-{ctx['call_key']}",
            defaults={"kind": "code_execution", "metric": "python_analysis",
                "method": "本地 Python" if result["runtime"] == "local" else "Docker Python",
                "value": {"stdout": result["stdout"], "artifacts": response["artifacts"],
                          "input_attachment_ids": args["attachment_ids"], "runtime": result["runtime"],
                          "execution_status": result.get("status")},
                "data_contract": {"source": "Python 实际执行结果", "code_sha256": result["code_sha256"],
                    "input_version": ctx["version"], "inputs": result["inputs"],
                    "input_assets": [{"id": str(a.id), "sha256": a.sha256} for a in attachments],
                    "python_version": result.get("python_version"), "packages": result.get("packages", {})}})
        for artifact in artifacts:
            if evidence.evidence_id not in artifact.evidence_refs:
                artifact.evidence_refs = [*artifact.evidence_refs, evidence.evidence_id]
                artifact.save(update_fields=["evidence_refs"])
        response["artifacts"] = [artifact_json(artifact) for artifact in artifacts]
        response["evidence_ids"] = [evidence.evidence_id]
        response["evidence"] = evidence_json(evidence)
        analysis_result = _analysis_result(result)
        if analysis_result is not None:
            evidence.value = {**(evidence.value or {}), "result": analysis_result}
            evidence.save(update_fields=["value"])
            response["evidence"] = evidence_json(evidence)
    return response


@retry_sqlite_write
def import_python_output(args, ctx):
    """Promote a raster/image output to a real, inspectable spatial attachment."""
    from ..models import RunArtifact, SpatialAttachment
    artifact = RunArtifact.objects.filter(pk=args["artifact_id"], run=ctx["run"]).first()
    if artifact is None or artifact.kind != "python_output":
        raise ValueError("产物不属于当前运行的 Python 分析")
    relative = str((artifact.metadata or {}).get("relative_path") or "")
    base = (Path(settings.MEDIA_ROOT).resolve() / "v3").resolve()
    source = (base / relative).resolve()
    suffix = source.suffix.lower()
    if suffix not in {".tif", ".tiff", ".png", ".jpg", ".jpeg"} or not source.is_file() or not source.is_relative_to(base):
        raise ValueError("只有已保存的 GeoTIFF、PNG 或 JPEG Python 产物可注册为空间附件")
    existing = next((item for item in ctx["run"].conversation.attachments.filter(parent__isnull=True)
                     if (item.metadata or {}).get("python_import", {}).get("artifact_id") == artifact.id), None)
    if existing:
        observation = existing.observations.filter(run=ctx["run"], kind="python_output").first()
        response = {"attachment": assets.attachment_payload(existing), "artifact_id": artifact.id}
        if observation:
            response.update({"observation": assets.observation_payload(observation), "image_refs": [str(observation.id)]})
        return response
    provenance = {"artifact_id": artifact.id, "artifact_key": artifact.artifact_id,
                  "relative_path": relative,
                  "sha256": (artifact.metadata or {}).get("sha256"),
                  "code_sha256": (artifact.metadata or {}).get("code_sha256"),
                  "evidence_refs": artifact.evidence_refs}
    stored = assets.adopt_file(source, source.name)
    attachment = SpatialAttachment.objects.create(
        owner_session_key=ctx["run"].conversation.owner_session_key, conversation=ctx["run"].conversation,
        name=source.name[:240], kind="geotiff" if suffix in {".tif", ".tiff"} else "image", status="pending",
        file_path=str(stored), size_bytes=stored.stat().st_size, sha256=(artifact.metadata or {}).get("sha256", ""),
        metadata={"python_import": provenance})
    attachment = assets.process_attachment(attachment.id)
    if attachment.status != "ready":
        raise ValueError("Python 产物无法注册为空间附件: " + (attachment.error or attachment.status))
    item = assets.read_window(attachment, 0, 0, attachment.width, attachment.height, 1536,
        observation_context={"run": ctx["run"], "context_version": ctx["version"], "kind": "python_output",
                             "label": args.get("label") or attachment.name,
                             "summary": "Python 分析产物全图概览；位置和像素解释须以该产物的数据契约为准。",
                             "evidence_refs": artifact.evidence_refs,
                             "metadata": {"call_key": ctx["call_key"], "python_artifact_id": artifact.id, "provenance": provenance}})
    observation = item["observation"]
    return {"attachment": assets.attachment_payload(attachment), "artifact_id": artifact.id,
            "observation": assets.observation_payload(observation), "image_refs": [str(observation.id)]}

def specifications(Tool, schema):
    from .sandbox import python_runtime
    text = {"type": "string", "maxLength": 8000}
    id = {"type": "string", "maxLength": 160}
    ids = {"type": "array", "items": id, "maxItems": 64}
    integer = {"type": "integer", "minimum": 0}
    return [
        Tool("view_overview", "查看影像概览。大图先概览再读局部；保留原始宽高和坐标。", schema({"attachment_id": id, "label": text}, ["attachment_id"]), overview, parallel=True),
        Tool("read_image_window", "按原始像素读取窗口，默认1024px，相邻窗口建议重叠128px。输出空间观察和图像。", schema({"attachment_id": id, "x": integer, "y": integer, "width": {"type": "integer", "minimum": 1}, "height": {"type": "integer", "minimum": 1}, "max_size": {"type": "integer", "minimum": 1, "maximum": 4096}, "label": text}, ["attachment_id", "x", "y", "width", "height"]), read_window, parallel=True),
        Tool("update_plan", "更新公开行动计划、事实、假设与问题；不输出内部推理。", schema({"steps": {"type": "array", "items": schema({"label": text, "status": {"enum": ["pending", "running", "completed"]}}, ["label", "status"]), "maxItems": 12}, "facts": {"type": "array", "items": text}, "hypotheses": {"type": "array", "items": text}, "open_questions": {"type": "array", "items": text}}, ["steps"]), plan),
        Tool("annotate_observation", "将看过的窗口标为发现，建立稳定位置引用。", schema({"observation_id": id, "label": text, "summary": text, "confidence": {"type": "number", "minimum": 0, "maximum": 1}}, ["observation_id", "label", "summary"]), annotate),
        Tool("spatial_relation", "计算同一影像中两个区域的位置、距离和相交。", schema({"first": id, "second": id}, ["first", "second"]), relation, parallel=True),
        Tool("review_visual", "让专门视觉模型复核局部观察、困难目标或疑似变化。", schema({"observation_ids": {**ids, "minItems": 1, "maxItems": 4}, "question": text}, ["observation_ids", "question"]), vision, recovery="uncertain_external", parallel=True),
        Tool("read_history", "按消息序号读取旧历史；next_page 可继续读取长消息，原始消息完整保留。", schema({"after_sequence": integer, "character_offset": integer, "max_characters": {"type": "integer", "minimum": 100, "maximum": 32000}}), history),
        Tool("python_analysis", ("使用项目 Python 直接本地执行，可联网、读写文件和调用子进程，无需额外批准。" if python_runtime() == "local" else "使用禁网 Docker 容器执行，输入只读。") +
             "代码可直接使用 inputs（按 attachment_ids 顺序排列的 pathlib.Path 列表）、output_dir（产物目录）、project_dir（本地模式下的项目目录）。"
             "将图表、栅格、表格等写入 output_dir，会自动作为产物返回（analysis_result.json 除外，它并入证据元数据）。可使用 numpy/scipy/rasterio/pyproj/shapely/pandas/matplotlib。"
             "可指定 30–600 秒执行时限，默认 300 秒；取消会终止进程树。成功执行返回真实 evidence_ids，最终回答直接引用这些 ID。"
             "若生成 analysis_result.json（不超过 1 MiB 的 JSON 对象或数组），其内容会保存到证据 result 字段，可供数值引用。运行出错时根据实际 stdout/stderr 修改代码重试。",
             schema({"code": {"type": "string", "maxLength": 40000}, "attachment_ids": ids,
                     "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 600}}, ["code", "attachment_ids"]), python,
             timeout=660, recovery="uncertain_external" if python_runtime() == "local" else "isolated_replay", parallel=True),
        Tool("import_python_output", "将当前运行 Python 已保存的 GeoTIFF、PNG 或 JPEG 产物注册为真实空间附件并创建全图观察。保留代码、产物哈希和证据来源；不能将其自动视为量算真值。",
             schema({"artifact_id": {"type": "integer", "minimum": 1}, "label": text}, ["artifact_id"]), import_python_output,
             timeout=180, recovery="replay_safe"),
        Tool("finish_answer", "交付答案和真实位置/数据证据。明确计数是检出/估计/完整统计、日期、覆盖和不确定性。涉及面积占比、百分比或时相差值时用 numeric_claims 引用已保存测量记录核算。", schema({"answer": {"type": "string", "minLength": 1, "maxLength": 32000}, "observation_ids": ids, "evidence_ids": ids, "limitations": {"type": "array", "items": text}, "numeric_claims": NUMERIC_CLAIMS_SCHEMA}, ["answer", "observation_ids", "evidence_ids", "limitations"]), finish),
    ]
