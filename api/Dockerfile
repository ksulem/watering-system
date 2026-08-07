FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copied as a package so modules can import each other as `from api import db`.
COPY . ./api/

# One worker: this serves a single user, and each extra worker is another
# asyncpg pool against a 1 GB box's Postgres.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
