# Portali huquqi — application image (Flask + gunicorn). Data lives in the /data volume.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    INSTANCE_DIR=/data \
    PORTAL_TZ=Asia/Dushanbe

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --system --uid 10001 portal && mkdir -p /data && chown portal /data
USER portal

EXPOSE 8000
# init-db creates or upgrades the database on every start; it never adds demo content.
CMD ["sh", "-c", "flask --app app init-db && exec gunicorn --bind 0.0.0.0:8000 --workers ${WEB_WORKERS:-3} --threads 4 --timeout 120 --access-logfile - app:app"]
