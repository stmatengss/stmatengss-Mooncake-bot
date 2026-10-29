import json
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Config:
    root: Path
    repository: str = "kvcache-ai/Mooncake"
    repository_id: int = 819733173
    registration_owner_type: str = "Organization"
    allowed_actor_ids: list[int] = field(default_factory=lambda: [11641725])
    installation_ids: list[int] = field(default_factory=list)
    mention: str = "mooncake-bot"
    app_id: str = ""
    app_key_file: str = "secrets/app-key.pem"
    webhook_secret_file: str = "secrets/webhook-secret"
    data_dir: str = "data"
    publish: bool = False
    max_concurrent: int = 2
    timeout_seconds: int = 600
    model: str = ""
    backend: str = "codex"
    codex_bin: str = "codex"
    kimi_bin: str = "kimi"
    kimi_home: str = "runtime/kimi-home"
    policy_file: str = "data/rsi/active-policy.json"
    tunnel_bin: str = ".tools/cloudflared"
    host: str = "127.0.0.1"
    port: int = 8821
    webhook_url: str = ""

    @classmethod
    def load(cls, path: str | Path):
        path = Path(path).resolve()
        config = cls(root=path.parent, **tomllib.loads(path.read_text()))
        if not 1 <= config.max_concurrent <= 8 or not 30 <= config.timeout_seconds <= 3600:
            raise ValueError("Concurrency or task timeout is out of bounds")
        if not config.allowed_actor_ids or config.repository.count("/") != 1:
            raise ValueError("An explicit repository and actor allowlist are required")
        if config.registration_owner_type not in {"User", "Organization"}:
            raise ValueError("Registration owner type must be User or Organization")
        if config.backend not in {"codex", "kimi"}:
            raise ValueError("Analysis backend must be codex or kimi")
        return config

    def path(self, name):
        return (self.root / getattr(self, name)).resolve()

    def save(self, destination):
        values = asdict(self)
        values.pop("root")
        temporary = Path(destination).with_suffix(".tmp")
        temporary.write_text("\n".join(f"{k} = {json.dumps(v)}" for k, v in values.items()) + "\n")
        temporary.chmod(0o600)
        temporary.replace(destination)

    @property
    def connected(self):
        return bool(self.app_id and self.installation_ids and self.path("app_key_file").is_file())
