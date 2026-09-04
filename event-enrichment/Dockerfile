# The container never launches a browser - it attaches to Chrome running on
# the host - so no Playwright browser download is needed. Keeps the image
# to ~450MB instead of the ~2GB a browser-bundled image would need.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN mkdir -p /app/data

ENV BROWSER_MODE=cdp CDP_HOST=host.docker.internal CDP_PORT=9222 PORT=8000
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
