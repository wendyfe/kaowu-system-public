FROM python:3.10-slim

# 安装 pip 和必要系统依赖
RUN apt-get update && apt-get install -y python3-pip && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1. 先复制 requirements.txt 到容器（注意路径：项目根目录的requirements.txt）
COPY requirements.txt .

# 2. 安装所有依赖（包括jinja2）
RUN pip install --no-cache-dir -r requirements.txt

# 3. 复制项目代码
COPY ./app /app

EXPOSE 8000

ENV DB_DIR=/app/db

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]