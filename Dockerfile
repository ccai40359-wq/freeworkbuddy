FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEBBUDDY_DATA_DIR=/var/lib/webbuddy \
    WEBBUDDY_SECURE_COOKIE=true

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin webbuddy \
    && mkdir -p /var/lib/webbuddy \
    && chown -R webbuddy:webbuddy /app /var/lib/webbuddy

COPY webgui.py converter.py desensitize.py ./
COPY webbuddy/ ./webbuddy/

USER webbuddy
EXPOSE 8788

CMD ["python", "webgui.py", "--host", "0.0.0.0", "--port", "8788", "--data-dir", "/var/lib/webbuddy", "--desensitize", "--secure-cookie"]
