FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY sienner ./sienner
RUN pip install --no-cache-dir .

# Non-root: nothing here needs to write to the filesystem.
RUN useradd --create-home --uid 10001 sienner
USER sienner

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "sienner.main:app", "--host", "0.0.0.0", "--port", "8000"]
