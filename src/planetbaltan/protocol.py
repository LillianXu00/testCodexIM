from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives import serialization
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def verify(public_key: str, payload: dict, signature: str):
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True)).verify(
            base64.b64decode(signature, validate=True), canonical(payload)
        )
    except Exception as exc:
        raise ValueError("invalid signature") from exc


class Identity:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            key = Ed25519PrivateKey.generate()
            data = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        self.key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        self.public_key = base64.b64encode(self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()

    def sign(self, payload: dict) -> str:
        return base64.b64encode(self.key.sign(canonical(payload))).decode()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Event(StrictModel):
    event_id: UUID = Field(default_factory=uuid4)
    sender_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
    sender_seq: int = Field(ge=1)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    room_id: Literal["lobby"] = "lobby"
    mentions: list[str] = Field(max_length=500)
    recipients: list[str] = Field(max_length=500)
    kind: Literal["CHAT", "TASK_PUBLISH", "TASK_REPORT"]
    content: str = Field(default="", max_length=100_000)
    task_id: UUID | None = None
    parent_task_id: UUID | None = None
    reply_to_event_id: UUID | None = None
    title: str | None = Field(default=None, max_length=500)
    status: str | None = Field(default=None, max_length=100)
    artifacts: dict = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def utc_timestamp(cls, value):
        if value.tzinfo is None:
            raise ValueError("timestamp must include timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def task_fields(self):
        if self.kind != "CHAT" and self.task_id is None:
            raise ValueError("task_id required")
        if self.kind == "TASK_PUBLISH" and not self.title:
            raise ValueError("title required")
        if self.kind == "TASK_REPORT" and not self.status:
            raise ValueError("status required")
        if self.parent_task_id is not None and self.parent_task_id == self.task_id:
            raise ValueError("task cannot be its own parent")
        if len(self.recipients) != len(set(self.recipients)) or self.sender_id in self.recipients:
            raise ValueError("recipients must be unique and exclude sender")
        return self


class SignedEvent(StrictModel):
    event: Event
    signature: str

    def payload(self):
        return self.event.model_dump(mode="json")


class AgentRegistration(StrictModel):
    agent_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
    name: str = Field(min_length=1, max_length=200)
    bio: str = Field(default="", max_length=2000)
    capabilities: list[str] = Field(default_factory=list, max_length=100)
    endpoint: str
    public_key: str
    timestamp: datetime
    signature: str

    def payload(self):
        return self.model_dump(mode="json", exclude={"signature"})


class Resource(StrictModel):
    resource_id: UUID = Field(default_factory=uuid4)
    publisher_id: str
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)
    hub: str = Field(min_length=1, max_length=200)
    url: str
    version: str | None = Field(default=None, max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("url")
    @classmethod
    def external_url(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("resource URL must be HTTP(S) without credentials")
        return value


class SignedResource(StrictModel):
    resource: Resource
    signature: str


def validate_endpoint(url: str, allow_local: bool):
    p = urlsplit(url)
    if not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError("invalid endpoint")
    if allow_local:
        if p.scheme not in ("http", "https"):
            raise ValueError("HTTP(S) endpoint required")
    else:
        import ipaddress
        if p.scheme != "https":
            raise ValueError("internet endpoints require HTTPS")
        if p.hostname in ("localhost",) or p.hostname.endswith(".local"):
            raise ValueError("public endpoint required")
        try:
            addr = ipaddress.ip_address(p.hostname)
        except ValueError:
            pass
        else:
            if not addr.is_global:
                raise ValueError("public endpoint required")
    return url.rstrip("/")
