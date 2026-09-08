# 基础镜像取自私有仓库（服务器拉不到 Docker Hub）。已推过一次（amd64），此后常驻
# registry、无需再推；服务端构建首次 pull 后走本地缓存。本机构建验证时用
# --build-arg BASE_IMAGE=<公网端点>/fangzuzu/python:3.12-slim 覆盖。
ARG BASE_IMAGE=fangzuzu-docker-registry-vpc.cn-guangzhou.cr.aliyuncs.com/fangzuzu/python:3.12-slim
FROM ${BASE_IMAGE}
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
