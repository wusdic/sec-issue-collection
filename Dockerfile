FROM python:3.12-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt || pip install --no-cache-dir \
    fastapi uvicorn "sqlalchemy>=2.0" "pydantic>=2.6" httpx PyYAML jsonschema typer \
    beautifulsoup4 lxml python-dateutil celery redis psycopg2-binary

COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
