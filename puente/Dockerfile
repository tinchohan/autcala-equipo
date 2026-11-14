FROM python:3.12-slim
RUN pip install --no-cache-dir "paho-mqtt>=2,<3"
WORKDIR /app
COPY puente.py .
ENV PYTHONUNBUFFERED=1
CMD ["python", "puente.py"]
