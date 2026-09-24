FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN pip install --no-cache-dir .
COPY docker/entrypoint.sh /usr/local/bin/invoice-entrypoint
RUN chmod 0755 /usr/local/bin/invoice-entrypoint
ENTRYPOINT ["invoice-entrypoint"]
CMD ["uvicorn", "invoice_intelligence.main:app", "--host", "0.0.0.0", "--port", "8000"]
