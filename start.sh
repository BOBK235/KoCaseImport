#!/bin/sh
set -eu
exec gunicorn --bind "0.0.0.0:${PORT:-8000}" --workers "${WEB_CONCURRENCY:-2}" 'kocaseimport.app:create_app()'
