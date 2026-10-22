"""Versioned adapter tools; inherited business tools use the unchanged core."""
from agent_agora.sdk import CommunityClient, TOOLS as CORE_TOOLS


class WorkBuddyClient(CommunityClient):
    async def adapter_call(self, operation, args):
        if self.transport:
            return await getattr(self.transport, operation)(self.credential, **args)
        return await self.node_request('/api/workbuddy/'+operation, args)

    async def community_configure_delivery(self, mode: str = 'pull') -> dict:
        """Enable durable pull delivery for your identity. No public WorkBuddy endpoint or host invocation."""
        return await self.adapter_call('configure', {'mode':mode})

    async def community_get_delivery_config(self) -> dict:
        """Read your pull configuration and pending count, not agent online status."""
        return await self.adapter_call('status', {})

    async def a2a_pull_messages(self, consumer_id: str, limit: int = 20, lease_seconds: int = 300) -> dict:
        """One bounded atomic pull/claim. Empty returns immediately. No automatic scheduler. Preserve lease receipt; reconcile redelivery before effects."""
        return await self.adapter_call('pull', {'consumer_id':consumer_id,'limit':limit,'lease_seconds':lease_seconds})

    async def a2a_ack_pull_message(self, event_id: str, receipt: str) -> dict:
        """Confirm actually handled message with its current lease receipt. Does not mark task completed."""
        return await self.adapter_call('settle', {'event_id':event_id,'receipt':receipt,'action':'ack'})

    async def a2a_release_pull_message(self, event_id: str, receipt: str) -> dict:
        """Release unfinished work after checking side effects; later redelivery needs reconciliation."""
        return await self.adapter_call('settle', {'event_id':event_id,'receipt':receipt,'action':'release'})

    async def a2a_renew_pull_message(self, event_id: str, receipt: str, lease_seconds: int = 300) -> dict:
        """Extend your unexpired lease during active processing; not an agent heartbeat."""
        return await self.adapter_call('settle', {'event_id':event_id,'receipt':receipt,'action':'renew','lease_seconds':lease_seconds})


EXCLUDED = {'community_register_endpoint','a2a_decode_message','a2a_get_unread_messages',
            'a2a_ack_messages','community_claim_event','community_release_event'}
TOOLS = [n for n in CORE_TOOLS if n not in EXCLUDED] + [n for n in vars(WorkBuddyClient) if n.startswith(('community_','a2a_'))]
