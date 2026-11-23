#!/usr/bin/env bash
# ViMi Pay: read-only audit of running Docker/PostgreSQL. No credentials printed.
set -euo pipefail
python3 - <<'PY'
import json
import subprocess
from urllib.parse import urlsplit, unquote

PUBLIC = {'postgres', 'membership', 'membership_runtime'}
ROLES = [('membership', 'membership'), ('membership_runtime', 'membership_runtime'), ('postgres', 'postgres')]

def cmd(argv, timeout=15):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None

def status(value):
    if value is None:
        return 'NOT_SET'
    if value == '':
        return 'EMPTY'
    return 'PUBLIC_DEFAULT' if value in PUBLIC else 'OTHER_VALUE'

def env_dict(info):
    return dict(item.split('=', 1) for item in info.get('Config', {}).get('Env', []) if '=' in item)

listed = cmd(['docker', 'ps', '-q'])
if not listed or listed.returncode:
    print('DOCKER_ACCESS=FAILED (check Docker permissions)')
    raise SystemExit(0)
ids = listed.stdout.split()
if not ids:
    print('RUNNING_CONTAINERS=0')
    raise SystemExit(0)
inspected = cmd(['docker', 'inspect', *ids])
if not inspected or inspected.returncode:
    print('DOCKER_INSPECT=FAILED')
    raise SystemExit(0)
items = json.loads(inspected.stdout)
selected = []
for info in items:
    labels = info.get('Config', {}).get('Labels') or {}
    project = labels.get('com.docker.compose.project', '')
    name = info.get('Name', '').lstrip('/')
    if 'telegram-membership-saas' in project or 'vimi-pay' in project or 'telegram-membership-saas' in name:
        selected.append((info, labels, project))
print('VIMI_CONTAINERS=' + str(len(selected)))
for info, labels, project in selected:
    env = env_dict(info)
    service = labels.get('com.docker.compose.service', 'unknown')
    cid = info['Id']
    print('\nPROJECT=' + project + ' SERVICE=' + service)
    print('ENVIRONMENT=' + env.get('TMS_ENVIRONMENT', 'NOT_SET'))
    for key in ('TMS_DATABASE_URL', 'TMS_MIGRATION_DATABASE_URL', 'TMS_SHORTLINK_DATABASE_URL'):
        value = env.get(key)
        try:
            password = unquote(urlsplit(value).password or '') if value else None
            print(key + '_PASSWORD=' + status(password))
        except ValueError:
            print(key + '_PASSWORD=UNPARSEABLE')
    if service != 'postgres':
        continue
    print('POSTGRES_BOOTSTRAP_ENV_PASSWORD=' + status(env.get('POSTGRES_PASSWORD')))
    dbuser = env.get('POSTGRES_USER') or 'postgres'
    dbname = env.get('POSTGRES_DB') or dbuser
    sql = (
        "SELECT 'DB_QUERY=OK'; "
        "SELECT 'ROLE_'||rolname||'_LOGIN='||rolcanlogin::text||'_SUPERUSER='||rolsuper::text "
        "FROM pg_roles WHERE rolname IN ('membership','membership_runtime','postgres') ORDER BY rolname; "
        "SELECT 'HOST_AUTH_METHODS='||coalesce(string_agg(DISTINCT auth_method, ','), 'unknown') "
        "FROM pg_hba_file_rules WHERE type LIKE 'host%';"
    )
    base = ['docker', 'exec', '-i', cid, 'psql', '-X', '-w', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-U', dbuser, '-d', dbname]
    metadata = cmd([*base, '-c', sql])
    if metadata and metadata.returncode == 0:
        for line in metadata.stdout.splitlines():
            if line.startswith(('DB_QUERY=', 'ROLE_', 'HOST_AUTH_METHODS=')):
                print(line)
    else:
        print('DB_QUERY=UNAVAILABLE (Docker local PostgreSQL access failed)')

    def attempt(role, candidate):
        check = cmd([
            'docker', 'exec', '-i', '-e', 'PGPASSWORD=' + candidate, cid,
            'psql', '-X', '-w', '-A', '-t', '-h', '127.0.0.1', '-U', role,
            '-d', dbname, '-c', 'SELECT 1;'
        ], timeout=8)
        if check is None:
            return 'UNAVAILABLE'
        if check.returncode == 0 and check.stdout.strip() == '1':
            return 'ACCEPTED'
        # An explicit auth failure is different from timeout, SQL error,
        # missing role, network failure or Docker daemon unavailability.
        if 'password authentication failed' in check.stderr.lower():
            return 'REJECTED'
        return 'UNAVAILABLE'

    for role, public_pw in ROLES:
        public = attempt(role, public_pw)
        # Compare the same source and role with a deliberately invalid secret.
        # This distinguishes password auth from trust/cert/other no-password auth.
        invalid = attempt(role, '__vimi_invalid_password_probe_20261123__')
        if public == 'ACCEPTED' and invalid == 'REJECTED':
            result = 'PUBLIC_PASSWORD_CONFIRMED'
        elif public == 'ACCEPTED' and invalid == 'ACCEPTED':
            result = 'ACCEPTED_UNVERIFIED_AUTH'
        elif public == 'REJECTED' and invalid == 'REJECTED':
            result = 'PUBLIC_PASSWORD_NOT_ACCEPTED'
        else:
            result = 'UNAVAILABLE'
        print('PUBLIC_PASSWORD_LOGIN_' + role + '=' + result)
print('\nAUDIT_FINISHED=READ_ONLY; NO_SECRETS_PRINTED; NO_DATA_CHANGED')
PY
