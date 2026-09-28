FROM python:3.11-slim

WORKDIR /app

# 安裝 ADB 工具與系統依賴
RUN apt-get update && apt-get install -y --no-install-recommends \
    android-tools-adb \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 預設暴露 8000 埠
EXPOSE 8000

CMD ["python", "-m", "lineflow.main"]
