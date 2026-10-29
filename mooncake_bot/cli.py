import argparse
import json
import os
from pathlib import Path

import uvicorn

from .config import Config
from .service import create_app
from .store import Store


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "serve",
            "tunnel",
            "status",
            "tasks",
            "setup",
            "connect",
            "rsi",
            "rsi-once",
            "rsi-status",
            "scan-once",
        ],
    )
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--rsi-config", default="rsi.json")
    args = parser.parse_args()
    config = Config.load(args.config)
    if args.command.startswith("rsi"):
        from .rsi import main as rsi_main

        rsi_main(config, Path(args.rsi_config).resolve(), args.command)
    elif args.command == "scan-once":
        from .automation import scan
        from .github import GitHub

        print(
            json.dumps({"task_ids": scan(config, GitHub(config), Store(config.path("data_dir")))})
        )
    elif args.command == "tunnel":
        from .tunnel import run_tunnel

        run_tunnel(Path(args.config).resolve())
    elif args.command == "setup":
        from .setup import create_setup

        uvicorn.run(
            create_setup(Path(args.config).resolve()), host="127.0.0.1", port=8822, access_log=False
        )
    elif args.command == "connect":
        from .github import GitHub

        github = GitHub(config)
        installed = github.request("GET", github.prefix + "/installation", github.assertion())
        config.installation_ids = [installed["id"]]
        config.save(Path(args.config).resolve())
        print("GitHub App installation recorded. Restart the service to load it.")
    elif args.command == "serve":
        uvicorn.run(create_app(config), host=config.host, port=config.port, access_log=False)
    elif args.command == "status":
        print(
            json.dumps(
                {
                    "repository": config.repository,
                    "github_connected": config.connected,
                    "publication_enabled": config.publish,
                    "analysis_backend": config.backend,
                    "config": str(Path(args.config).resolve()),
                }
            )
        )
    else:
        for row in Store(config.path("data_dir")).list():
            print(
                json.dumps({k: row[k] for k in ["id", "number", "state", "error", "published_url"]})
            )


if __name__ == "__main__":
    main()
