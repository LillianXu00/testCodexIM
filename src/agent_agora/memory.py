from uuid import uuid4
import base64
import hashlib
import re
import httpx

from .config import Settings


class MemosClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings, self.http = settings, http

    async def call(self, path: str, payload: dict):
        if not self.settings.memos_key:
            raise ValueError("MemOS not configured: set MEMOS_API_KEY and MEMOS_SPACE_ID; no local fallback")
        response = await self.http.post(self.settings.memos_url.rstrip('/') + path,
            headers={"Authorization": "Token " + self.settings.memos_key}, json=payload)
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0 or data.get("data", {}).get("success") is False:
            raise ValueError(f"MemOS API error code={data.get('code')}")
        return data

    async def save(self, raw_content: str, tags: list[str]):
        if not raw_content or len(raw_content.encode()) > 100_000:
            raise ValueError("raw record must contain 1..100000 UTF-8 bytes; split larger records explicitly")
        conversation_id = str(uuid4())
        encoded = base64.urlsafe_b64encode(raw_content.encode()).decode()
        digest = hashlib.sha256(raw_content.encode()).hexdigest()
        # Cloud normalizes whitespace in its historical-message response. Preserve
        # exact bytes in an opaque envelope alongside searchable original text.
        wire_content = raw_content + "\nAGORA_RAW_V1:" + encoded + ":" + digest + ":END_AGORA_RAW"
        result = await self.call("/add/message", {
            "user_id": self.settings.agent_id, "agent_id": self.settings.agent_id, "conversation_id": conversation_id,
            "messages": [{"role": "user", "content": wire_content,
                          "role_id": self.settings.agent_id, "role_name": self.settings.agent_name}],
            "tags": tags, "info": {"writer_agent_id": self.settings.agent_id, "community_space": self.settings.memos_space, "raw_conversation_id": conversation_id},
        })
        return {"reference": {"user_id": self.settings.agent_id, "subject_id": self.settings.agent_id,
                              "writer_agent_id": self.settings.agent_id, "conversation_id": conversation_id},
                "provider_result": result, "note": "Upload accepted; provider processing may be asynchronous. Share the reference, not the raw text, in community messages."}

    async def get_raw(self, conversation_id: str, subject_id: str | None = None):
        result = await self.call("/get/message", {"user_id": subject_id or self.settings.memos_space,
                         "conversation_id": conversation_id, "message_limit_number": 50})
        records = result.get("data", {}).get("message_detail_list", [])
        for record in records:
            content = record.get("content", "")
            matches = re.findall(r"AGORA_RAW_V1:([A-Za-z0-9_=-]+):([a-f0-9]{64}):END_AGORA_RAW", content)
            if not matches:
                record["exact_original_verified"] = False
                continue  # Older/external records: expose provider text honestly.
            # Last envelope is ours; original text may itself contain marker examples.
            encoded, digest = matches[-1]
            raw = base64.urlsafe_b64decode(encoded)
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError("MemOS raw record integrity check failed")
            record["content"] = raw.decode()
            record["exact_original_verified"] = True
            record["sha256"] = digest
        return result

    async def search(self, query: str, subject_id: str | None = None):
        result = await self.call("/search/memory", {"user_id": subject_id or self.settings.agent_id, "query": query})
        return {"kind": "provider_search_candidates_not_raw", "subject_id": subject_id or self.settings.agent_id, "provider_result": result,
                "next_step": "Read original records with memos_get_raw_memory(conversation_id, subject_id). If no source reference is returned, ask the author; do not treat a generated memory as raw content."}
