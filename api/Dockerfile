FROM python:3.11-slim-bookworm

# psycopg[binary] 自带 libpq，无需额外系统依赖
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# 单 worker 单进程：连接池上限 2 才严格等价于“单 API 进程最多两条连接”
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
