"""Entry point: `python -m swarm.owner --state DIR <command> ...`.

Reads `DIR/server.json` (URL) and `DIR/tokens.json` (owner token). Prints JSON.
Commands:
  submit (--spec FILE.json | --prompt TEXT [--model ID] [--tools a,b] [--workspace PATH])
         [--request-id ID] [--wait]       a full TaskSpec only through --spec
  show TASK_ID | history AGENT_ID | events [--task ID] [--follow]
  resolve AGENT_ID STEP_SEQ CALL_ID --result JSON [--error] [--request-id ID]
  cancel TASK_ID [--request-id ID] | procs TASK_ID | log PROCESS_ID [--offset N] | stop PROCESS_ID
"""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
