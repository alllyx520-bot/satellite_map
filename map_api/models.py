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
