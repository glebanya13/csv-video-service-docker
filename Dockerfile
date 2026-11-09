# Apache 2.4 + mod_wsgi + Flask video web service.

# ---- build stage: compile mod_wsgi and psycopg2 against this Python and Apache ----
FROM python:3.12-slim-bookworm AS build

RUN apt-get -o Acquire::Retries=10 update \
 && apt-get -o Acquire::Retries=10 install -y --no-install-recommends build-essential apache2-dev libpq-dev \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r /tmp/requirements.txt

# ---- runtime stage ----
FROM python:3.12-slim-bookworm

RUN apt-get -o Acquire::Retries=10 update \
 && apt-get -o Acquire::Retries=10 install -y --no-install-recommends apache2 libpq5 curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index /wheels/* && rm -rf /wheels

# Load the mod_wsgi built for this Python (mod_wsgi-express prints the LoadModule line).
RUN mod_wsgi-express module-config > /etc/apache2/mods-available/wsgi.load \
 && a2enmod wsgi \
 && a2dissite 000-default \
 && sed -i 's/^Listen .*/Listen 80/' /etc/apache2/ports.conf

COPY apache/video-service.conf /etc/apache2/sites-available/video-service.conf
RUN a2ensite video-service

WORKDIR /app
COPY web_service.py web_service.wsgi docker-entrypoint.sh ./
# Do not rely on the host's file mode or line endings: a copy made via Windows or a zip
# loses the +x bit and may turn LF into CRLF ("permission denied" / "no such file").
RUN sed -i 's/\r$//' docker-entrypoint.sh && chmod 755 docker-entrypoint.sh
# The named volume is mounted here; on first use Docker copies this owner into it.
RUN mkdir -p /app/uploads && chown www-data:www-data /app/uploads

ENV PYTHONUNBUFFERED=1 \
    UPLOAD_DIR=/app/uploads

EXPOSE 80
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost/health || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["apache2ctl", "-D", "FOREGROUND"]
