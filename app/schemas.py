from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    field_validator,
    model_validator,
)


class ChatToolFunction(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    arguments: str | dict[str, Any]


class ChatToolCall(BaseModel):
    # extra="allow" akzeptiert Streaming-Feld "index" sowie provider-spezifische
    # Zusatzfelder. Strict "forbid" brach Round-2-Calls wenn der Client frueheren
    # Assistant-tool_calls aus Streaming-Responses zurueck in die History nahm.
    model_config = ConfigDict(extra="allow")

    id: str | None = None
    type: Literal["function"]
    function: ChatToolFunction


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = None
    tool_calls: list[ChatToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    # Some LLMs (gpt-oss-120b) return reasoning_content in responses.
    # Clients may include these in conversation history — must not reject.
    reasoning_content: str | None = None

    @model_validator(mode="after")
    def validate_openai_tool_fields(self) -> "ChatMessage":
        if self.tool_calls and self.role != "assistant":
            raise ValueError("tool_calls are only valid for assistant messages")
        if self.tool_call_id and self.role != "tool":
            raise ValueError("tool_call_id is only valid for tool messages")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool messages require tool_call_id")
        # Normalize empty content to None for assistant messages with tool_calls
        # (OpenAI spec: content is null when assistant uses tools)
        if self.role == "assistant" and self.tool_calls and not self.content:
            self.content = None
        if self.content is None and not (self.role == "assistant" and self.tool_calls):
            raise ValueError("content may only be null for assistant messages with tool_calls")
        return self


class ChatCompletionsRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage]
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False
    metadata: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    response_format: dict[str, Any] | None = None

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model must not be empty")
        return value.strip()

    @field_validator("messages")
    @classmethod
    def validate_messages(cls, value: list[ChatMessage]) -> list[ChatMessage]:
        if not value:
            raise ValueError("messages must not be empty")
        return value


class EmbeddingsRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    input: str | list[str] | list[int] | list[list[int]]
    encoding_format: str | None = None
    dimensions: int | None = None
    user: str | None = None

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model must not be empty")
        return value.strip()


class RerankRequest(BaseModel):
    """Cohere-/Jina-kompatibler Rerank-Request.

    De-facto-Standard fuer Cross-Encoder-Reranking. ``documents`` ist die
    Kandidatenliste, ``query`` die Suchanfrage. ``top_n`` schneidet das
    Ergebnis ab, ``return_documents`` haengt den Original-Text an jedes
    Ergebnis-Objekt an.
    """

    model_config = ConfigDict(extra="allow")

    model: str
    query: str
    documents: list[str]
    top_n: int | None = None
    return_documents: bool = False

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model must not be empty")
        return value.strip()

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be empty")
        return value

    @field_validator("documents")
    @classmethod
    def validate_documents(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("documents must not be empty")
        return value

    @field_validator("top_n")
    @classmethod
    def validate_top_n(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("top_n must be >= 1")
        return value


class RerankResultDocument(BaseModel):
    model_config = ConfigDict(extra="allow")

    text: str


class RerankResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    index: int
    relevance_score: float
    document: RerankResultDocument | None = None


class RerankResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    results: list[RerankResult]
    usage: dict[str, Any] | None = None
