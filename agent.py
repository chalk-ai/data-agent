"""A data agent: ChalkSQL for the numbers, Notion and Linear for the context around them.

Runs inside a Chalk sandbox started by run.py. Every credential it sees is a placeholder that
the sandbox's egress proxy swaps for the real value, and only on requests to the host that
credential belongs to. Its network policy allows four hosts and nothing else.

Prints one JSON object per line: the run's trace, which run.py renders and records.
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import time
from typing import Any, Callable

import httpx
import pyarrow.parquet as pq

CHALK_API = os.environ["CHALK_API_HOST"].rstrip("/")
ENV_ID = os.environ["CHALK_ENVIRONMENT_ID"]
NOTION_URL = os.environ["NOTION_URL"].rstrip("/")
LINEAR_URL = os.environ["LINEAR_URL"].rstrip("/")
MODEL = os.environ["AGENT_MODEL"]
DATA_PREFIX = os.environ["DATA_PREFIX"].rstrip("/")
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "24"))
SQL_ROW_LIMIT = 60

http = httpx.Client(timeout=120)


def emit(event: dict[str, Any]) -> None:
    print(json.dumps(event), flush=True)


def chalk_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ['CHALK_API_TOKEN']}", "X-Chalk-Env-Id": ENV_ID}


# ---- Tools -----------------------------------------------------------------------------------


def run_sql(query: str) -> str:
    """ChalkSQL over the Connect protocol; the result comes back as parquet."""
    resp = http.post(
        f"{CHALK_API}/chalk.protosql.v1.SqlService/ExecuteSqlQuery",
        headers={**chalk_headers(), "x-chalk-deployment-type": "engine-grpc", "Content-Type": "application/json"},
        json={"query": query, "syncOptions": {}},
    )
    body = resp.json()
    if resp.status_code != 200 or body.get("errors") or "parquet" not in body:
        return "ERROR: " + json.dumps(body.get("errors") or body)[:1500]
    table = pq.read_table(io.BytesIO(base64.b64decode(body["parquet"])))
    rows = table.slice(0, SQL_ROW_LIMIT).to_pylist()
    header = " | ".join(table.column_names)
    lines = [header, "-" * len(header)]
    lines += [" | ".join("" if v is None else str(v) for v in r.values()) for r in rows]
    if table.num_rows > SQL_ROW_LIMIT:
        lines.append(f"... {table.num_rows - SQL_ROW_LIMIT} more rows")
    return f"{table.num_rows} rows\n" + "\n".join(lines)


def notion(method: str, path: str, payload: dict[str, Any] | None) -> str:
    headers = {"Authorization": f"Bearer {os.environ['NOTION_TOKEN']}"}
    resp = http.request(method, f"{NOTION_URL}{path}", headers=headers, json=payload)
    return resp.text if resp.status_code == 200 else f"ERROR {resp.status_code}: {resp.text}"


def linear(path: str, params: dict[str, str] | None) -> str:
    headers = {"Authorization": f"Bearer {os.environ['LINEAR_TOKEN']}"}
    resp = http.get(f"{LINEAR_URL}{path}", headers=headers, params=params)
    return resp.text if resp.status_code == 200 else f"ERROR {resp.status_code}: {resp.text}"


TOOLS: dict[str, tuple[str, dict[str, Any], Callable[..., str]]] = {
    "run_sql": (
        "Run a ChalkSQL query (Postgres-like SQL; JOINs, CTEs, CASE and GROUP BY all work) and get the rows back.",
        {"query": {"type": "string"}},
        lambda query: run_sql(query),
    ),
    "notion_search": (
        "Search the company Notion workspace. Returns matching pages and databases with their ids.",
        {"query": {"type": "string"}},
        lambda query: notion("POST", "/v1/search", {"query": query}),
    ),
    "notion_get_page": (
        "Read one Notion page as markdown.",
        {"page_id": {"type": "string"}},
        lambda page_id: notion("GET", f"/v1/pages/{page_id}", None),
    ),
    "notion_query_database": (
        "Read every row of a Notion database.",
        {"database_id": {"type": "string"}},
        lambda database_id: notion("POST", f"/v1/databases/{database_id}/query", {}),
    ),
    "linear_search_issues": (
        "Search Linear issues by keyword. Returns identifiers, titles and states.",
        {"query": {"type": "string"}},
        lambda query: linear("/v1/issues", {"query": query}),
    ),
    "linear_get_issue": (
        "Read one Linear issue, including its description and comments.",
        {"identifier": {"type": "string"}},
        lambda identifier: linear(f"/v1/issues/{identifier}", None),
    ),
}

SUBMIT_ANSWER = {
    "type": "function",
    "function": {
        "name": "submit_answer",
        "description": "Submit the final answer. Call this exactly once, at the end.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Two or three sentences answering the question."},
                "rows": {
                    "type": "array",
                    "description": "One row per product line.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "product": {"type": "string"},
                            "ai_native_usd": {"type": "number"},
                            "not_ai_native_usd": {"type": "number"},
                        },
                        "required": ["product", "ai_native_usd", "not_ai_native_usd"],
                    },
                },
                "assumptions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Each definition or convention applied, with the document it came from.",
                },
                "final_sql": {"type": "string", "description": "The query that produced the numbers."},
            },
            "required": ["summary", "rows", "assumptions", "final_sql"],
        },
    },
}


def tool_specs() -> list[dict[str, Any]]:
    specs = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "parameters": {"type": "object", "properties": props, "required": list(props)},
            },
        }
        for name, (desc, props, _) in TOOLS.items()
    ]
    return specs + [SUBMIT_ANSWER]


SYSTEM = f"""You are a data analyst at Fieldstone, a B2B software company. Answer the user's question with
numbers from the warehouse, interpreted the way the company defines them.

The warehouse has two tables, read with ChalkSQL straight from object storage:
  '{DATA_PREFIX}/customers.parquet'      customer_id, name, domain, segment, region, created_at
  '{DATA_PREFIX}/invoice_lines.parquet'  line_id, invoice_id, customer_id, invoice_date, sku, line_type, amount_usd
Reference them by quoted path, e.g. SELECT * FROM '{DATA_PREFIX}/customers.parquet' LIMIT 5.

Business definitions, conventions and decisions are not in the warehouse. They live in Notion and Linear.
Before you compute anything, look up how the company defines every term in the question, and follow
any pointer a document gives you to another source. Prefer one final query that applies every rule.
Keep tool calls purposeful. Finish by calling submit_answer, citing each assumption's source."""


def chat(messages: list[dict[str, Any]]) -> dict[str, Any]:
    resp = http.post(
        f"{CHALK_API}/v1/router/chat/completions",
        headers={**chalk_headers(), "Content-Type": "application/json"},
        json={"model": MODEL, "messages": messages, "tools": tool_specs(), "max_tokens": 4096},
        timeout=300,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]


def main(question: str) -> int:
    emit({"type": "question", "text": question, "model": MODEL})
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]
    t_start = time.monotonic()
    for _ in range(MAX_STEPS):
        msg = chat(messages)
        messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
        if msg.get("content"):
            emit({"type": "thought", "text": msg["content"]})
        calls = msg.get("tool_calls") or []
        if not calls:
            messages.append({"role": "user", "content": "Call submit_answer with your final answer."})
            continue
        for call in calls:
            name = call["function"]["name"]
            args = json.loads(call["function"]["arguments"] or "{}")
            if name == "submit_answer":
                emit({"type": "answer", **args, "elapsed_s": round(time.monotonic() - t_start, 1)})
                return 0
            emit({"type": "tool_call", "id": call["id"], "name": name, "args": args})
            t0 = time.monotonic()
            try:
                result = TOOLS[name][2](**args)
            except Exception as e:  # The model sees the failure and can recover from it.
                result = f"ERROR: {type(e).__name__}: {e}"
            emit(
                {
                    "type": "tool_result",
                    "id": call["id"],
                    "name": name,
                    "result": result,
                    "ms": round((time.monotonic() - t0) * 1000),
                }
            )
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    emit({"type": "error", "text": f"no answer after {MAX_STEPS} steps"})
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
