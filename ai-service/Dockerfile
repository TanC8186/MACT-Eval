FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04 AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# CUDA runtime image does not include Python. Keep the image minimal.
RUN sed -i 's|archive.ubuntu.com|mirrors.aliyun.com|g; s|security.ubuntu.com|mirrors.aliyun.com|g' /etc/apt/sources.list && \
    apt-get -o Acquire::Retries=20 update && \
    apt-get -o Acquire::Retries=20 --fix-missing install -y --no-install-recommends \
      python3 python3-venv python3-pip libgomp1 ca-certificates && \
    ln -sf /usr/bin/python3 /usr/local/bin/python && \
    ln -sf /usr/bin/python3 /usr/local/bin/python3 && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# 安装链必须 fail-fast：任一 pip install 失败都要让构建失败。
# 只有「卸载 GUI 版 OpenCV」允许失败（未安装时 pip uninstall 返回非零），
# 因此容错只包住该子命令，而不是整条 && 链。
RUN python -m pip install --upgrade pip && \
    python -m pip install --index-url https://download.pytorch.org/whl/cu124 \
        torch==2.5.1 torchvision==0.20.1 && \
    python -m pip install -r requirements.txt && \
    (python -m pip uninstall -y opencv-python opencv-contrib-python || true) && \
    python -m pip install --no-cache-dir --force-reinstall --no-deps \
        opencv-python-headless==4.11.0.86 && \
    python - <<'PY'
import os
import cv2, numpy, fastapi, redis, minio, faster_whisper, ultralytics, torch
assert hasattr(cv2, "CascadeClassifier"), "cv2.CascadeClassifier missing"
cascade = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
assert os.path.exists(cascade), f"OpenCV cascade data missing: {cascade}"
print("ai-service dependency check ok:", cv2.__version__, torch.__version__)
PY

COPY . .

EXPOSE 8001
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
