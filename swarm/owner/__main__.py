"""Owner CLI: use a local state directory, or a server URL and an owner token."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import math
import os
import sys
from pathlib import Path

from swarm import wire
from swarm.client import OwnerClient, Rejected, Unreachable


def build_parser() -> argparse.ArgumentParser:
    """Describe the owner commands and their arguments."""
    parser = argparse.ArgumentParser(description="Control a swarm")
    endpoint = parser.add_mutually_exclusive_group(required=True)
    endpoint.add_argument("--state", type=Path)
    endpoint.add_argument("--url", help="server URL; requires SWARM_OWNER_TOKEN")
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit")
    source = submit.add_mutually_exclusive_group(required=True)
    source.add_argument("--spec", type=Path)
    source.add_argument("--prompt")
    for name in ("model", "tools", "workspace", "request-id"):
        submit.add_argument(f"--{name}")
    submit.add_argument("--wait", action="store_true")
    submit.add_argument("--timeout", type=float, default=60, help="wait deadline in seconds")
    for command, argument in (("show", "task_id"), ("history", "agent_id"), ("cancel", "task_id"),
                              ("procs", "task_id"), ("log", "process_id"), ("stop", "process_id")):
        sub = commands.add_parser(command)
        sub.add_argument(argument)
        if command in ("cancel", "stop"):
            sub.add_argument("--request-id")
        if command == "log":
            sub.add_argument("--offset", type=int, default=0)
            sub.add_argument("--json", action="store_true", help="print the chunk with its offsets")
        if command == "history":
            sub.add_argument("--after-seq", type=int, default=-1)
            sub.add_argument("--limit", type=int, default=200)
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
            if args.wait and (not math.isfinite(args.timeout) or args.timeout <= 0):
                raise ValueError("--timeout must be positive and finite")
            task = client.create_task(_task_spec(args), args.request_id)
            return client.wait_for_task(task.task_id, args.timeout) if args.wait else task
        case "show": return client.get_task(args.task_id)
        case "history": return client.history(args.agent_id, args.after_seq, args.limit)
        case "events": return client.events(args.task)
        case "resolve": return client.resolve_call(args.agent_id, args.step_seq, args.call_id,
                                                    args.result, args.error, args.request_id)
        case "cancel": return client.cancel_task(args.task_id, args.request_id)
        case "procs": return client.processes(args.task_id)
        case "log": return client.read_log(args.process_id, args.offset)
        case "stop": return client.stop_process(args.process_id, args.request_id)


def _connect(args: argparse.Namespace) -> OwnerClient:
    if args.url:
        token = os.environ.get("SWARM_OWNER_TOKEN")
        if not token:
            raise ValueError("Set SWARM_OWNER_TOKEN when using --url")
        return OwnerClient(args.url, token)
    url = json.loads((args.state / "server.json").read_text())["url"]
    return OwnerClient(url, json.loads((args.state / "tokens.json").read_text())["owner"])


def main(argv: list[str] | None = None) -> int:
    """Run an owner command using the URL and token saved in the state directory."""
    logging.basicConfig(format="%(asctime)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        client = _connect(args)
        if args.command == "events" and args.follow:
            after = 0
            while True:
                page = client.events(args.task, after=after, wait_s=20)
                after = page.next_after
                for event in page.events:
                    print(event.model_dump_json(), flush=True)
        reply = _request(client, args)
        if isinstance(reply, wire.LogChunk) and not args.json:
            sys.stdout.buffer.write(base64.b64decode(reply.data_b64))
        else:
            print(reply.model_dump_json())
    except KeyboardInterrupt:
        return 130
    except (OSError, KeyError, ValueError, Rejected, Unreachable) as error:
        logging.getLogger(__name__).error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
