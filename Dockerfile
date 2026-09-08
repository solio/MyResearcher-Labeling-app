FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py storage.py ./
COPY static/ static/
COPY schema/ schema/
COPY tools/ tools/
EXPOSE 8787
CMD ["python3", "server.py", "--config", "config.json", "--port", "8787"]
