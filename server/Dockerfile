FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY live_collector.py .
COPY data ./data
CMD ["python", "-u", "live_collector.py"]
