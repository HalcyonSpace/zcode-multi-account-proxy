#!/usr/bin/env python3
"""
Generate multi-account docker-compose.multi.yml and data/accounts.json.

Creates a unified Docker Compose file running N proxy instances sharing a single
built image, with isolated stores, collision-free ports, and individual API keys.
"""

import argparse
import json
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT_COMPOSE = ROOT / "docker-compose.multi.yml"
REG_FILE = DATA_DIR / "accounts.json"

IMAGE_NAME = "zcode-proxy:latest"
BASE_PORT = 8080  # slot N listens on 8080 + (N - 1)


def generate(num_slots=5):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Load existing registry to preserve generated secrets/keys
    existing = {}
    dash_token = secrets.token_urlsafe(24)
    if REG_FILE.exists():
        try:
            d = json.loads(REG_FILE.read_text(encoding="utf-8"))
            dash_token = d.get("dashboardToken", dash_token)
            for inst in d.get("instances", []):
                existing[inst["id"]] = inst
        except Exception:
            pass

    instances = []
    services = {}

    for idx in range(1, num_slots + 1):
        aid = f"{idx:02d}"
        port = BASE_PORT + idx - 1

        prev = existing.get(aid, {})
        api_key = prev.get("apiKey") or secrets.token_urlsafe(24)
        cred_secret = prev.get("credentialSecret") or secrets.token_urlsafe(32)

        inst = {
            "id": aid,
            "port": port,
            "store": f"stores/account-{aid}",
            "apiKey": api_key,
            "credentialSecret": cred_secret,
            "proxy": None,
            "signedIn": prev.get("signedIn", False)
        }
        instances.append(inst)

        services[f"zcode-proxy-{aid}"] = {
            "image": IMAGE_NAME,
            "build": {
                "context": ".",
                "dockerfile": "Dockerfile"
            },
            "container_name": f"zcode-proxy-{aid}",
            "restart": "unless-stopped",
            "ports": [
                f"{port}:8080"
            ],
            "volumes": [
                f"./stores/account-{aid}:/store",
                "./config:/data"
            ],
            "environment": {
                "ZCODE_PROXY_PORT": "8080",
                "ZCODE_PROXY_API_KEY": api_key,
                "ZCODE_PROXY_STORE_DIR": "/store",
                "ZCODE_PROXY_CREDENTIAL_SECRET": cred_secret,
                "BUN_JSC_useJIT": "0",
                "CAPTCHA_SOLVER_MODE": "child",
                "CAPTCHA_CHILD_BATCH": "2",
                "CAPTCHA_CHILD_CONCURRENCY": "1"
            },
            "healthcheck": {
                "test": ["CMD", "curl", "-f", f"http://localhost:8080/v1/models"],
                "interval": "30s",
                "timeout": "10s",
                "retries": 3,
                "start_period": "15s"
            }
        }

    # Add dashboard service
    services["zcode-dashboard"] = {
        "build": {
            "context": "./dashboard",
            "dockerfile": "Dockerfile"
        },
        "container_name": "zcode-dashboard",
        "restart": "unless-stopped",
        "ports": [
            "39777:39777"
        ],
        "volumes": [
            "./data:/app/data",
            "./stores:/app/stores",
            "./logs:/app/logs",
            "/var/run/docker.sock:/var/run/docker.sock:ro"
        ],
        "environment": {
            "DASHBOARD_PORT": "39777",
            "DASHBOARD_BIND": "0.0.0.0",
            "DATA_DIR": "/app/data",
            "STORES_DIR": "/app/stores",
            "LOGS_DIR": "/app/logs",
            "ZCODE_BACKEND": "docker"
        }
    }

    compose = {
        "version": "3.8",
        "name": "zcode-fleet",
        "services": services
    }

    OUT_COMPOSE.write_text(json.dumps(compose, indent=2), encoding="utf-8")
    print(f"[OK] Generated {OUT_COMPOSE} with {num_slots} account slots.")

    reg_data = {
        "app": "zcode-proxy-fleet",
        "count": num_slots,
        "dashboardToken": dash_token,
        "instances": instances
    }
    REG_FILE.write_text(json.dumps(reg_data, indent=2), encoding="utf-8")
    print(f"[OK] Generated {REG_FILE} with {num_slots} account slots.")


def main():
    parser = argparse.ArgumentParser(description="Generate multi-account Docker Compose")
    parser.add_argument("--slots", type=int, default=5, help="Number of account instances (default: 5)")
    args = parser.parse_args()
    generate(args.slots)


if __name__ == "__main__":
    main()
