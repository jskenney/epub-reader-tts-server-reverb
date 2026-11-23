# Reverb-TTS with the EPUB reader's voice-server API (API.md), on the GPU. PyTorch 2.9.1 with CUDA 12.8 runs on
# Blackwell (RTX 50xx, RTX PRO Blackwell) as well as older cards.
FROM pytorch/pytorch:2.9.1-cuda12.8-cudnn9-runtime
# ffmpeg decodes reference clips in any format; curl is for the health check.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
# The weights (about 1.4 GB, fetched from the Hugging Face Hub on the first start) are kept in /models, a volume.
ENV HF_HOME=/models REVERB_VARIANT=fp16 PYTHONUNBUFFERED=1
EXPOSE 7860
CMD ["python", "app.py"]
