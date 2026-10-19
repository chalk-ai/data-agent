# A data agent with controlled access to your warehouse and your docs

Ask a business question, get a number you can trust. The agent reads the numbers with
**ChalkSQL**, reads what the numbers *mean* from **Notion** and **Linear**, and does both from a
**Chalk sandbox** that can reach only those three services, with credentials it never sees.

> What is our revenue per product for enterprise customers split by whether or not they're "AI native"?

Nothing in the warehouse says which accounts are AI-native, which SKUs make up a product, or which
accounts are internal tests. That lives in the company's docs and tickets, so the agent has to
find it there first:

| What the question needs | Where it lives |
| --- | --- |
| Invoice lines, customers, segments | ChalkSQL over two parquet tables |
| SKU → product map; support and onboarding are *Services* | Notion: *Product catalog*, *Revenue reporting conventions* |
| Revenue is net of credits; `.internal` accounts are tests | Notion: *Revenue reporting conventions* |
| What "AI-native" means, and the decision for each account | Notion: *Definition: AI-native customer*, *Enterprise account review* |
| The retired `STRM-LEGACY` SKU counts as Relay | Linear: BILL-118 |
| Three accounts the Notion review has not covered yet | Linear: GTM-214 |

[`traces/example-run.jsonl`](traces/example-run.jsonl) is a recorded run. It took 16 tool calls
and 25 seconds, and every figure matches the reference answer that `data/generate.py --truth`
computes.

## How access is controlled

`run.py` starts the agent in a sandbox whose `NetworkPolicy` allowlist is empty, then binds each
credential to one host with `add_credentials`:

```python
sandbox = (
    Sandbox(image=image, network_policy=NetworkPolicy(allowed_hosts=[]), lifetime="900s")
    .add_credentials("api.chalk.ai", Secret.from_local_env("CHALK_API_TOKEN"))  # model router + ChalkSQL
    .add_credentials(notion_host, Secret.from_local_env("NOTION_TOKEN"))
    .add_credentials(linear_host, Secret.from_local_env("LINEAR_TOKEN"))
)
```

- **Only three hosts are reachable.** Anything else, a package index or a paste site, fails to connect.
- **The agent never holds a real token.** `$NOTION_TOKEN` inside the sandbox is a random
  placeholder. The egress proxy replaces it with the real token, and only in headers on
  requests to the Notion host. A prompt-injected agent cannot exfiltrate a key it does not have,
  or spend the Notion token on the Linear API.
- **Every call is attributable.** The fake apps log each request, and the Chalk token is a
  short-lived one minted for this run.

Before the agent starts, `run.py` records a short probe that shows all three from inside the
sandbox:

```
[policy] $NOTION_TOKEN in the sandbox                 __chalk-secr... (placeholder)
[policy] GET https://api.github.com                   blocked (ConnectError)
[policy] GET https://pypi.org/simple/                 blocked (ConnectError)
[policy] GET <notion>/v1/pages/x (no token)           401
```

## Files

| File | Role |
| --- | --- |
| `agent.py` | The agent: a tool-calling loop over an OpenAI-compatible endpoint (Chalk's model router). Tools: `run_sql`, `notion_search`, `notion_get_page`, `notion_query_database`, `linear_search_issues`, `linear_get_issue`, `submit_answer`. Prints its trace as JSON lines. |
| `run.py` | Starts the sandbox with the policy above, runs the agent, saves `traces/<timestamp>.jsonl`. |
| `deploy_workspace.py` | Deploys the fake Notion and fake Linear APIs as two small ScalingGroups, each with its own token. |
| `workspace/` | The fake apps (`server.py`) and their content (`notion.json`, `linear.json`). |
| `data/generate.py` | Builds the warehouse tables for Fieldstone, a fictional SaaS company, and prints the reference answer with `--truth`. |

## Run it

You need [`uv`](https://docs.astral.sh/uv/), the Chalk CLI, and a Chalk environment with
sandboxes and the model router enabled. Credentials come from the SDK’s ambient credentials from `chalk login`.

```bash
# 1. Build the tables and put them where your environment's ChalkSQL can read them.
./data/generate.py --truth
gcloud storage cp data/out/*.parquet gs://<your-bucket>/data-agent/   # or aws s3 cp ...

# 2. Deploy the fake Notion and Linear.
./deploy_workspace.py

# 3. Ask away.
./run.py --data-prefix gs://<your-bucket>/data-agent
./run.py "Which AI-native enterprise accounts grew Forge spend fastest this year?" --data-prefix gs://<your-bucket>/data-agent
```

To point the agent at the real apps, swap the two base URLs and tokens. The tools only use a
handful of read endpoints, and the sandbox policy stays the same.

## License

Apache-2.0. The company, accounts and documents are fictional.

## Local authentication

The SDK reads credentials, API host, and the selected environment from your existing
`chalk login` in `~/.chalk.yml`, including project-scoped and default logins.
For unattended runs, `CHALK_CLIENT_ID` and `CHALK_CLIENT_SECRET` remain supported. Where available, `--env-id`
overrides the target environment; otherwise the SDK uses your login or environment settings.
