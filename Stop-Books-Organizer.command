#!/bin/zsh
set -e
cd "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  echo "The application environment is not installed."
  exit 0
fi
exec .venv/bin/python server_control.py stop
