FROM python:3.11-slim

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY . .

# Railway injects PORT — not used directly but good practice
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Entrypoint
CMD ["python", "main.py"]
