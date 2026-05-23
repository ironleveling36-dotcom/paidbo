# ── Swiggy Offer Telegram Bot ──────────────────────────────────────────────────
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Install Python dependencies (includes psycopg2-binary for Postgres)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-install Chromium browser
RUN playwright install chromium --with-deps

# Copy source
COPY bot.py database.py ./

CMD ["python", "bot.py"]
