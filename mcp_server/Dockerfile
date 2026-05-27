FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml /app/
COPY src/ /app/src/

RUN pip install --no-cache-dir .

EXPOSE 8401

CMD ["jhe-mcp-http"]
