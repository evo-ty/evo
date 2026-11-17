# EVO HUB

EVO HUB is a Python/Flask hosting control panel. The project includes the web dashboard, account/admin screens, server management, file workspace, and the Docker runner materials.

## Requirements

- Python 3.11 or newer
- Dependencies from `requirements.txt`
- A Linux host for production deployment
- Docker Engine when deploying untrusted user projects with container isolation

## Configure administrator access

Set these values in your hosting provider's environment-variable settings (not in source code):

| Variable | Purpose |
|---|---|
| `ADMIN_USERNAME` | Administrator username; set a private username of your choice |
| `ADMIN_EMAIL` | Administrator email; default is `admin@evo.local` |
| `ADMIN_PASSWORD` | Required for a known, persistent administrator password |
| `SECRET_KEY` | Long random Flask session secret |
| `PUBLIC_IP` | Public address used for server links |
| `PORT` | Web application port; defaults to `13506` |

The source intentionally does **not** contain the administrator password. If `ADMIN_PASSWORD` is not configured, the application uses a random fallback, so set it before deployment to ensure you can sign in.

## Install and run

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export ADMIN_USERNAME='your-private-admin-username'
export ADMIN_EMAIL='admin@evo.local'
export ADMIN_PASSWORD='set-a-long-unique-password-in-your-host-settings'
export SECRET_KEY='set-a-long-random-secret-in-your-host-settings'
export PUBLIC_IP='YOUR_SERVER_PUBLIC_IP'
python3 main.py
```

In production, enter secrets in your hosting provider's environment settings rather than shell history or committed files. Do not reuse passwords used for other accounts.

## Repository hygiene

The runtime SQLite database, uploaded user files, local environment files, and credential-repair scripts are intentionally excluded. A fresh deployment creates its own database. Back up runtime data separately and keep backups private.

## Docker and isolation

Review `docker/README.md` and `docker/SECURE_ISOLATION.md` before enabling user project execution. A per-user folder alone is not a complete isolation boundary; production hosting of untrusted projects requires an appropriately configured container/sandbox environment.

## Licensing and attribution

The supplied source archive did not include a `LICENSE` file. Confirm the applicable license and your right to redistribute the upstream/source materials before making this public or granting others reuse rights. Existing third-party copyright and attribution notices, where present, should be retained.
