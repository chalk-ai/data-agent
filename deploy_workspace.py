#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["chalkcompute>=2.13.4"]
# ///
"""Deploy the fake Notion and fake Linear APIs as two small Chalk ScalingGroups.

Each app gets its own random bearer token. The tokens and URLs are written to .workspace.env
(gitignored), where run.py picks them up. Re-running reuses the existing tokens.

    ./deploy_workspace.py
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".workspace.env"
APPS = ("notion", "linear")


def read_env_file() -> dict[str, str]:
    if not ENV_FILE.exists():
        return {}
    pairs = (line.split("=", 1) for line in ENV_FILE.read_text().splitlines() if "=" in line)
    return {k.strip(): v.strip() for k, v in pairs}


def main() -> None:
    from chalkcompute import Image, ScalingGroup, Secret

    saved = read_env_file()
    image = (
        Image.debian_slim(python_version="3.12")
        .pip_install(["fastapi==0.116.1", "uvicorn[standard]==0.35.0"])
        .add_local_file(str(HERE / "workspace" / "server.py"), "/app/server.py", strategy="copy")
        .add_local_file(str(HERE / "workspace" / "notion.json"), "/app/notion.json", strategy="copy")
        .add_local_file(str(HERE / "workspace" / "linear.json"), "/app/linear.json", strategy="copy")
        .workdir("/app")
    )
    for app in APPS:
        token_key = f"{app.upper()}_TOKEN"
        token = saved.get(token_key) or f"{app}_{secrets.token_urlsafe(24)}"
        saved[token_key] = token
        os.environ[f"FAKE_{token_key}"] = token
        sg = ScalingGroup(
            image=image,
            name=f'fake-{app}',
            cpu="500m",
            memory="1Gi",
            env={"WORKSPACE_KIND": app, "WORKSPACE_TOKEN": Secret.from_local_env(f"FAKE_{token_key}")},
            entrypoint=["python", "/app/server.py"],
            port=8080,
            min_replicas=1,
            max_replicas=1,
        ).deploy(ready_timeout=900)
        saved[f"{app.upper()}_URL"] = str(sg.web_url).rstrip("/")
        print(f"{app}: {sg.web_url}")

    ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in sorted(saved.items())))
    print(f"wrote {ENV_FILE.name}")


if __name__ == "__main__":
    main()
