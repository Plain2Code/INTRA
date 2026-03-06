FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Dashboard port
EXPOSE 8080

# Inside Docker, uvicorn must bind to 0.0.0.0 so Docker can forward traffic.
# The 127.0.0.1 restriction is enforced at the host level (-p 127.0.0.1:8080:8080).
ENV DASHBOARD_HOST=0.0.0.0

# Default: demo mode. Pass --live via CMD override for live trading.
CMD ["python", "main.py"]
