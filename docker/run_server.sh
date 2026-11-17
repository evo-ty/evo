#!/usr/bin/env bash
set -euo pipefail

# Usage: run_server.sh <public-id> <workspace-dir> <host-port> <python|nodejs>
NAME="${1:?public id required}"
PROJECT="${2:?workspace directory required}"
PORT="${3:?host port required}"
RUNTIME="${4:-python}"

if [[ ! "$NAME" =~ ^[0-9]{8}$ ]]; then echo "public ID must be exactly 8 digits" >&2; exit 2; fi
if [[ ! "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1024 || PORT > 65535 )); then echo "Port must be an integer between 1024 and 65535" >&2; exit 2; fi
if [[ ! -d "$PROJECT" ]]; then echo "Workspace does not exist: $PROJECT" >&2; exit 2; fi

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(realpath "$PROJECT")"
CONTAINER="evo-server-$NAME"
NETWORK="evo-net-$NAME"
case "$RUNTIME" in
  python|python3) IMAGE="evo-hub-python:3.12-deps-v2"; DOCKERFILE="python-server.Dockerfile"; CMD=("python");;
  node|nodejs) IMAGE="evo-hub-node:22"; DOCKERFILE="node-server.Dockerfile"; CMD=("node");;
  *) echo "Unsupported runtime: $RUNTIME" >&2; exit 2;;
esac

# The image and entrypoint live outside every user workspace. User code cannot edit them.
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  # The image contains only the fixed runtime and entrypoint; no user code is in the build context.
  # Disable build networking so image creation does not depend on host iptables rules.
  docker build --network=none -t "$IMAGE" -f "$ROOT_DIR/$DOCKERFILE" "$ROOT_DIR"
fi

docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker network rm "$NETWORK" >/dev/null 2>&1 || true
NETWORK_ARGS=(--network "$NETWORK")
PUBLISH_ARGS=(--publish "127.0.0.1:${PORT}:8000")
if [[ "${EVO_HUB_DOCKER_HOST_NETWORK:-0}" == "1" ]]; then
  # Compatibility fallback for restricted kernels without the iptables raw table.
  # Filesystem isolation, non-root execution, read-only image, and capabilities remain active.
  NETWORK_ARGS=(--network host)
  PUBLISH_ARGS=()
  CONTAINER_PORT="$PORT"
elif [[ "${EVO_HUB_ALLOW_EGRESS:-0}" == "1" ]]; then
  docker network create --driver bridge "$NETWORK" >/dev/null
  CONTAINER_PORT=8000
else
  docker network create --internal "$NETWORK" >/dev/null
  CONTAINER_PORT=8000
fi

# Only /workspace is mounted, and it is the selected server's public-ID directory.
# The Docker socket, application source, database, uploads, and other workspaces are not mounted.
docker run -d --name "$CONTAINER" \
  "${NETWORK_ARGS[@]}" \
  "${PUBLISH_ARGS[@]}" \
  --mount "type=bind,src=$PROJECT,dst=/workspace" \
  --mount "type=bind,src=$PROJECT/Dockerfile,dst=/workspace/Dockerfile,ro" \
  --user 10001:10001 \
  --workdir /workspace \
  --env "PORT=$CONTAINER_PORT" \
  --env "EVO_HUB_SERVER_ID=$NAME" \
  --env "EVO_HUB_SERVER_ROOT=/workspace" \
  --memory="${EVO_HUB_MEMORY:-512m}" \
  --memory-swap="${EVO_HUB_MEMORY:-512m}" \
  --cpus="${EVO_HUB_CPUS:-0.50}" \
  --pids-limit="${EVO_HUB_PIDS:-128}" \
  --ulimit nofile=1024:1024 \
  --cap-drop=ALL \
  --security-opt=no-new-privileges:true \
  --security-opt=apparmor=docker-default \
  --init \
  --ipc=private \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=128m \
  --restart=unless-stopped \
  "$IMAGE" "${CMD[@]}"

echo "Started isolated Docker container $CONTAINER for workspace $PROJECT"
