from __future__ import annotations

import asyncio

from provider import ExampleFeatureFlagProvider

from codex_web.action_providers import ActionProviderBinding, ActionRequest


async def main() -> None:
    provider = ExampleFeatureFlagProvider()
    binding = ActionProviderBinding(
        organization_id="local",
        workspace_id="default",
        provider_type=provider.provider_type,
        provider_instance=provider.provider_instance,
    )
    request = ActionRequest(
        action_id="example.feature-flag.set",
        organization_id="local",
        workspace_id="default",
        parameters={"name": "new-dashboard", "enabled": True},
        idempotency_key="demo-flag-change-1",
        requested_by="demo-user",
    )

    print(await provider.prepare(request, binding=binding))
    result = await provider.execute(request, binding=binding)
    print(result)
    print(await provider.verify(result, binding=binding))
    print(await provider.rollback(result, binding=binding))


if __name__ == "__main__":
    asyncio.run(main())
