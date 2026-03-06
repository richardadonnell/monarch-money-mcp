FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY server.py .

EXPOSE 8000

CMD ["python", "server.py"]
