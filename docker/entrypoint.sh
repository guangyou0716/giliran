#!/bin/sh
# Apply migrations (and optionally the synthetic demo data), then run the given command.
set -eu

python manage.py migrate --noinput

if [ "${GILIRAN_SEED_DEMO:-0}" = "1" ]; then
    # seed_demo reads GILIRAN_DEMO_PASSWORD (or prints a random one once); it is idempotent.
    python manage.py seed_demo
fi

exec "$@"
