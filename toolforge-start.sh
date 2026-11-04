#!/bin/sh
set -eu

# KoCaseImport 0.2.0 uses a local SQLite queue. Keep the worker in the same
# Toolforge webservice container so the web UI and worker share /tmp.
python -m kocaseimport.init_db
python -m kocaseimport.worker &

exec gunicorn \
  --workers 1 \
  --bind 0.0.0.0:8000 \
  --forwarded-allow-ips='*' \
  'kocaseimport.app:create_app()'
