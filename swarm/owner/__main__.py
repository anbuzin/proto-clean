"""Entry point: `python -m swarm.owner --state DIR <command> ...`.

Reads the server URL from `DIR/server.json` and the owner token from `DIR/tokens.json`.
Commands (each prints JSON):
  submit --prompt TEXT [--model ID] [--tools a,b] [--workspace PATH] [--budget N] [--kind loop|external] [--wait]
  show TASK_ID            the task tree
  history AGENT_ID
  events [--task TASK_ID] [--follow]
  resolve AGENT_ID STEP_SEQ CALL_ID --result JSON [--error]
  cancel TASK_ID
  procs TASK_ID
  log PROCESS_ID [--offset N] [--follow]
  stop PROCESS_ID
"""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
