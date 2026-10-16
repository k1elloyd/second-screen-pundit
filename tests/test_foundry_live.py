import asyncio
import os
from collections.abc import Callable, Sequence

import pytest
from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv


def _foundry_settings() -> tuple[str, str]:
    load_dotenv()
    required = ("FOUNDRY_PROJECT_ENDPOINT", "MODEL_FAST")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        pytest.fail(
            "Set the following variables in the root .env or process environment "
            f"before running live tests: {', '.join(missing)}"
        )

    return os.environ["FOUNDRY_PROJECT_ENDPOINT"], os.environ["MODEL_FAST"]


def _build_agent(
    endpoint: str,
    deployment: str,
    *,
    tools: Sequence[Callable[[], str]] = (),
    include_encrypted_reasoning: bool = False,
) -> Agent:
    default_options: dict[str, object] = {"max_tokens": 96, "store": False}
    if include_encrypted_reasoning:
        default_options["include"] = ["reasoning.encrypted_content"]

    return Agent(
        client=FoundryChatClient(
            project_endpoint=endpoint,
            model=deployment,
            credential=DefaultAzureCredential(),
        ),
        instructions="Follow the user's request exactly and keep the reply brief.",
        tools=list(tools),
        default_options=default_options,
    )


@pytest.mark.live
def test_foundry_chat_call_prints_response() -> None:
    endpoint, deployment = _foundry_settings()
    agent = _build_agent(endpoint, deployment)
    response = asyncio.run(
        asyncio.wait_for(agent.run("Reply with the single word PONG."), timeout=30)
    )

    print(f"FoundryChatClient response: {response.text}")
    assert "PONG" in response.text.upper()


@pytest.mark.live
def test_foundry_tool_call_round_trip_with_store_false() -> None:
    endpoint, deployment = _foundry_settings()
    tool_calls: list[str] = []

    def phase_zero_fact() -> str:
        """Return the fixed value used by the Phase 0 tool-call probe."""
        tool_calls.append("called")
        return "COPPER"

    agent = _build_agent(
        endpoint,
        deployment,
        tools=[phase_zero_fact],
        include_encrypted_reasoning=True,
    )
    response = asyncio.run(
        asyncio.wait_for(
            agent.run(
                "Call phase_zero_fact and reply with its exact returned value."
            ),
            timeout=30,
        )
    )

    print(f"Foundry tool-call response: {response.text}")
    assert tool_calls == ["called"]
    assert "COPPER" in response.text.upper()
