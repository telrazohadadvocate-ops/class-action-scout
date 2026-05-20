FROM python:3.11-slim

# Needed before playwright install-deps runs apt internally
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer caches before browser download)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium + all its system dependencies in one step.
# --with-deps calls apt internally so no manual lib list needed.
RUN playwright install chromium --with-deps

# Copy the rest of the project
COPY . .

# Ensure the persistent-disk mount point exists (Render creates it but
# the image needs the path so code that references it doesn't fail locally)
RUN mkdir -p /var/data

EXPOSE 5000

CMD ["sh", "-c", "gunicorn -w 2 -b 0.0.0.0:${PORT:-5000} app:app"]
