# Base image official Python 3.10
FROM python:3.10-slim

# System update, FFmpeg aur basic utilities install karein
RUN apt-get update && apt-get install -y \
    ffmpeg \
    wget \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Work directory set karein
WORKDIR /app

# Python packages copy aur install karein
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright ke Chromium browser aur uski sabhi system dependencies install karein
RUN playwright install chromium
RUN playwright install-deps chromium

# Project ki baaki sabhi files copy karein
COPY . .

# Environment Variable set karein
ENV PORT=8080

# Main script ko run karne ke liye update ki gayi command
CMD ["python", "main.py"]
