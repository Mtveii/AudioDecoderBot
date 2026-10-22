FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    ca-certificates \
    procps \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -r -m bot

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chown -R bot:bot /app

USER bot

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=15s \
    CMD pgrep -f "run.py|bot.py" || exit 1

CMD ["python", "run.py"]
