"""Read-only readiness checks; never claims to prove inter-peer message delivery."""
from __future__ import annotations

import asyncio
import json

import httpx
from a2a.types import AgentCard
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .protocol import validate_endpoint

QUERY_TOOLS = {
    "kanban_list_agents", "kanban_list_tasks", "kanban_get_task_detail",
    "kanban_get_recent_messages", "community_list_resources",
}


async def check_mcp(url: str, token: str, timeout: float) -> int:
    # Use a separate client: central credentials must never accompany peer requests.
    async with asyncio.timeout(timeout):
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False,
                                     headers={"Authorization": "Bearer " + token}) as client:
            async with streamable_http_client(url + "/mcp", http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    names = {tool.name for tool in listing.tools}
                    if names != QUERY_TOOLS:
                        raise ValueError("unexpected central MCP tool set")
                    result = await session.call_tool("kanban_list_agents", {"limit": 1})
                    if result.isError:
                        raise ValueError("MCP query failed")
                    return len(names)


async def preflight(central_url: str, token: str, expected_agents: list[str], *,
                    allow_local: bool = False, timeout: float = 15,
                    http: httpx.AsyncClient | None = None, mcp_probe=check_mcp) -> dict:
    if not token:
        raise ValueError("COMMUNITY_READ_TOKEN required")
    central_url = validate_endpoint(central_url, allow_local)
    if not expected_agents or len(set(expected_agents)) != len(expected_agents):
        raise ValueError("provide at least one unique expected agent ID")
    own_client = http is None
    http = http or httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    checks = []

    def record(name, passed, detail):
        checks.append({"check": name, "passed": passed, "detail": detail})

    def failure(name, exc):
        # Error bodies/exception strings can include remote content or credentials.
        detail = type(exc).__name__
        if isinstance(exc, httpx.HTTPStatusError):
            detail += " HTTP " + str(exc.response.status_code)
        record(name, False, detail)

    try:
        peers = {}
        try:
            unauth = await http.get(central_url + "/api/v1/agents")
            record("central_read_auth", unauth.status_code in (401, 403),
                   {"unauthenticated_status": unauth.status_code})
            offset = 0
            while True:
                r = await http.get(central_url + "/api/v1/agents",
                    headers={"Authorization": "Bearer " + token}, params={"limit": 500, "offset": offset})
                r.raise_for_status()
                page = r.json()
                if not isinstance(page, list):
                    raise ValueError("invalid directory response")
                for peer in page:
                    peers[peer["agent_id"]] = peer
                if len(page) < 500:
                    break
                offset += 500
                if offset >= 10000:
                    raise ValueError("directory exceeds preflight limit")
            record("directory", True, {"registered_agents": len(peers)})
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            failure("directory", exc)

        for agent_id in expected_agents:
            name = "peer:" + agent_id
            peer = peers.get(agent_id)
            if not peer:
                record(name, False, "not registered")
                continue
            try:
                endpoint = validate_endpoint(peer["endpoint"], allow_local)
                if peer.get("identity_version") == 2:
                    endpoint = endpoint.removesuffix("/a2a")
                # Only visit expected peers. Never forward the central read token.
                card_url = peer.get("agent_card_url") or endpoint + "/.well-known/agent-card.json"
                if peer.get("endpoint_source") == "self_registered" and not peer.get("agent_card_url"):
                    raise ValueError("self-registered receiver has no Agent Card URL; readiness unverified")
                validate_endpoint(card_url, allow_local)
                response = await http.get(card_url)
                response.raise_for_status()
                card = AgentCard.model_validate(response.json())
                if card.url != (peer.get("a2a_url") or endpoint + "/a2a") or card.protocol_version != "0.3.0":
                    raise ValueError("Agent Card endpoint/protocol mismatch")
                if not any(ext.uri in ("urn:agent-agora:event:v1", "urn:agent-agora:submission:v2", "urn:agent-agora:peer:v3") and ext.required
                           for ext in card.capabilities.extensions or []):
                    raise ValueError("missing community extension")
                record(name, True, {"endpoint": endpoint, "protocol": card.protocol_version})
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                failure(name, exc)

        try:
            count = await mcp_probe(central_url, token, timeout)
            record("central_mcp", True, {"read_only_tools": count})
        except Exception as exc:
            failure("central_mcp", exc)
    finally:
        if own_client:
            await http.aclose()

    return {"passed": all(c["passed"] for c in checks), "mode": "local" if allow_local else "https",
            "scope": "readiness_from_this_machine", "central_url": central_url,
            "checks": checks, "peer_to_peer_delivery_tested": False,
            "independent_internet_hosts_verified": False}


def main():
    import argparse
    import os
    from pathlib import Path
    parser = argparse.ArgumentParser(description="Read-only PlanetBaltan network readiness check")
    parser.add_argument("--central-url", default=os.getenv("CENTRAL_URL"))
    parser.add_argument("--agent", action="append", required=True, help="Expected agent ID; repeat for each peer")
    parser.add_argument("--allow-local", action="store_true", help="Allow HTTP for development; report is not internet validation")
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.central_url or args.timeout <= 0:
        parser.error("central URL and positive timeout required")
    try:
        report = asyncio.run(preflight(args.central_url, os.getenv("COMMUNITY_READ_TOKEN", ""),
                                      args.agent, allow_local=args.allow_local, timeout=args.timeout))
    except ValueError as exc:
        parser.error(str(exc))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
