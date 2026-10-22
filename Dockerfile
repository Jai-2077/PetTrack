FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PETTRACK_MODE=cloud PETTRACK_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
VOLUME /data
EXPOSE 5000
CMD ["python", "run.py"]
