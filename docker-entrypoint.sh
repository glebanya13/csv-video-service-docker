#!/bin/sh
# Wait for PostgreSQL and create the schema before Apache starts accepting requests.
set -e

python /app/web_service.py wait-db

# Apache's own pid file from a previous run would block a restart of the container.
rm -f /var/run/apache2/apache2.pid
exec "$@"
