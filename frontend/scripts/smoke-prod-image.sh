#!/usr/bin/env bash
# Proves the production frontend image (frontend/Dockerfile.prod) does its three jobs:
# serves the built dashboard, falls back to index.html for client-side routes, and
# proxies /api/<path> to BACKEND_URL/<path> with the /api prefix stripped. A stand-in
# backend on the network alias `backend` answers /health with "ok".
#
# Usage: frontend/scripts/smoke-prod-image.sh IMAGE
set -euo pipefail

image=${1:?usage: smoke-prod-image.sh IMAGE}
net=leykhaa-fe-smoke-$$

cleanup() {
  docker rm -f "$net-backend" "$net-frontend" >/dev/null 2>&1 || true
  docker network rm "$net" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create "$net" >/dev/null
docker run -d --name "$net-backend" --network "$net" --network-alias backend python:3.12-alpine \
  sh -c 'mkdir -p /srv && printf ok > /srv/health && cd /srv && python -m http.server 8000' >/dev/null
docker run -d --name "$net-frontend" --network "$net" -p 127.0.0.1::8080 "$image" >/dev/null
port=$(docker port "$net-frontend" 8080/tcp | head -1 | sed 's/.*://')
base="http://127.0.0.1:${port}"

for _ in $(seq 1 30); do
  curl -fsS "$base/" >/dev/null 2>&1 && break
  sleep 1
done

curl -fsS "$base/" | grep -q '<div id="root">' \
  || { echo "FAIL: index.html not served at /" >&2; docker logs "$net-frontend" >&2; exit 1; }
curl -fsS "$base/tasks/42" | grep -q '<div id="root">' \
  || { echo "FAIL: no index.html fallback for a client-side route" >&2; exit 1; }
# The python stand-in backend binds its port slightly after nginx starts
# serving, so give /api/health the same wait-and-retry the frontend check above
# already gets, rather than racing it with a single attempt.
body=""
for _ in $(seq 1 30); do
  body=$(curl -fsS "$base/api/health" 2>/dev/null) && break
  sleep 1
done
[ -n "$body" ] \
  || { echo "FAIL: /api/health did not answer" >&2; docker logs "$net-frontend" >&2; exit 1; }
[ "$body" = ok ] || { echo "FAIL: /api/health returned '$body', want 'ok' from backend /health" >&2; exit 1; }
if docker run --rm --entrypoint sh "$image" -c 'grep -rl "localhost:8000" /usr/share/nginx/html' >/dev/null; then
  echo "FAIL: built bundle still points at localhost:8000; VITE_API_URL=/api was not applied" >&2; exit 1
fi
echo "frontend prod image OK"
