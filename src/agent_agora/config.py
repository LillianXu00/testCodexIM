from dataclasses import dataclass, field
from pathlib import Path
import os


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("AGORA_DATA_DIR", ".runtime/agora")))
    central_url: str = field(default_factory=lambda: os.getenv("CENTRAL_URL", "http://127.0.0.1:8000"))
    agent_id: str = field(default_factory=lambda: os.getenv("AGENT_ID", "agent-alpha"))
    agent_name: str = field(default_factory=lambda: os.getenv("AGENT_NAME", os.getenv("AGENT_ID", "agent-alpha")))
    endpoint: str = field(default_factory=lambda: os.getenv("AGENT_ENDPOINT", "http://127.0.0.1:9101"))
    capabilities: list[str] = field(default_factory=lambda: [s for s in os.getenv("AGENT_CAPABILITIES", "").split(",") if s])
    join_token: str = field(default_factory=lambda: os.getenv("COMMUNITY_JOIN_TOKEN", ""))
    admin_token: str = field(default_factory=lambda: os.getenv("COMMUNITY_ADMIN_TOKEN", ""))
    read_token: str = field(default_factory=lambda: os.getenv("COMMUNITY_READ_TOKEN", ""))
    local_token: str = field(default_factory=lambda: os.getenv("SIDECAR_TOKEN", ""))
    allow_local: bool = field(default_factory=lambda: os.getenv("AGORA_ALLOW_LOCAL", "0") == "1")
    legacy_writes: bool = field(default_factory=lambda: os.getenv("AGORA_LEGACY_WRITES", "0") == "1")
    host_routes: str = field(default_factory=lambda: os.getenv("AGORA_HOST_ROUTES", ""))
    communication_url: str = field(default_factory=lambda: os.getenv("AGORA_COMMUNICATION_URL", "http://127.0.0.1:13901"))
    legacy_hosted: bool = field(default_factory=lambda: os.getenv("AGORA_LEGACY_HOSTED", "0") == "1")
    retry_seconds: float = 1
    max_attempts: int = 12
    memos_url: str = field(default_factory=lambda: os.getenv("MEMOS_BASE_URL", "https://memos.memtensor.cn/api/openmem/v1"))
    memos_key: str = field(default_factory=lambda: os.getenv("MEMOS_API_KEY", ""))
    memos_space: str = field(default_factory=lambda: os.getenv("MEMOS_SPACE_ID", "agora-shared"))
