FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system deps for matplotlib
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpng-dev \
    libfreetype6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-m", "bot.main"]
