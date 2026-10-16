# Binance Futures Strict Bot — runtime + test image.
# Build:  docker build -t bfx-bot .
# Test:   docker run --rm bfx-bot
# Paper:  docker run --rm -v $(pwd)/state-paper.json:/app/state-paper.json bfx-bot python run_executor.py --paper --loop
# Live:   NEVER bake keys into the image — pass them at runtime via env files:
#         docker run --rm --env-file /path/to/.binance-live.env bfx-bot python run_executor.py --live --loop
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Default: run the full unit suite (integration tests excluded — they hit
# live public endpoints). Override CMD to run the bot instead.
CMD ["python", "-m", "pytest", "-q", "-m", "not integration"]
