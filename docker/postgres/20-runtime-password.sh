#!/bin/sh
# Runs only at FIRST PostgreSQL data-directory initialization.
# Do not rely on this hook to rotate credentials on an existing Docker volume.
set -eu

: "${VIMI_PG_RUNTIME_PASSWORD:?VIMI_PG_RUNTIME_PASSWORD is required}"
# Generated password alphabet deliberately excludes SQL and URL metacharacters.
case "$VIMI_PG_RUNTIME_PASSWORD" in
    *[!A-Za-z0-9_-]*)
        echo "Invalid runtime password alphabet: use URL-safe letters, digits, _ or -" >&2
        exit 1
        ;;
esac

# Feed the secret through stdin, never process command-line arguments or logs.
# The role was created by 10-runtime.sql; new installations must be authenticated
# before accepting runtime TCP connections.
printf "ALTER ROLE membership_runtime WITH PASSWORD '%s';\n" "$VIMI_PG_RUNTIME_PASSWORD" \
    | psql -X -q -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"
