#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
if [[ -n "${OFFLINE_TEST_PYTHON:-}" ]]; then
  test_python="$OFFLINE_TEST_PYTHON"
elif [[ -x "$repo_root/.venv/bin/python" ]]; then
  test_python="$repo_root/.venv/bin/python"
else
  echo "Offline pytest needs a repository interpreter. Create .venv or set OFFLINE_TEST_PYTHON=/path/to/python." >&2
  exit 2
fi
if [[ ! -x "$test_python" ]]; then
  echo "Offline pytest interpreter is not executable: $test_python" >&2
  echo "Set OFFLINE_TEST_PYTHON to an executable Python interpreter, or create .venv/bin/python." >&2
  exit 2
fi
uv_bin="$(command -v uv || true)"
if [[ -z "$uv_bin" ]]; then
  echo "uv is required for the offline repository test runner." >&2
  exit 2
fi

test_home="$(mktemp -d "$repo_root/.offline-test-home.XXXXXX")"
trap 'rm -rf "$test_home"' EXIT
mkdir -p "$test_home/tmp"

cd "$repo_root"
env -i \
  PATH="$(dirname "$uv_bin"):/usr/bin:/bin:/usr/sbin:/sbin" \
  HOME="$test_home" \
  TMPDIR="$test_home/tmp" \
  PYTHONPATH="$repo_root" \
  PYTHON_DOTENV_DISABLED=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  "$uv_bin" run --offline --no-sync --python "$test_python" -- python -c '
import socket
import sys

import dotenv

dotenv.load_dotenv = lambda *args, **kwargs: False
dotenv.dotenv_values = lambda *args, **kwargs: {}

def reject_network(*args, **kwargs):
    raise RuntimeError("offline test runner blocked a network connection")

socket.socket.connect = reject_network
socket.socket.connect_ex = reject_network
socket.create_connection = reject_network

import pytest

raise SystemExit(pytest.main([
    "-p", "pytest_asyncio.plugin", "--basetemp", __import__("os").path.join(
        __import__("os").environ["HOME"], "pytest"
    ), *sys.argv[1:]
]))
' "$@"
