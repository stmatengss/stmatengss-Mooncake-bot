import json
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath


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
    max_model_calls_per_day: int = 24
    auto_review: bool = False
    auto_summary: bool = False
    auto_title: bool = False
    auto_triage: bool = False
    auto_ci: bool = False
    automation_author_ids: list[int] = field(default_factory=list)
    scan_interval_seconds: int = 300
    enable_fix_prs: bool = False
    max_fix_prs: int = 2
    fix_paths: list[str] = field(
        default_factory=lambda: [
            "scripts/",
            "test/",
            "tests/",
            "mooncake-store/",
            "mooncake-transfer-engine/",
            "mooncake-common/",
            "mooncake-p2p-store/",
            "mooncake-integration/",
        ]
    )

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
        if any(
            type(getattr(config, name)) is not bool
            for name in [
                "auto_review",
                "auto_summary",
                "auto_title",
                "auto_triage",
                "auto_ci",
                "enable_fix_prs",
            ]
        ):
            raise ValueError("Feature switches must be booleans")
        if (
            type(config.max_model_calls_per_day) is not int
            or not 1 <= config.max_model_calls_per_day <= 96
            or type(config.scan_interval_seconds) is not int
            or not 60 <= config.scan_interval_seconds <= 3600
            or type(config.max_fix_prs) is not int
            or not 1 <= config.max_fix_prs <= 5
        ):
            raise ValueError("Automation budget, scan interval or draft limit is invalid")
        if any(
            type(value) is not int or value <= 0
            for value in config.allowed_actor_ids + config.automation_author_ids
        ):
            raise ValueError("Actor allowlists require positive numeric IDs")
        for prefix in config.fix_paths:
            if (
                not isinstance(prefix, str)
                or not prefix.endswith("/")
                or prefix.startswith("/")
                or ".." in PurePosixPath(prefix).parts
                or prefix.startswith(".github/")
            ):
                raise ValueError("Fix prefixes must be explicit repository directories")
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
