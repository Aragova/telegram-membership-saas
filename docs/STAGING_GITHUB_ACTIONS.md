# ViMi Pay staging deployment with GitHub Actions

The only workflow that can deploy this VM is
[`.github/workflows/staging-deploy.yml`](../.github/workflows/staging-deploy.yml).
It is **manual-only** (`workflow_dispatch`), from **main**. Pushing or merging
a PR **does not deploy** anything automatically.

## GitHub environment (one-time)

Create **Settings → Environments → staging**, with deployment branch restriction
to **main**. Add the following **environment-scoped** values:

| Scope | Exact name | Contents |
| --- | --- | --- |
| Environment secret | `STAGING_SSH_PRIVATE_KEY` | Dedicated deploy-only ED25519 private key; never commit/paste into logs or chat |
| Environment secret | `STAGING_SSH_KNOWN_HOSTS` | One complete pinned `<IP> ssh-ed25519 <key>` public host-key line, taken directly from the VM's `/etc/ssh/ssh_host_ed25519_key.pub` |
| Environment variable | `STAGING_SSH_HOST` | Public IPv4 address of the staging VM |

Don't configure any of these as repository-level secrets or production values.

## VM security boundary (one-time)

A dedicated `vimi-deploy` account has no password, `docker` group, or
general sudo rights. Its `~/.ssh/authorized_keys` entry uses an ED25519
key with:

```text
restrict,command="/usr/local/sbin/vimi-staging-deploy" ssh-ed25519 ...
```

The root-owned `/usr/local/sbin/vimi-staging-deploy` validates the exact
original SSH command `deploy <40-char lower-case hex SHA>`, and may only run
`sudo -n /usr/local/sbin/vimi-staging-deploy-root <SHA>`. An exact sudoers
rule grants only that script. Do not grant general shell or Docker privileges.

The root-owned deploy script:
- serializes deployments with `flock`;
- verifies the repository, clean tracked tree and `compose.light100.yaml`;
- fetches **origin/main** as the original VM checkout owner;
- requires **origin/main == requested SHA** and a fast-forward from the
  current VM HEAD;
- builds `bot` and `customer-supervisor`;
- runs Alembic migrations before updating the containers;
- restarts **only** those services via existing `compose.light100.yaml`.

It intentionally does not clear databases or access production. The script
and sudoers file must remain owned by root and protected from editing by
the deploy user.

## How to test without deploying

1. In the GitHub repo: **Actions → ViMi Pay | Staging SSH deployment**.
2. Click **Run workflow**, select branch `main`.
3. Choose `operation: connection-test` (default) and run.
4. Open the run's **Validate inputs and run restricted staging SSH command**
   log. Expect `STAGING SSH CONNECTION TEST: SUCCESS`.
   This sends the intentionally invalid original SSH command `preflight`
   and verifies that the forced-command wrapper rejects it with code **2**.
   It does **not** update Git, PostgreSQL or Docker.

If it fails: check VM TCP/22 ingress, the exact environment variable/secret
names, account authorized_keys, and the actual server ED25519 host key. Never
disable StrictHostKeyChecking as a workaround.

## How to deploy an approved main commit

1. Merge the feature PR only after explicit approval, green exact-HEAD CI,
   clean exact-HEAD Codex review, and no unresolved review threads.
2. Confirm that `main` is the commit you want on staging.
3. GitHub **Actions → ViMi Pay | Staging SSH deployment → Run workflow**.
4. Choose branch `main`, **operation: deploy**, then run. Check the logs
   for `DEPLOY SUCCESS: <SHA>` and healthy container status.

The workflow uses the **run's own `GITHUB_SHA`**; it never accepts an
arbitrary user-provided deployment SHA. The VM requires the SHA to still
match current `origin/main`, so an outdated queued run fails safely.

GitHub Actions setup **does not give ChatGPT itself an SSH terminal**.
Its current GitHub connector may inspect workflows and logs but does not
guarantee the ability to trigger `workflow_dispatch`; the user may need to
click **Run workflow**. Do not promise deployment without the resulting logs.

## Operational constraints

- Staging only. No automatic deploy on `push`, `pull_request`, or tags.
- Single queued deployment at a time; `cancel-in-progress: false`.
- No secrets in logs, chat or repository; do not `set -x`.
- Untrusted pull requests never receive staging environment secrets.
- `connection-test` first, then `deploy` only when explicitly desired.
- Do not run a deploy against a dirty staging checkout. The server stops
  rather than clobbering local tracked modifications.
- A deployed `main` commit with a failing migration stops before restarting
  containers. Investigate the failure; do not skip or fake Alembic heads.
- Changing the root-owned script or authorized_keys requires an explicit VM
  admin action, never an automatic GitHub repository update.
