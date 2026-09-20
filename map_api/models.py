import uuid

from django.db import models


class ChatHistory(models.Model):
    scene = models.ForeignKey("ImageryScene", null=True, blank=True, on_delete=models.SET_NULL, related_name="chat_histories")
    image_file = models.CharField(max_length=255, unique=True)
    messages = models.JSONField(default=list)
    spatial_context = models.CharField(max_length=500, blank=True, default="")
    bbox = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]


class DownloadTask(models.Model):
    scene = models.ForeignKey("ImageryScene", null=True, blank=True, on_delete=models.SET_NULL, related_name="download_tasks")
    file_name = models.CharField(max_length=255, unique=True)
    status = models.CharField(max_length=20, default="downloading")
    total = models.PositiveIntegerField(default=1)
    done = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)
    min_lng = models.FloatField()
    min_lat = models.FloatField()
    max_lng = models.FloatField()
    max_lat = models.FloatField()
    gsd_m = models.FloatField(default=0)
    area_km2 = models.FloatField(default=0)
    resolution_px = models.PositiveIntegerField(default=0)
    error_message = models.CharField(max_length=500, blank=True, default="")
    worker_claim = models.CharField(max_length=80, blank=True, default="")
    claimed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]


class ImageryScene(models.Model):
    SOURCE_MAPBOX = "mapbox"
    GRADE_REFERENCE = "reference"
    GRADE_SCREENING = "screening"
    GRADE_DECISION_SUPPORT = "decision_support"
    GRADE_EVIDENCE = "evidence"

    file_name = models.CharField(max_length=255, unique=True)
    source = models.CharField(max_length=50, default=SOURCE_MAPBOX)
    source_label = models.CharField(max_length=120, default="Mapbox Satellite Basemap")
    product_id = models.CharField(max_length=255, blank=True, default="")
    acquired_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    fetched_at = models.DateTimeField(auto_now_add=True)
    min_lng = models.FloatField()
    min_lat = models.FloatField()
    max_lng = models.FloatField()
    max_lat = models.FloatField()
    gsd_m = models.FloatField(default=0)
    area_km2 = models.FloatField(default=0)
    cloud_percent = models.FloatField(null=True, blank=True)
    processing_level = models.CharField(max_length=80, default="basemap")
    license_type = models.CharField(max_length=80, default="mapbox_terms")
    decision_grade = models.CharField(max_length=50, default=GRADE_REFERENCE)
    limitations = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]


class AgentSession(models.Model):
    STATUS_RUNNING = "running"
    STATUS_WAITING_USER = "waiting_user"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"

    goal = models.TextField()
    request_id = models.CharField(max_length=120, unique=True, null=True, blank=True)
    # 匿名浏览器级隔离：不依赖登录系统，也避免仅凭可枚举的自增 id 读取他人任务。
    # 旧数据允许为空，便于迁移；新 API 创建的会话必须写入当前 Django session key。
    owner_session_key = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    message_request_ids = models.JSONField(default=list, blank=True)
    message_request_states = models.JSONField(default=dict, blank=True)
    mode = models.CharField(max_length=20, default="precise")
    status = models.CharField(max_length=30, default=STATUS_RUNNING)
    # 独立于 JSON artifacts 的持久化取消熔断；SQLite 无 select_for_update 时仍可靠。
    cancel_requested = models.BooleanField(default=False)
    slots = models.JSONField(default=dict, blank=True)
    plan = models.JSONField(default=dict, blank=True)
    timeline = models.JSONField(default=list, blank=True)
    messages = models.JSONField(default=list, blank=True)
    artifacts = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")
    scene = models.ForeignKey(ImageryScene, null=True, blank=True, on_delete=models.SET_NULL, related_name="agent_sessions")
    history = models.ForeignKey(ChatHistory, null=True, blank=True, on_delete=models.SET_NULL, related_name="agent_sessions")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]


class AgentRun(models.Model):
    """持久化运行事实；AgentSession 仅作为兼容层保留。"""
    STATUS_QUEUED = "queued"
    STATUS_PLANNING = "planning"
    STATUS_RUNNING = "running"
    STATUS_WAITING_USER = "waiting_user"
    STATUS_RETRYING = "retrying"
    STATUS_CANCELLING = "cancelling"
    STATUS_CANCELLED = "cancelled"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_BLOCKED = "blocked"
    STATUS_NOT_SUPPORTED = "not_supported"
    STATUS_EXTERNAL_UNAVAILABLE = "external_service_unavailable"

    goal = models.TextField()
    conversation = models.ForeignKey("Conversation", null=True, blank=True, on_delete=models.SET_NULL, related_name="runs")
    trigger_message = models.ForeignKey("ConversationMessage", null=True, blank=True, on_delete=models.SET_NULL, related_name="triggered_runs")
    worker_claim = models.CharField(max_length=64, blank=True, default="")
    lease_until = models.DateTimeField(null=True, blank=True)
    budget = models.JSONField(default=dict, blank=True)
    usage = models.JSONField(default=dict, blank=True)
    run_key = models.CharField(max_length=120, unique=True)
    status = models.CharField(max_length=40, default=STATUS_QUEUED)
    mode = models.CharField(max_length=30, default="precise")
    current_step_id = models.CharField(max_length=120, blank=True, default="")
    plan_version = models.PositiveIntegerField(default=1)
    context_version = models.PositiveIntegerField(default=1)
    event_sequence = models.PositiveIntegerField(default=0)
    provider = models.CharField(max_length=120, blank=True, default="")
    model = models.CharField(max_length=120, blank=True, default="")
    execution_engine = models.CharField(max_length=20, default="legacy")
    cancellation_epoch = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)


class RunStep(models.Model):
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="steps")
    step_id = models.CharField(max_length=120)
    kind = models.CharField(max_length=60)
    label = models.CharField(max_length=200)
    status = models.CharField(max_length=40, default="queued")
    depends_on = models.JSONField(default=list, blank=True)
    required_capabilities = models.JSONField(default=list, blank=True)
    input_refs = models.JSONField(default=list, blank=True)
    output_refs = models.JSONField(default=list, blank=True)
    attempt = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=1)
    retry_policy = models.JSONField(default=dict, blank=True)
    approval_policy = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    purpose = models.TextField(blank=True, default="")
    completion_schema = models.JSONField(default=dict, blank=True)
    failure_conditions = models.JSONField(default=list, blank=True)
    optional = models.BooleanField(default=False)
    allow_replan = models.BooleanField(default=True)
    lease_token = models.CharField(max_length=64, blank=True, default="")
    lease_until = models.DateTimeField(null=True, blank=True)
    lease_plan_version = models.PositiveIntegerField(default=0)
    lease_cancellation_epoch = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "step_id"], name="uniq_run_step")]


class RunCheckpoint(models.Model):
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="checkpoints")
    sequence = models.PositiveIntegerField()
    state_snapshot = models.JSONField(default=dict)
    context_snapshot = models.JSONField(default=dict)
    plan_snapshot = models.JSONField(default=dict)
    last_event_sequence = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "sequence"], name="uniq_run_checkpoint")]


class RunEvent(models.Model):
    """Committed v2 events; sequence and command identity are scoped to a run."""
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="events")
    sequence = models.PositiveIntegerField()
    schema_version = models.PositiveIntegerField(default=1)
    type = models.CharField(max_length=60)
    step_id = models.CharField(max_length=120, blank=True, default="")
    payload = models.JSONField(default=dict)
    refs = models.JSONField(default=list)
    command_key = models.CharField(max_length=160)
    command_digest = models.CharField(max_length=64)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(fields=["run", "sequence"], name="uniq_run_event_sequence"),
            models.UniqueConstraint(fields=["run", "command_key"], name="uniq_run_event_command"),
        ]


class RunToolCall(models.Model):
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="tool_calls")
    call_key = models.CharField(max_length=64)
    name = models.CharField(max_length=120)
    step_id = models.CharField(max_length=120)
    arguments = models.JSONField(default=dict)
    inputs = models.JSONField(default=dict)
    status = models.CharField(max_length=30, default="running")
    result = models.JSONField(default=dict)
    context_patch = models.JSONField(default=dict)
    attempt = models.PositiveIntegerField(default=1)
    claim = models.CharField(max_length=64)
    lease_until = models.DateTimeField()
    error = models.JSONField(default=dict)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "call_key"], name="uniq_run_tool_call")]


class RunArtifact(models.Model):
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="artifacts_v2")
    artifact_id = models.CharField(max_length=160)
    kind = models.CharField(max_length=60)
    title = models.CharField(max_length=240)
    uri = models.CharField(max_length=500)
    preview_uri = models.CharField(max_length=500, blank=True, default="")
    mime_type = models.CharField(max_length=120, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    evidence_refs = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "artifact_id"], name="uniq_run_artifact")]


class RunEvidence(models.Model):
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="evidence_v2")
    evidence_id = models.CharField(max_length=160)
    kind = models.CharField(max_length=60)
    scene_id = models.CharField(max_length=160, blank=True, default="")
    asset_id = models.CharField(max_length=160, blank=True, default="")
    metric = models.CharField(max_length=120, blank=True, default="")
    value = models.JSONField(null=True, blank=True)
    method = models.CharField(max_length=240, blank=True, default="")
    aoi = models.JSONField(null=True, blank=True)
    mask_statistics = models.JSONField(default=dict, blank=True)
    data_contract = models.JSONField(default=dict, blank=True)
    confidence = models.FloatField(null=True, blank=True)
    limitations = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "evidence_id"], name="uniq_run_evidence")]


class ExecutionEvent(models.Model):
    """可恢复的 Agent 执行事件；UI projection 不再是唯一事实来源。"""

    session = models.ForeignKey(AgentSession, on_delete=models.CASCADE, related_name="execution_events")
    sequence = models.PositiveIntegerField()
    kind = models.CharField(max_length=40)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(fields=["session", "sequence"], name="uniq_agent_event_sequence"),
        ]
        indexes = [models.Index(fields=["session", "sequence"])]


class AnalysisRun(models.Model):
    """一次可复现的遥感分析运行记录。"""
    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    query = models.TextField()
    status = models.CharField(max_length=20, default=STATUS_RUNNING)
    scene_ids = models.JSONField(default=list, blank=True)
    parameters = models.JSONField(default=dict, blank=True)
    model_versions = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-started_at"]


class RasterAsset(models.Model):
    scene = models.ForeignKey(ImageryScene, null=True, blank=True, on_delete=models.CASCADE, related_name="raster_assets")
    asset_type = models.CharField(max_length=40)
    file_path = models.CharField(max_length=500)
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    bands = models.JSONField(default=list, blank=True)
    resolution_m = models.FloatField(null=True, blank=True)
    nodata_ratio = models.FloatField(null=True, blank=True)
    checksum = models.CharField(max_length=128, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Finding(models.Model):
    analysis_run = models.ForeignKey(AnalysisRun, on_delete=models.CASCADE, related_name="findings")
    finding_type = models.CharField(max_length=60)
    label = models.CharField(max_length=200)
    bbox = models.JSONField(default=dict, blank=True)
    geometry = models.JSONField(null=True, blank=True)
    area_m2 = models.FloatField(null=True, blank=True)
    confidence = models.FloatField(null=True, blank=True)
    severity = models.CharField(max_length=30, blank=True, default="")
    source_method = models.CharField(max_length=80, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Evidence(models.Model):
    finding = models.ForeignKey(Finding, on_delete=models.CASCADE, related_name="evidence")
    scene = models.ForeignKey(ImageryScene, null=True, blank=True, on_delete=models.SET_NULL)
    asset = models.ForeignKey(RasterAsset, null=True, blank=True, on_delete=models.SET_NULL)
    evidence_type = models.CharField(max_length=40)
    crop_file = models.CharField(max_length=500, blank=True, default="")
    bbox = models.JSONField(default=dict, blank=True)
    metric = models.CharField(max_length=80, blank=True, default="")
    metric_value = models.FloatField(null=True, blank=True)
    description = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)


class ReportJob(models.Model):
    """持久化 Agent Word 报告任务，避免生成过程绑定在 HTTP 请求线程。"""

    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"

    agent_session = models.ForeignKey(
        AgentSession, null=True, blank=True, on_delete=models.CASCADE, related_name="report_jobs"
    )
    request_key = models.CharField(max_length=120, unique=True)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, default=STATUS_QUEUED)
    result = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    attempts = models.PositiveIntegerField(default=0)
    worker_claim = models.CharField(max_length=80, blank=True, default="")
    claimed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]


class ExternalServiceHealth(models.Model):
    """跨进程共享的外部服务短期健康/熔断状态。"""

    service_key = models.CharField(max_length=120, unique=True)
    failure_count = models.PositiveIntegerField(default=0)
    open_until = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    last_error_type = models.CharField(max_length=40, blank=True, default="")
    endpoint = models.CharField(max_length=500, blank=True, default="")
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    last_http_status = models.PositiveIntegerField(null=True, blank=True)
    last_retry_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["service_key"]


class ApiRateLimitBucket(models.Model):
    """跨进程共享的 API 令牌桶；key 只保存客户端标识哈希。"""

    bucket_key = models.CharField(max_length=80, unique=True)
    tokens = models.FloatField(default=0)
    capacity = models.PositiveIntegerField(default=1)
    last_refill_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["bucket_key"]


class Conversation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner_session_key = models.CharField(max_length=64, db_index=True)
    title = models.CharField(max_length=200, default="新的影像问答")
    event_sequence = models.PositiveIntegerField(default=0)
    context_version = models.PositiveIntegerField(default=1)
    active_run = models.ForeignKey(AgentRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="active_in_conversations")
    workspace = models.JSONField(default=dict, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    archived = models.BooleanField(default=False)
    starred = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]


class SpatialAttachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner_session_key = models.CharField(max_length=64, db_index=True)
    conversation = models.ForeignKey(Conversation, null=True, blank=True, on_delete=models.SET_NULL, related_name="attachments")
    scene = models.ForeignKey(ImageryScene, null=True, blank=True, on_delete=models.SET_NULL, related_name="attachments_v3")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="revisions")
    name = models.CharField(max_length=240)
    kind = models.CharField(max_length=30, default="image")
    status = models.CharField(max_length=30, default="uploading")
    coordinate_space = models.CharField(max_length=30, default="image_pixels")
    file_path = models.CharField(max_length=500, blank=True, default="")
    preview_path = models.CharField(max_length=500, blank=True, default="")
    sha256 = models.CharField(max_length=64, blank=True, default="")
    size_bytes = models.BigIntegerField(default=0)
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    bbox = models.JSONField(null=True, blank=True)
    geometry = models.JSONField(null=True, blank=True)
    crs = models.CharField(max_length=120, blank=True, default="")
    transform = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")
    processing_claim = models.CharField(max_length=64, blank=True, default="")
    processing_lease_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class ConversationMessage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    run = models.ForeignKey(AgentRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="conversation_messages")
    role = models.CharField(max_length=20)
    content = models.TextField(blank=True, default="")
    parts = models.JSONField(default=list, blank=True)
    attachments = models.ManyToManyField(SpatialAttachment, related_name="messages", blank=True)
    status = models.CharField(max_length=30, default="queued")
    delivery = models.CharField(max_length=20, default="steer")
    sequence = models.PositiveIntegerField()
    request_id = models.CharField(max_length=120)
    request_digest = models.CharField(max_length=64)
    context_version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence"]
        constraints = [models.UniqueConstraint(fields=["conversation", "request_id"], name="v3_message_request"), models.UniqueConstraint(fields=["conversation", "sequence"], name="v3_message_sequence")]


class ConversationEvent(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="events")
    sequence = models.PositiveIntegerField()
    type = models.CharField(max_length=80)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence"]
        constraints = [models.UniqueConstraint(fields=["conversation", "sequence"], name="v3_event_sequence")]


class AgentTurn(models.Model):
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="turns")
    number = models.PositiveIntegerField()
    context_version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=30, default="started")
    decision = models.JSONField(default=dict)
    usage = models.JSONField(default=dict)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "number"], name="v3_turn_number")]


class SpatialObservation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(Conversation, null=True, blank=True, on_delete=models.SET_NULL, related_name="observations")
    attachment = models.ForeignKey(SpatialAttachment, on_delete=models.CASCADE, related_name="observations")
    run = models.ForeignKey(AgentRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="observations")
    label = models.CharField(max_length=200)
    kind = models.CharField(max_length=40, default="window")
    window = models.JSONField(default=list)
    geometry = models.JSONField(null=True, blank=True)
    summary = models.TextField(blank=True, default="")
    confidence = models.FloatField(null=True, blank=True)
    evidence_refs = models.JSONField(default=list, blank=True)
    preview_path = models.CharField(max_length=500, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    context_version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)


class AttachmentUpload(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    attachment = models.OneToOneField(SpatialAttachment, on_delete=models.CASCADE, related_name="upload")
    size_bytes = models.BigIntegerField()
    chunk_size = models.PositiveIntegerField(default=1024 * 1024)
    received_chunks = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=30, default="uploading")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class LegacyConversationLink(models.Model):
    source_model = models.CharField(max_length=40)
    source_pk = models.PositiveIntegerField()
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="legacy_links")
    migrated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source_model", "source_pk"], name="v3_legacy_link")]
