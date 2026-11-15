FROM python:3.12-slim
# ffmpeg para sacar el audio de la cámara; gcc por si webrtcvad hay que compilarlo.
RUN apt-get update \
  && apt-get install -y --no-install-recommends ffmpeg gcc \
  && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "webrtcvad-wheels>=2.0,<3"
WORKDIR /app
COPY oido.py .
ENV PYTHONUNBUFFERED=1
CMD ["python", "oido.py"]
