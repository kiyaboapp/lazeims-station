#!/usr/bin/env bash
# Convenience shim: forwards to launcher/start.sh so developers can invoke
# the same daily launcher used by the packaged Setup Kit.
set -e
cd "$(dirname "$0")"
exec ./launcher/start.sh "$@"
