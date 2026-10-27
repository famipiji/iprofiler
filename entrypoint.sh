#!/bin/sh
set -e

# docker-compose mounts ./templates:/app/templates over the image's baked-in
# templates/ directory (for local hot-reloading without a rebuild). If the
# host directory is missing or empty at deploy time, Docker silently binds an
# empty directory over the image copy, hiding index.html and breaking every
# render_template() call with no clear error. Fail fast here instead.
if [ ! -f "/app/templates/index.html" ]; then
    echo "FATAL: /app/templates/index.html not found." >&2
    echo "The './templates' bind mount in docker-compose.yml is pointing at a missing or empty directory on the host, which hides the image's built-in templates." >&2
    echo "Fix: make sure the host's ./templates directory exists and contains index.html before running docker-compose up." >&2
    exit 1
fi

exec waitress-serve --host=0.0.0.0 --port=5000 app:app
