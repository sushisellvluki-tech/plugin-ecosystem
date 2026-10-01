FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY core ./core
COPY plugins ./plugins
COPY server ./server
COPY scripts/check_tools.py ./scripts/check_tools.py
COPY deploy ./deploy
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin ecosystem
USER 10001:10001
CMD ["python", "-m", "deploy.runtime"]
