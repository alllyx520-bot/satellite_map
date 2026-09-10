"""Read-only acceptance assessment. Missing execution telemetry is unknown, never success."""


def assess_run(run, *, evidence, artifacts):
    if run.execution_engine == "dag":
        return _assess_dag(run, evidence=evidence, artifacts=artifacts)
    steps = list(run.steps.all())
    evidence_ids = {item.evidence_id for item in evidence}
    referenced_ids = {ref for item in artifacts for ref in item.evidence_refs}
    missing = []
    if not steps or any(step.status != "completed" for step in steps):
        missing.append("required_steps_incomplete")
    # The current adapter has no persisted required-evidence declaration. Existing
    # evidence alone cannot prove that all requirements were satisfied.
    missing.append("required_evidence_declaration_missing")
    if not referenced_ids:
        missing.append("final_evidence_refs_missing")
    elif not referenced_ids.issubset(evidence_ids):
        missing.append("dangling_evidence_refs")
    if not any(step.step_id == "quality_gate" and step.status == "completed" for step in steps):
        missing.append("quality_gate_unverified")
    if run.error or any(step.error for step in steps):
        missing.append("unresolved_errors")
    missing.append("execution_telemetry_missing")
    return {
        "passed": False,
        "required_evidence_complete": False,
        "fallback_used": None,
        "degraded": None,
        "provider_switch_count": None,
        "unresolved_errors": None,
        "known_error_count": int(bool(run.error)) + sum(bool(step.error) for step in steps),
        "missing_requirements": missing,
    }


def _assess_dag(run, *, evidence, artifacts):
    from .run_products import read_product
    latest = run.checkpoints.order_by("-sequence").first()
    context = latest.context_snapshot if latest else {}
    steps = list(run.steps.all())
    missing = []
    if not steps or any(step.status != "completed" and not (step.optional and step.status == "skipped") for step in steps):
        missing.append("required_steps_incomplete")
    requirements = context.get("requirements")
    if not requirements:
        missing.append("required_evidence_declaration_missing")
    if context.get("capabilities", {}).get("status") != "matched":
        missing.append("source_capabilities_unmatched")
    if context.get("quality", {}).get("passed") is not True:
        missing.append("quality_gate_unverified")
    current = [item for item in evidence if item.evidence_id.startswith(f"v{run.plan_version}:")]
    ids = {item.evidence_id for item in current}
    final = context.get("final") or {}
    refs = final.get("evidence_refs") or []
    if not refs or set(refs) != ids or not ids:
        missing.append("final_evidence_refs_incomplete")
    if not final.get("content") or not final.get("limitations"):
        missing.append("final_limitations_missing")
    periods = len(context.get("periods", []))
    for index in (requirements or {}).get("required_indices", []):
        if periods == 0 or sum(item.kind == "computed_metric" and item.metric == index for item in current) != periods:
            missing.append("required_metric_missing:" + index)
    if not any(item.kind == "model_inference" for item in current):
        missing.append("visual_evidence_missing")
    if (requirements or {}).get("needs_two_dates") and not any(item.kind == "temporal_change" for item in current):
        missing.append("temporal_evidence_missing")
    unresolved = int(bool(run.error)) + sum(bool(step.error) for step in steps)
    if unresolved:
        missing.append("unresolved_errors")
    current_artifacts = [item for item in artifacts if item.artifact_id.startswith(f"file:{run.plan_version}:")]
    for kind in ("report", "final_result"):
        if not any(item.kind == kind and set(item.evidence_refs) == set(refs) for item in current_artifacts):
            missing.append(kind + "_missing")
    for item in current_artifacts:
        try:
            read_product(run.id, item.metadata)
        except (OSError, ValueError, KeyError):
            missing.append("artifact_unreadable:" + item.artifact_id)
    if context.get("slots", {}).get("source") != "mapbox":
        masks = context.get("mask_statistics", [])
        if len(masks) != periods or not masks or any(mask.get("valid_pixel_ratio", 0) < 0.6 or mask.get("valid_pixel_count", 0) < 1024 for mask in masks):
            missing.append("qa_mask_incomplete")
    requests = list(run.events.filter(type="provider.requested").values_list("payload", flat=True))
    successful = [item for item in requests if not item.get("error")]
    if not any(item.get("operation") == "plan" for item in successful) or sum(item.get("operation") == "review" for item in successful) < 2:
        missing.append("provider_execution_unverified")
    providers = {(item.get("provider", "").lower(), item.get("model")) for item in requests}
    switches = max(0, len(providers) - 1)
    if providers != {(run.provider.lower(), run.model)}:
        missing.append("unexpected_provider_switch")
    forbidden = run.events.filter(type__in=["source.fallback", "model.fallback", "run.degraded"]).exists()
    if forbidden:
        missing.append("fallback_or_degradation")
    return {"passed": not missing, "required_evidence_complete": not any("evidence" in item or "metric" in item for item in missing),
            "fallback_used": forbidden, "degraded": forbidden, "provider_switch_count": switches,
            "unresolved_errors": unresolved, "known_error_count": unresolved, "missing_requirements": missing}
