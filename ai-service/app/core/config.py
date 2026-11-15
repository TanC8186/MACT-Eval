"""
MACT-Eval AI Service Configuration
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="MACT_AI_")

    # Service
    service_name: str = "mact-eval-ai-service"
    debug: bool = False
    demo_enabled: bool = False
    # 单端交付（2026-10-27 起的范围决定）：正式 Web 入口只有教师/管理员端
    # teacher-web，开发态 5173、容器态 3000。3001 是已下线的学生端，
    # 5174/5175 是双端时代的第二个 Vite 端口，都不能再出现在默认放行列表里
    # ——默认值写着它们，等于在配置层面宣称"还有第二个正式 Web 端"。
    # 需要额外来源时用 MACT_AI_CORS_ORIGINS 显式覆盖（逗号分隔）。
    cors_origins: str = (
        "http://localhost:3000,http://localhost:5173,"
        "http://127.0.0.1:3000,http://127.0.0.1:5173"
    )

    # Server
    host: str = "0.0.0.0"
    port: int = 8001

    # Redis (job queue)
    redis_url: str = "redis://localhost:6379/0"
    job_stream: str = "analysis:jobs"
    job_group: str = "ai-workers"
    poll_interval: int = 1

    # MinIO
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "macteval"
    minio_secret_key: str = "macteval_secret"
    minio_secure: bool = False
    # Evidence thumbnails for human verification of classroom states.
    evidence_frames_enabled: bool = True
    evidence_frames_bucket: str = "analysis-artifacts"
    evidence_frame_max_per_window: int = 4
    evidence_frame_width: int = 960
    evidence_frame_quality: int = 70
    # Dedicated classroom action checkpoint; does not change scoring features.
    behavior_weights: str = ""
    behavior_confidence: float = 0.4
    behavior_imgsz: int = 640
    behavior_max_detections: int = 100

    # Durable result store: an undelivered result must survive a full Redis loss,
    # so it is written to a private MinIO prefix before the stream message is ACKed.
    # See docs/operations/result-delivery-and-recovery-protocol.md.
    result_bucket: str = "analysis-artifacts"
    result_prefix: str = "analysis-results"
    result_schema_version: int = 1
    # Delivered results may expire; undelivered results never expire (alert instead).
    result_retention_after_delivery_seconds: int = 7 * 24 * 3600
    result_pending_alert_seconds: int = 900
    result_read_timeout_seconds: float = 10.0

    # Readiness: /live stays 200 always; /ready needs recent queue communication.
    ready_stale_seconds: float = 15.0
    ready_ping_timeout_seconds: float = 2.0

    # Backend API
    backend_url: str = "http://localhost:8000/api"
    service_token: str = "mact-eval-dev-token"

    # Model paths
    model_dir: str = "../models"

    # Temp media dir for MinIO downloads. Defaulting to the OS temp dir puts
    # analysis copies on the system drive; this project keeps models/data on
    # the E: data disk, so compose sets MACT_AI_WORK_DIR to ./data/tmp/ai-work.
    # Empty means "use the OS temp dir" (only for machines without that rule).
    work_dir: str = ""

    # Vision backend (YOLO preferred, automatic Haar fallback)
    vision_backend: str = "yolo"   # "yolo" | "haar"
    face_weights: str = ""         # empty = auto-detect: runs/detect/face/weights/best.pt -> models/yolov8n-face.pt -> models/yolov8n.pt

    # External LLM (optional, requires de-identification gateway)
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""

    # GPU
    device: str = "cuda"  # or "cpu"

    # Whisper ASR (faster-whisper)
    whisper_model_size: str = "small"
    whisper_device: str = "cuda"  # or "cpu"
    whisper_compute_type: str = "float16"  # int8 for CPU, float16 for CUDA

    # DeepSeek LLM (courseware prep analysis)
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

settings = Settings()
