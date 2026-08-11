# Use official Python runtime as base image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY data/ ./data/
COPY docs/ ./docs/

# Create logs directory
RUN mkdir -p logs

# Expose port (Railway will set PORT env var)
EXPOSE 8080

# Run the application
# Railway provides PORT environment variable, default to 8080
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}