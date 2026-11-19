"""Entry point: `python -m swarm.owner --state DIR <command> ...`.

Reads `DIR/server.json` (URL) and `DIR/tokens.json` (owner token). Prints JSON.
Commands:
  submit (--spec FILE.json | --prompt TEXT [--model ID] [--tools a,b] [--workspace PATH])
         [--request-id ID] [--wait]       a full TaskSpec only through --spec
  show TASK_ID | history AGENT_ID | events [--task ID] [--follow]
  resolve AGENT_ID STEP_SEQ CALL_ID --result JSON [--error] [--request-id ID]
  cancel TASK_ID [--request-id ID] | procs TASK_ID | log PROCESS_ID [--offset N]
  stop PROCESS_ID [--request-id ID]       every owner write takes --request-id
"""

import argparse
import json
import sys
from pathlib import Path

from swarm import wire
from swarm.client import OwnerClient, Rejected, Unreachable


def build_parser() -> argparse.ArgumentParser:
    """Describe the owner commands and their arguments."""
    parser = argparse.ArgumentParser(description="Control a swarm")
    parser.add_argument("--state", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit")
    source = submit.add_mutually_exclusive_group(required=True)
    source.add_argument("--spec", type=Path)
    source.add_argument("--prompt")
    for name in ("model", "tools", "workspace", "request-id"):
        submit.add_argument(f"--{name}")
    submit.add_argument("--wait", action="store_true")
    for command, argument in (("show", "task_id"), ("history", "agent_id"), ("cancel", "task_id"),
                              ("procs", "task_id"), ("log", "process_id"), ("stop", "process_id")):
        sub = commands.add_parser(command)
        sub.add_argument(argument)
        if command in ("cancel", "stop"):
            sub.add_argument("--request-id")
        if command == "log":
            sub.add_argument("--offset", type=int, default=0)
    events = commands.add_parser("events")
    events.add_argument("--task")
    events.add_argument("--follow", action="store_true")
    resolve = commands.add_parser("resolve")
    resolve.add_argument("agent_id")
    resolve.add_argument("step_seq", type=int)
    resolve.add_argument("call_id")
    resolve.add_argument("--result", type=json.loads, required=True)
    resolve.add_argument("--error", action="store_true")
    resolve.add_argument("--request-id")
    return parser


def _task_spec(args: argparse.Namespace) -> wire.TaskSpec:
    fields = {name: value for name in ("model", "tools", "workspace") if (value := getattr(args, name)) is not None}
    if args.spec is not None:
        if fields:
            raise ValueError("Use --model, --tools and --workspace only with --prompt")
        return wire.TaskSpec.model_validate_json(args.spec.read_bytes())
    if args.tools is not None:
        fields["tools"] = args.tools.split(",") if args.tools else []
    return wire.TaskSpec(prompt=args.prompt, **fields)


def _request(client: OwnerClient, args: argparse.Namespace) -> wire.WireModel:
    match args.command:
        case "submit":
            task = client.create_task(_task_spec(args), args.request_id)
            return client.wait_for_task(task.task_id) if args.wait else task
        case "show": return client.get_task(args.task_id)
        case "history": return client.history(args.agent_id)
        case "events": return client.events(args.task)
        case "resolve": return client.resolve_call(args.agent_id, args.step_seq, args.call_id,
                                                    args.result, args.error, args.request_id)
        case "cancel": return client.cancel_task(args.task_id, args.request_id)
        case "procs": return client.processes(args.task_id)
        case "log": return client.read_log(args.process_id, args.offset)
        case "stop": return client.stop_process(args.process_id, args.request_id)
    raise ValueError(f"Unknown command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    """Run an owner command using the URL and token saved in the state directory."""
    args = build_parser().parse_args(argv)
    try:
        server = json.loads((args.state / "server.json").read_text())
        tokens = json.loads((args.state / "tokens.json").read_text())
        client = OwnerClient(server["url"], tokens["owner"])
        if args.command == "events" and args.follow:
            after = 0
            while True:
                page = client.events(args.task, after=after, wait_s=20)
                after = page.next_after
                for event in page.events:
                    print(event.model_dump_json(), flush=True)
        print(_request(client, args).model_dump_json())
    except KeyboardInterrupt:
        return 130
    except (OSError, KeyError, ValueError, Rejected, Unreachable) as error:
        print(f"swarm owner: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
