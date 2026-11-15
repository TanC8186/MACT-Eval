from app.api.routes import service_status


def test_status_does_not_claim_haar_when_yolo_is_configured(monkeypatch):
    from app.api import routes
    monkeypatch.setattr(routes.settings, "vision_backend", "yolo")
    monkeypatch.setattr(routes.settings, "face_weights", "models/yolov8n.pt")
    payload = __import__("asyncio").run(service_status())
    assert payload["capabilities"]["vision"]["engine"] == "YOLOv8 + OpenCV fallback"
