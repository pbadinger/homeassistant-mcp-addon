#!/usr/bin/with-contenv sh
set -eu

export PYTHONPATH=/app
exec python -m mcp_companion
