#!/bin/zsh
set -e
cd "${0:A:h}/.."
if [[ -x .venv/bin/python ]]; then
  .venv/bin/python server_control.py stop
fi
exec "${0:A:h}/Start-Books-Organizer.command"
