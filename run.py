#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["chalkcompute>=2.13.8", "httpx"]
# ///
"""Run the data agent in a Chalk sandbox with controlled access to ChalkSQL, Notion and Linear.

    ./run.py ["<question>"] --data-prefix gs://<bucket>/data-agent

Needs .workspace.env from deploy_workspace.py, and Chalk credentials from your existing `chalk login` (or the
CHALK_* environment variables). Writes the run's trace to traces/<timestamp>.jsonl.

What the sandbox can reach is the whole point:
  - its network policy allows exactly three hosts: the Chalk API (model router and ChalkSQL),
    the Notion API and the Linear API;
  - each credential is bound to one of those hosts. Inside the sandbox its environment variable
    holds a random placeholder, and the egress proxy swaps in the real value only on requests
    to that host. The agent cannot read a token, and cannot send one anywhere else.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit


HERE = Path(__file__).resolve().parent
DEFAULT_QUESTION = (
    'What is our revenue per product for enterprise customers split by whether or not they\'re "AI native"?'
)

# Runs in the sandbox before the agent: the token it sees is a placeholder, hosts outside the
# allowlist are unreachable, and an allowed host still rejects a request that carries no token.
POLICY_PROBE = """
import os, httpx

def get(url):
    try:
        return str(httpx.get(url, timeout=10).status_code)
    except Exception as e:
        return f"blocked ({type(e).__name__})"

print("$NOTION_TOKEN in the sandbox|" + os.environ["NOTION_TOKEN"][:12] + "... (placeholder)")
print("GET https://api.github.com|" + get("https://api.github.com"))
print("GET https://pypi.org/simple/|" + get("https://pypi.org/simple/"))
print("GET NOTION_URL/v1/pages/x (no token)|" + get("NOTION_URL/v1/pages/x"))
"""


def load_config() -> dict[str, str]:
    env_file = HERE / ".workspace.env"
    if not env_file.exists():
        sys.exit("Run ./deploy_workspace.py first.")
    pairs = (line.split("=", 1) for line in env_file.read_text().splitlines() if "=" in line)
    return {k.strip(): v.strip() for k, v in pairs}


def render(event: dict) -> str:
    kind = event["type"]
    if kind == "question":
        return f"\n? {event['text']}   ({event['model']})\n"
    if kind == "policy":
        return f"  [policy] {event['request']:<44} {event['outcome']}"
    if kind == "thought":
        return f"\n  {event['text'].strip()}\n"
    if kind == "tool_call":
        args = ", ".join(f"{k}={json.dumps(v)}" for k, v in event["args"].items())
        return f"  → {event['name']}({args[:160]})"
    if kind == "tool_result":
        first = event["result"].splitlines()[0][:100] if event["result"] else ""
        return f"    ← {event['ms']} ms  {first}"
    if kind == "answer":
        lines = ["", event["summary"], ""]
        lines += [
            f"  {r['product']:<10} AI-native ${r['ai_native_usd']:>14,.2f}   not ${r['not_ai_native_usd']:>14,.2f}"
            for r in event["rows"]
        ]
        lines += [""] + [f"  • {a}" for a in event["assumptions"]]
        return "\n".join(lines)
    return json.dumps(event)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("question", nargs="?", default=DEFAULT_QUESTION)
    parser.add_argument(
        "--data-prefix", required=True, help="where the parquet tables live, e.g. gs://bucket/data-agent"
    )
    args = parser.parse_args()

    ws = load_config()
    from chalkcompute import ConnectClient, Image, NetworkPolicy, Sandbox, SandboxClient, Secret

    with ConnectClient() as connection:
        api = connection.api_server
        env_id = connection.environment_id
        chalk_credential = Secret.from_value(
            "CHALK_API_TOKEN", connection.request_headers()["authorization"].removeprefix("Bearer ")
        )
    hosts = {
        name: urlsplit(url).hostname
        for name, url in (("chalk", api), ("notion", ws["NOTION_URL"]), ("linear", ws["LINEAR_URL"]))
    }

    image = (
        Image.debian_slim(python_version="3.12")
        .pip_install(["httpx==0.28.1", "pyarrow==21.0.0"])
        .add_local_file(str(HERE / "agent.py"), "/app/agent.py", strategy="copy")
    )
    sandbox = (
        Sandbox(
            image=image,
            name=f"data-agent-{int(time.time())}",
            cpu="1",
            memory="2Gi",
            lifetime="900s",
            # An empty allowlist: the three hosts below are the only egress.
            network_policy=NetworkPolicy(allowed_hosts=[]),
        )
        .add_credentials(hosts["chalk"], chalk_credential)
        .add_credentials(hosts["notion"], Secret.from_value("NOTION_TOKEN", ws["NOTION_TOKEN"]))
        .add_credentials(hosts["linear"], Secret.from_value("LINEAR_TOKEN", ws["LINEAR_TOKEN"]))
    )
    env = {
        "CHALK_API_HOST": api,
        "CHALK_ENVIRONMENT_ID": env_id,
        "NOTION_URL": ws["NOTION_URL"],
        "LINEAR_URL": ws["LINEAR_URL"],
        "AGENT_MODEL": 'anthropic/claude-sonnet-5-5',
        "DATA_PREFIX": args.data_prefix,
        "PYTHONUNBUFFERED": "1",
    }

    traces = HERE / "traces"
    traces.mkdir(exist_ok=True)
    trace_path = traces / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    client = SandboxClient.from_env()
    exit_code = 1
    try:
        print("Starting sandbox...")
        client.run(sandbox)
        with trace_path.open("w") as trace:

            def record(event: dict) -> None:
                trace.write(json.dumps(event) + "\n")
                trace.flush()
                print(render(event), flush=True)

            # Show the policy before the agent runs: tokens are placeholders, unlisted hosts fail,
            # and a listed host still checks the (swapped-in) token.
            probe = sandbox.exec(
                "python3", "-c", POLICY_PROBE.replace("NOTION_URL", ws["NOTION_URL"]), env=env, timeout_secs=60
            )
            for line in probe.stdout_text.splitlines():
                request, _, outcome = line.partition("|")
                record({"type": "policy", "request": request, "outcome": outcome or "-"})

            # Output arrives in arbitrary chunks; parse only complete lines.
            pending = ""
            for event in sandbox.exec_stream("python3", "/app/agent.py", args.question, env=env, timeout_secs=600):
                pending += event.stdout or ""
                *lines, pending = pending.split("\n")
                for line in lines:
                    if line.startswith("{"):
                        record(json.loads(line))
                if event.stderr:
                    print(event.stderr, end="", file=sys.stderr)
                if event.exit_code is not None:
                    exit_code = event.exit_code
        print(f"\ntrace: {trace_path.relative_to(HERE)}")
        return exit_code
    finally:
        if sandbox.id is not None:
            sandbox.terminate()


if __name__ == "__main__":
    sys.exit(main())
