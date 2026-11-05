# Recipe for building the Airwave Archive container.
FROM python:3.13-slim

# Don't write .pyc files; print logs immediately.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run as an ordinary user, not the all-powerful "root" user.
RUN useradd --create-home --uid 1000 airwave && mkdir /data /inbox && chown airwave /data /inbox
USER airwave

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
