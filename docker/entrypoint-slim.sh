#!/bin/sh
# vim:set et ts=8 sts=4 sw=4 ai fenc=utf-8:

set -e

# Docker selects the backend through the environment before Python starts.
STORAGE_BACKEND=${STORAGE_BACKEND-git}
case "$STORAGE_BACKEND" in
    git|files) ;;
    *) echo "Fatal: STORAGE_BACKEND must be git or files." >&2; exit 1 ;;
esac
export STORAGE_BACKEND

# take care of repository directory
if [ ! -d "$OTTERWIKI_REPOSITORY" ]; then
    mkdir -p "$OTTERWIKI_REPOSITORY"
fi

if [ "$STORAGE_BACKEND" = "git" ]; then
    if [ ! -d "$OTTERWIKI_REPOSITORY/.git" ]; then
        git init -b main "$OTTERWIKI_REPOSITORY"
    fi

    # configure git
    cd "$OTTERWIKI_REPOSITORY"
    git config user.name "Otterwiki Robot"
    git config user.email "noreply@otterwiki"
fi

# take care of the otterwiki settings file
if [ ! -f "$OTTERWIKI_SETTINGS" ]; then
    python3 - "$OTTERWIKI_REPOSITORY" >> "$OTTERWIKI_SETTINGS" <<'PY'
import secrets
import sys

print("DEBUG = False")
print(f"REPOSITORY = {sys.argv[1]!r}")
print(f"SECRET_KEY = {secrets.token_hex(32)!r}")
print("SQLALCHEMY_DATABASE_URI = 'sqlite:////app-data/db.sqlite'")
PY
fi

# install plugins found in /app-data/plugins and /plugins
for PLUGIN in /app-data/plugins/*/ /plugins/*/; do
    test -d "$PLUGIN" || continue
    echo Installing: $PLUGIN
    cd "$PLUGIN"
    pip install -U . || echo "Error: Installation of plugin in $PLUGIN failed." >&2
done

# Get the absolute path of the static files from the environment variable
export USE_STATIC_PATH=${STATIC_PATH:-'/app/otterwiki/static'}
# Configure UWSGI via ENV
export UWSGI_STATIC_MAP="/static=$USE_STATIC_PATH"

exec "$@"
