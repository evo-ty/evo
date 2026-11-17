# EVO HUB — Public ID workspace isolation

## Storage layout

Every server uses its immutable eight-digit `public_id` as its physical workspace directory. For example, a server with public ID `58955715` uses:

```text
storage/instances/58955715/
```

New servers are created with this layout. Existing server directories are migrated at application startup when the old directory exists and the public-ID directory is available; the database `servers.folder` value is then updated to the public ID.

## File API protection

Upload, listing, reading, saving, renaming, deletion, download, zip, and unzip operations resolve paths through `realpath` and require the eight-digit server ID. Absolute paths, `..` traversal, symlink escapes, and archive members outside the selected workspace are rejected. Ownership checks prevent a normal account from accessing another account’s server.

Terminal working-directory state is also rooted in the selected public-ID directory and cannot move above that root.

## Host execution safety

Running arbitrary user code directly as a host process is unsafe because Python can use absolute paths, inspect the host filesystem, or spawn child processes. EVO HUB now fails closed: server start, dependency installation, and terminal command execution return `503` unless `EVO_HUB_ALLOW_UNSAFE_HOST_EXECUTION=1` is explicitly set. That variable must never be set in production.

Production user code must be launched through the provided Docker runner. The Docker runner uses a per-server container, a non-root user, a mounted project workspace, dropped capabilities, no-new-privileges, CPU/RAM/PID limits, a read-only root filesystem, and a private network. Docker must be installed and integrated on the deployment host before enabling server execution.

## Verification

The automated checks confirmed that current database-backed servers have `folder == public_id` and matching physical directories. A normal user could list their own server (`200`), received `400` for `../` traversal, and received `403` when requesting another owner’s server. A multipart upload returned `200` and placed the probe file inside the selected public-ID directory. After the security hardening, start, terminal, and install each return `503` when no Docker sandbox is configured.

## Docker integration status

The Flask lifecycle now calls `docker/run_server.sh` for start/restart when `EVO_HUB_DOCKER_ENABLED=1` and Docker is installed. Stop and delete remove the per-server container and network, and Terminal uses `docker exec` rather than host `subprocess` in Docker mode. If Docker is missing, EVO HUB remains fail-closed and refuses to run user code.

The current sandbox does not have the Docker binary, so the Docker path was syntax-checked but not started here. Install Docker on the real deployment host, protect the `docker/` directory from the service account, set `EVO_HUB_DOCKER_ENABLED=1`, and do not set `EVO_HUB_ALLOW_UNSAFE_HOST_EXECUTION`.

## Reserved Dockerfile per server

Each Server receives a `Dockerfile` named with its own public-ID workspace. EVO HUB creates it once with mode `0444`, rejects upload/save/rename/delete attempts against it, and the production runner mounts that file read-only inside the container. The visible file is a contract and documentation aid; the actual immutable runtime image and entrypoint remain under the administrator’s `docker/` directory and are not copied from user content.

## Live Docker verification

Docker Engine was installed and enabled in the test environment. Server `51803371` started successfully as container `evo-server-51803371` and remained `running` with restart count `0`. Inside the container, the host path `/home/ubuntu/evo_project/storage/evo.db` was not visible. Writing to `/workspace/Dockerfile` failed, deleting it failed, and the file remained present. This confirms that the user workspace is isolated from the host project and that the reserved Dockerfile is read-only at runtime.

## Startup dependency fix for Server 69263931

The container was starting correctly, but `main.py` exited because the Server's `requirements.txt` was empty while the code imported `telebot`. The dependency file now contains `pyTelegramBotAPI==4.22.0`. On the next container start, the fixed entrypoint installs that dependency into `/tmp/evo-venv` inside the container and executes `/workspace/main.py`; the host environment is not used. Verified result: package installation completed, the application printed its startup message, and the container remained `running` with restart count `0`.
