FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.json run.py ./
COPY autobiz ./autobiz
COPY static ./static

# 默认跑 24/7 守护循环；控制台容器用 command: python run.py dashboard
CMD ["python", "run.py", "daemon", "--interval", "3600"]
