FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app

# Non-root user: even if this process were compromised, it has no
# elevated rights inside its own container (see TECHNICAL_OVERVIEW.md).
RUN useradd --no-create-home --uid 10001 appuser

# Pre-create /data owned by appuser. A fresh named Docker volume mounted onto
# a path that already exists in the image inherits that path's ownership on
# first creation — without this, the volume would default to root-owned and
# the non-root process below could never write jobs.db to it.
RUN mkdir -p /data && chown appuser:appuser /data

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
