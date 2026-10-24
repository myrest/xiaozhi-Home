FROM python:3.13-alpine3.22@sha256:7f7ee17311e0273954ffe76a7b84a2d8c8ecf16f9992e780e4d46c86175df431
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apk add --no-cache ffmpeg=6.1.2-r2
WORKDIR /app
COPY services/mcp/requirements.lock.txt ./requirements.lock.txt
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY services/mcp/bedtime_mcp ./bedtime_mcp
COPY services/mcp/config.json ./config.json
RUN mkdir -p /data && chown 1000:1000 /data
USER 1000:1000
CMD ["python", "-m", "bedtime_mcp.server"]
