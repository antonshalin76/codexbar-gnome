#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

echo "==> Git diff hygiene"
git diff --check

echo "==> Python syntax"
python3 -m py_compile bin/codexbar-gnome-indicator

echo "==> Shell syntax"
sh -n install.sh
sh -n uninstall.sh
sh -n scripts/install-git-hooks.sh

if command -v shellcheck >/dev/null 2>&1; then
  echo "==> ShellCheck"
  shellcheck install.sh uninstall.sh scripts/install-git-hooks.sh .githooks/pre-commit
else
  echo "==> ShellCheck skipped: shellcheck is not installed"
fi

echo "==> SonarQube"
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is required to run the SonarQube scanner." >&2
  exit 1
fi

host_url=${SONAR_HOST_URL:-http://localhost:9000}
container_url=${SONARQUBE_URL:-http://host.docker.internal:9000}
token=${SONAR_TOKEN:-${SONARQUBE_TOKEN:-}}
geek_sonar=/home/anton/Source/geek/scripts/sonar-local.mjs
geek_sonar_env=/home/anton/Source/geek/.sonar/local.env

if [ -z "$token" ] && [ -f "$geek_sonar" ]; then
  node "$geek_sonar" token >/dev/null
  if [ -f "$geek_sonar_env" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$geek_sonar_env"
    set +a
    host_url=${SONAR_HOST_URL:-$host_url}
    container_url=${SONARQUBE_URL:-$container_url}
    token=${SONAR_TOKEN:-${SONARQUBE_TOKEN:-}}
  fi
fi

if [ -z "$token" ]; then
  echo "ERROR: SONAR_TOKEN or SONARQUBE_TOKEN is required to run SonarQube." >&2
  exit 1
fi

docker run --rm \
  --add-host=host.docker.internal:host-gateway \
  -e "SONAR_HOST_URL=$container_url" \
  -e "SONAR_TOKEN=$token" \
  -v "$repo_root:/usr/src" \
  sonarsource/sonar-scanner-cli:latest \
  -Dsonar.qualitygate.wait=true \
  -Dsonar.qualitygate.timeout=300

echo "Quality gate passed: $host_url"
