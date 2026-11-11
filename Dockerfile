FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY btcbot ./btcbot
ENV PYTHONUNBUFFERED=1 DATA_DIR=/app/data
EXPOSE 8080
VOLUME ["/app/data"]
CMD ["python", "-m", "btcbot", "run"]
