from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal

from agent_framework import Agent, FunctionInvocationConfiguration, FunctionMiddleware
from agent_framework.foundry import FoundryChatClient
from azure.core.credentials import TokenCredential
from pydantic import BaseModel

from second_screen_pundit.agents.models import (
    CommentaryLine,
    DirectorDecision,
    Explanation,
    Insight,
    Recap,
)


AgentRole = Literal["director", "analyst", "commentator", "explainer", "recap"]
AgentTool = Callable[..., str]

_MAX_TOKENS: dict[AgentRole, int] = {
    "director": 256,
    "analyst": 384,
    "commentator": 256,
    "explainer": 512,
    "recap": 768,
}
_TIME_BUDGETS: dict[AgentRole, float] = {
    "director": 15,
    "analyst": 15,
    "commentator": 15,
    "explainer": 20,
    "recap": 45,
}
_FAST_ROLES = {"director", "analyst", "commentator"}
_LOW_REASONING_ROLES = {"director", "commentator"}
_OUTPUT_MODELS: dict[AgentRole, type[BaseModel]] = {
    "director": DirectorDecision,
    "analyst": Insight,
    "commentator": CommentaryLine,
    "explainer": Explanation,
    "recap": Recap,
}


def create_foundry_chat_client(
    *,
    endpoint: str,
    deployment: str,
    credential: TokenCredential,
    time_budget_seconds: float,
    max_function_calls: int = 4,
) -> FoundryChatClient:
    invocation_config: FunctionInvocationConfiguration = {
        "max_function_calls": max_function_calls,
        "max_duration_seconds": time_budget_seconds,
    }
    return FoundryChatClient(
        project_endpoint=endpoint,
        model=deployment,
        credential=credential,
        function_invocation_configuration=invocation_config,
    )


def create_foundry_agent(
    role: AgentRole,
    *,
    endpoint: str,
    model_fast: str,
    model_deep: str,
    credential: TokenCredential,
    instructions: str,
    tools: Sequence[AgentTool] = (),
    middleware: Sequence[FunctionMiddleware] = (),
    output_model: type[BaseModel] | None = None,
    time_budget_seconds: float | None = None,
) -> Agent:
    deployment = model_fast if role in _FAST_ROLES else model_deep
    if not deployment:
        raise ValueError(f"Missing model deployment for {role}")
    budget = time_budget_seconds or _TIME_BUDGETS[role]
    client = create_foundry_chat_client(
        endpoint=endpoint,
        deployment=deployment,
        credential=credential,
        time_budget_seconds=budget,
    )
    default_options: dict[str, object] = {
        "max_tokens": _MAX_TOKENS[role],
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "response_format": output_model or _OUTPUT_MODELS[role],
    }
    if role in _LOW_REASONING_ROLES:
        default_options["reasoning"] = {"effort": "low"}
    return Agent(
        client=client,
        name=role.replace("_", " ").title(),
        instructions=instructions,
        tools=list(tools),
        middleware=list(middleware),
        default_options=default_options,
    )
