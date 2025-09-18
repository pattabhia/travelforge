# ---- Base ----
FROM python:3.12-slim

# Prevent .pyc, ensure unbuffered logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    PORT=8080

# (Optional) system deps if you ever need them
# RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*

# ---- Install deps using cache ----
WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade --no-cache-dir pip && \
    pip install --no-cache-dir -r requirements.txt

# ---- App code ----
COPY . .

# Create a non-root user and ensure write access (Streamlit creates /.streamlit)
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# Use $PORT if the platform sets it (Cloud Run etc.)
CMD ["sh", "-c", "streamlit run app.py --server.port=${PORT} --server.address=0.0.0.0"]
