"""Internal request model for the Anonymous Vertex protocol layer.

This module defines the *internal* representation of a request that the
Anonymous Vertex Provider builds and hands to the Protocol Layer.  The
Protocol Layer is responsible for translating this internal model into the
Google GraphQL envelope (see protocol.py); the Provider must never build
Google payload dictionaries itself.

The model intentionally mirrors the *real* Gemini variables structure used
by the upstream (vertex-singbox internal/engine/transform dto.go), i.e.
only fields that actually exist upstream are modelled.  Fields that are
Gateway-only concepts (e.g. OpenAI ``tools`` arrays) are converted *before*
this point, in request.py, and are never placed raw into Google variables.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def _content_from_dict(data: Dict[str, Any]) -> "AnonymousVertexContent":
    """Build an AnonymousVertexContent from a raw variables-style dict."""
    parts = []
    for p in data.get("parts", []) or []:
        if not isinstance(p, dict):
            continue
        parts.append(
            AnonymousVertexPart(
                text=p.get("text", ""),
                thought=bool(p.get("thought", False)),
                thought_signature=p.get("thoughtSignature"),
                inline_data=p.get("inlineData"),
                file_data=p.get("fileData"),
                function_call=p.get("functionCall"),
                function_response=p.get("functionResponse"),
                executable_code=p.get("executableCode"),
                code_execution_result=p.get("codeExecutionResult"),
                video_metadata=p.get("videoMetadata"),
                media_resolution=p.get("mediaResolution", ""),
            )
        )
    return AnonymousVertexContent(role=str(data.get("role", "user")), parts=parts)


def _generation_config_from_dict(data: Optional[Dict[str, Any]]) -> Optional["AnonymousVertexGenerationConfig"]:
    if not data:
        return None
    return AnonymousVertexGenerationConfig(
        temperature=data.get("temperature"),
        max_output_tokens=data.get("maxOutputTokens"),
        top_p=data.get("topP"),
        top_k=data.get("topK"),
        stop_sequences=data.get("stopSequences"),
        response_mime_type=data.get("responseMimeType"),
        response_schema=data.get("responseSchema"),
        thinking_config=data.get("thinkingConfig"),
    )


@dataclass
class AnonymousVertexPart:
    """A single content part (text / thought / function call / media...)."""

    text: str = ""
    thought: bool = False
    thought_signature: Optional[str] = None
    inline_data: Optional[Dict[str, Any]] = None
    file_data: Optional[Dict[str, Any]] = None
    function_call: Optional[Dict[str, Any]] = None
    function_response: Optional[Dict[str, Any]] = None
    executable_code: Optional[Dict[str, Any]] = None
    code_execution_result: Optional[Dict[str, Any]] = None
    video_metadata: Optional[Any] = None
    media_resolution: str = ""

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if self.text:
            out["text"] = self.text
        if self.thought:
            out["thought"] = True
        if self.thought_signature:
            out["thoughtSignature"] = self.thought_signature
        if self.inline_data is not None:
            out["inlineData"] = self.inline_data
        if self.file_data is not None:
            out["fileData"] = self.file_data
        if self.function_call is not None:
            out["functionCall"] = self.function_call
        if self.function_response is not None:
            out["functionResponse"] = self.function_response
        if self.executable_code is not None:
            out["executableCode"] = self.executable_code
        if self.code_execution_result is not None:
            out["codeExecutionResult"] = self.code_execution_result
        if self.video_metadata is not None:
            out["videoMetadata"] = self.video_metadata
        if self.media_resolution:
            out["mediaResolution"] = self.media_resolution
        return out


@dataclass
class AnonymousVertexContent:
    """A conversation turn (role user | model | function)."""

    role: str
    parts: List[AnonymousVertexPart] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "parts": [p.to_dict() for p in self.parts],
        }


@dataclass
class AnonymousVertexGenerationConfig:
    """Generation parameters (all optional)."""

    temperature: Optional[float] = None
    max_output_tokens: Optional[int] = None
    top_p: Optional[float] = None
    top_k: Optional[float] = None
    stop_sequences: Optional[List[str]] = None
    response_mime_type: Optional[str] = None
    response_schema: Optional[Any] = None
    thinking_config: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if self.temperature is not None:
            out["temperature"] = self.temperature
        if self.max_output_tokens is not None:
            out["maxOutputTokens"] = self.max_output_tokens
        if self.top_p is not None:
            out["topP"] = self.top_p
        if self.top_k is not None:
            out["topK"] = self.top_k
        if self.stop_sequences:
            out["stopSequences"] = self.stop_sequences
        if self.response_mime_type:
            out["responseMimeType"] = self.response_mime_type
        if self.response_schema is not None:
            out["responseSchema"] = self.response_schema
        if self.thinking_config:
            out["thinkingConfig"] = self.thinking_config
        return out


@dataclass
class AnonymousVertexRequest:
    """Internal request model passed from request.py to protocol.py.

    This is the single internal structure that represents what the upstream
    Gemini ``variables`` will contain.  Protocol-only fields (model,
    region, recaptchaToken) are carried separately and placed into the
    variables envelope by the protocol layer; they are not part of the
    Gemini *content* request.
    """

    model: str
    contents: List[AnonymousVertexContent] = field(default_factory=list)
    system_instruction: Optional[AnonymousVertexContent] = None
    safety_settings: List[Dict[str, str]] = field(default_factory=list)
    generation_config: Optional[AnonymousVertexGenerationConfig] = None
    tools: List[Dict[str, Any]] = field(default_factory=list)
    tool_config: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any], model: str = "") -> "AnonymousVertexRequest":
        """Build an AnonymousVertexRequest from a raw variables-style dict."""
        contents = [_content_from_dict(c) for c in data.get("contents", []) or [] if isinstance(c, dict)]
        si = data.get("systemInstruction")
        system_instruction = _content_from_dict(si) if isinstance(si, dict) else None
        return cls(
            model=data.get("model", model),
            contents=contents,
            system_instruction=system_instruction,
            safety_settings=[dict(s) for s in data.get("safetySettings", []) or [] if isinstance(s, dict)],
            generation_config=_generation_config_from_dict(data.get("generationConfig")),
            tools=list(data.get("tools", []) or []),
            tool_config=data.get("toolConfig"),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Variables-style dict (mirrors GraphQLVariables.to_dict without protocol fields)."""
        out: Dict[str, Any] = {"model": self.model}
        if self.contents:
            out["contents"] = [c.to_dict() for c in self.contents]
        if self.system_instruction is not None:
            out["systemInstruction"] = self.system_instruction.to_dict()
        if self.safety_settings:
            out["safetySettings"] = self.safety_settings
        if self.generation_config is not None:
            out["generationConfig"] = self.generation_config.to_dict()
        if self.tools:
            out["tools"] = self.tools
        if self.tool_config is not None:
            out["toolConfig"] = self.tool_config
        return out


@dataclass
class AnonymousVertexRequestContext:
    """The fixed / random GraphQL request envelope context."""

    client_version: str = "boq_cloud-boq-clientweb-vertexaistudio_20260630.00_p0"
    page_path: str = "/agent-platform/studio/multimodal"
    page_view_id: int = 1000000000000000
    tracking_id: str = "d0000000000000000"
    backend_overrides: Dict[str, Any] = field(default_factory=dict)
    client_session_id: str = ""
    selected_purview: Dict[str, Any] = field(default_factory=dict)
    jurisdiction: str = "global"
    localization_data: Dict[str, str] = field(
        default_factory=lambda: {"locale": "zh_CN", "timezone": "Asia/Hong_Kong"}
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "clientVersion": self.client_version,
            "pagePath": self.page_path,
            "pageViewId": self.page_view_id,
            "trackingId": self.tracking_id,
            "backendOverrides": self.backend_overrides,
            "clientSessionId": self.client_session_id,
            "selectedPurview": self.selected_purview,
            "jurisdiction": self.jurisdiction,
            "localizationData": self.localization_data,
        }


@dataclass
class GraphQLVariables:
    """The ``variables`` member of the GraphQL envelope."""

    request: AnonymousVertexRequest
    region: str = "global"
    recaptcha_token: str = ""

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "model": self.request.model,
            "region": self.region,
            **({"recaptchaToken": self.recaptcha_token} if self.recaptcha_token else {}),
        }
        if self.request.contents:
            out["contents"] = [c.to_dict() for c in self.request.contents]
        if self.request.system_instruction is not None:
            out["systemInstruction"] = self.request.system_instruction.to_dict()
        if self.request.safety_settings:
            out["safetySettings"] = [
                {k: v for k, v in s.items() if v is not None}
                for s in self.request.safety_settings
            ]
        if self.request.generation_config is not None:
            out["generationConfig"] = self.request.generation_config.to_dict()
        if self.request.tools:
            out["tools"] = self.request.tools
        if self.request.tool_config is not None:
            out["toolConfig"] = self.request.tool_config
        return out


@dataclass
class GraphQLPayload:
    """The full Google batchGraphql envelope (request body)."""

    request_context: AnonymousVertexRequestContext
    query_signature: str
    operation_name: str
    variables: GraphQLVariables

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requestContext": self.request_context.to_dict(),
            "querySignature": self.query_signature,
            "operationName": self.operation_name,
            "variables": self.variables.to_dict(),
        }
