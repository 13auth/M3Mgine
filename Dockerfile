FROM python:3.11-slim

WORKDIR /app
COPY engine/ /app/engine/
RUN pip install --no-cache-dir -r /app/engine/requirements.txt

# Konteynerde 0.0.0.0 — ama ÖNÜNDE TLS sonlandıran reverse proxy çalıştır (README #24).
ENV CCE_HOST=0.0.0.0 \
    CCE_PORT=8770 \
    CCE_DB=/data/cce.db \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8770

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8770/health',timeout=2).status==200 else 1)"

CMD ["python", "/app/engine/api.py"]
