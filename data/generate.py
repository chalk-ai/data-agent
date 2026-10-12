#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyarrow>=17"]
# ///
"""Generate the demo warehouse: billing data for Fieldstone, a fictional SaaS company.

Writes two parquet tables that the agent reads with ChalkSQL:

    customers.parquet      customer_id, name, domain, segment, region, created_at
    invoice_lines.parquet  line_id, invoice_id, customer_id, invoice_date, sku, line_type, amount_usd

The tables deliberately leave out what the question needs from elsewhere: which product a SKU
belongs to, which accounts are AI-native, and which accounts are internal tests. That context
lives in the fake workspace (workspace/notion.json and workspace/linear.json).

    ./data/generate.py [--out data/out] [--truth]

`--truth` prints the reference answer computed directly from these tables plus the workspace
context, so an agent run can be checked against it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
from collections import defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

SEED = 20261012
HERE = Path(__file__).resolve().parent

# (name, domain, ai_native). ai_native is ground truth for --truth only; the tables never
# carry it. The Notion account list covers the first block and leaves the second block
# unreviewed; Linear records the decisions for those three.
ENTERPRISE_REVIEWED = [
    ("Lattice Labs", "latticelabs.ai", True),
    ("Synthex", "synthex.ai", True),
    ("Cortical Systems", "corticalsystems.com", True),
    ("Mosaic Vision", "mosaicvision.io", True),
    ("Parrot Speech", "parrotspeech.ai", True),
    ("Quill Legal AI", "quill.law", True),
    ("Inference Works", "inferenceworks.dev", True),
    ("Helio Robotics", "heliorobotics.com", True),
    ("Ember Search", "embersearch.ai", True),
    ("Cobalt Security", "cobaltsec.com", True),
    ("Halcyon Bank", "halcyonbank.com", False),
    ("Meridian Insurance", "meridianins.com", False),
    ("Bluefin Retail", "bluefinretail.com", False),
    ("Corvid Telecom", "corvidtel.com", False),
    ("Atlas Manufacturing", "atlasmfg.com", False),
    ("Summit Health Network", "summithealth.org", False),
    ("Keystone Energy", "keystoneenergy.com", False),
    ("Pioneer Media", "pioneermedia.com", False),
    ("Verdant Foods", "verdantfoods.com", False),
    ("Orbit Airlines", "orbitair.com", False),
    ("Tessera Analytics", "tessera-analytics.com", False),
    ("Northstar Payments", "northstarpay.com", False),
    ("Juniper Travel", "junipertravel.com", False),
    ("Kinetic Games", "kineticgames.com", False),
    ("Granite Freight", "granitefreight.com", False),
]
ENTERPRISE_UNREVIEWED = [
    ("Helix Bio", "helixbio.com", True),
    ("Granite Logistics", "granitelogistics.com", False),
    ("Nimbus Health", "nimbushealth.com", True),
]
# Internal test accounts: billed in the warehouse, excluded from revenue by convention.
TEST_ACCOUNTS = [
    ("[TEST] Fieldstone Internal", "fieldstone.internal"),
    ("Demo Enterprise Sandbox", "demo.fieldstone.internal"),
]

# SKU -> product, as documented in the Notion catalog plus the Linear issue that folds the
# retired streaming SKU into Relay. Support and onboarding are reported as Services.
SKU_PRODUCT = {
    "PUL-STD": "Pulse",
    "PUL-ENT": "Pulse",
    "VLT-STORE": "Vault",
    "VLT-BACKUP": "Vault",
    "RLY-MSG": "Relay",
    "RLY-CONN": "Relay",
    "STRM-LEGACY": "Relay",
    "FRG-GPU": "Forge",
    "FRG-SEAT": "Forge",
    "SUP-PREM": "Services",
    "ONB-PRO": "Services",
}
# Monthly list price band per SKU for an enterprise account, in USD.
SKU_BASE = {
    "PUL-STD": 4_000,
    "PUL-ENT": 11_000,
    "VLT-STORE": 6_500,
    "VLT-BACKUP": 2_500,
    "RLY-MSG": 5_000,
    "RLY-CONN": 3_000,
    "STRM-LEGACY": 4_500,
    "FRG-GPU": 18_000,
    "FRG-SEAT": 3_500,
    "SUP-PREM": 2_000,
    "ONB-PRO": 9_000,
}
REGIONS = ["NA", "EMEA", "APAC"]
MONTHS = [dt.date(2026, m, 1) for m in range(1, 10)]
OTHER_WORDS = [
    "Acorn",
    "Beacon",
    "Cedar",
    "Delta",
    "Echo",
    "Fable",
    "Gale",
    "Harbor",
    "Iris",
    "Jade",
    "Kite",
    "Lumen",
    "Maple",
    "Nova",
    "Opal",
    "Prism",
    "Quartz",
    "Raven",
    "Sable",
    "Tidal",
]
OTHER_SUFFIX = ["Co", "Labs", "Group", "Studio", "Partners", "Works"]


def skus_for(rng: random.Random, ai_native: bool) -> list[str]:
    skus = ["PUL-ENT" if rng.random() < 0.7 else "PUL-STD", "VLT-STORE"]
    if rng.random() < 0.5:
        skus.append("VLT-BACKUP")
    skus.append("STRM-LEGACY" if rng.random() < 0.25 else "RLY-MSG")
    if rng.random() < 0.5:
        skus.append("RLY-CONN")
    if ai_native or rng.random() < 0.3:
        skus.append("FRG-GPU")
    if rng.random() < 0.6:
        skus.append("FRG-SEAT")
    if rng.random() < 0.4:
        skus.append("SUP-PREM")
    return skus


def build() -> tuple[pa.Table, pa.Table, dict[str, bool], set[str]]:
    rng = random.Random(SEED)
    customers: list[dict[str, object]] = []
    ai_native_by_id: dict[str, bool] = {}
    test_ids: set[str] = set()

    def add(name: str, domain: str, segment: str) -> str:
        cid = f"cus_{len(customers) + 1:04d}"
        customers.append(
            {
                "customer_id": cid,
                "name": name,
                "domain": domain,
                "segment": segment,
                "region": rng.choice(REGIONS),
                "created_at": dt.datetime(2023 + rng.randint(0, 2), rng.randint(1, 12), rng.randint(1, 28)),
            }
        )
        return cid

    for name, domain, ai in ENTERPRISE_REVIEWED + ENTERPRISE_UNREVIEWED:
        ai_native_by_id[add(name, domain, "enterprise")] = ai
    for name, domain in TEST_ACCOUNTS:
        test_ids.add(add(name, domain, "enterprise"))
    used: set[str] = set()
    while len(customers) < 90:
        name = f"{rng.choice(OTHER_WORDS)} {rng.choice(OTHER_SUFFIX)}"
        if name in used:
            continue
        used.add(name)
        slug = name.lower().replace(" ", "")
        add(name, f"{slug}.com", rng.choice(["mid-market", "mid-market", "smb"]))

    lines: list[dict[str, object]] = []
    for c in customers:
        cid = str(c["customer_id"])
        enterprise = c["segment"] == "enterprise"
        ai = ai_native_by_id.get(cid, rng.random() < 0.3)
        scale = (1.0 if enterprise else 0.18) * (1.6 if cid in test_ids else 1.0)
        skus = skus_for(rng, ai)
        start = rng.randint(0, 3)
        for month in MONTHS[start:]:
            invoice_id = f"inv_{cid[4:]}_{month:%Y%m}"
            for sku in skus:
                base = SKU_BASE[sku] * scale
                if sku == "FRG-GPU" and ai:
                    base *= 2.2  # AI-native accounts run far more GPU hours
                amount = round(base * rng.uniform(0.75, 1.3), 2)
                lines.append(
                    {
                        "invoice_id": invoice_id,
                        "customer_id": cid,
                        "invoice_date": month,
                        "sku": sku,
                        "line_type": "charge",
                        "amount_usd": amount,
                    }
                )
                if rng.random() < 0.04:
                    lines.append(
                        {
                            "invoice_id": invoice_id,
                            "customer_id": cid,
                            "invoice_date": month,
                            "sku": sku,
                            "line_type": "credit",
                            "amount_usd": -round(amount * rng.uniform(0.1, 0.5), 2),
                        }
                    )
            if month == MONTHS[start] and enterprise and rng.random() < 0.5:
                lines.append(
                    {
                        "invoice_id": invoice_id,
                        "customer_id": cid,
                        "invoice_date": month,
                        "sku": "ONB-PRO",
                        "line_type": "charge",
                        "amount_usd": round(SKU_BASE["ONB-PRO"] * rng.uniform(0.8, 1.2), 2),
                    }
                )
    for i, line in enumerate(lines):
        line["line_id"] = f"ln_{i + 1:06d}"

    customers_table = pa.Table.from_pylist(
        customers,
        schema=pa.schema(
            [
                ("customer_id", pa.string()),
                ("name", pa.string()),
                ("domain", pa.string()),
                ("segment", pa.string()),
                ("region", pa.string()),
                ("created_at", pa.timestamp("us")),
            ]
        ),
    )
    lines_table = pa.Table.from_pylist(
        lines,
        schema=pa.schema(
            [
                ("line_id", pa.string()),
                ("invoice_id", pa.string()),
                ("customer_id", pa.string()),
                ("invoice_date", pa.date32()),
                ("sku", pa.string()),
                ("line_type", pa.string()),
                ("amount_usd", pa.float64()),
            ]
        ),
    )
    return customers_table, lines_table, ai_native_by_id, test_ids


def truth(
    customers: pa.Table, lines: pa.Table, ai_native: dict[str, bool], test_ids: set[str]
) -> dict[str, dict[str, float]]:
    """Net revenue by product and AI-native flag for non-test enterprise accounts."""
    enterprise = {r["customer_id"] for r in customers.to_pylist() if r["segment"] == "enterprise"} - test_ids
    out: dict[str, dict[str, float]] = defaultdict(lambda: {"ai_native": 0.0, "not_ai_native": 0.0})
    for r in lines.to_pylist():
        cid = r["customer_id"]
        if cid not in enterprise:
            continue
        key = "ai_native" if ai_native[cid] else "not_ai_native"
        out[SKU_PRODUCT[r["sku"]]][key] += r["amount_usd"]
    return {p: {k: round(v, 2) for k, v in d.items()} for p, d in sorted(out.items())}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=HERE / "out")
    parser.add_argument("--truth", action="store_true")
    args = parser.parse_args()

    customers, lines, ai_native, test_ids = build()
    args.out.mkdir(parents=True, exist_ok=True)
    pq.write_table(customers, args.out / "customers.parquet")
    pq.write_table(lines, args.out / "invoice_lines.parquet")
    print(f"wrote {customers.num_rows} customers and {lines.num_rows} invoice lines to {args.out}")
    if args.truth:
        print(json.dumps(truth(customers, lines, ai_native, test_ids), indent=2))


if __name__ == "__main__":
    main()
