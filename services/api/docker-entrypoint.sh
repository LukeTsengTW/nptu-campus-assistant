#!/bin/sh
set -eu

alembic -c /app/alembic.ini upgrade head
exec uvicorn nptu_assistant.main:app --host 0.0.0.0 --port 8000
