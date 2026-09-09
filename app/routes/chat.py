"POST /v1/chat/completions route (stream + non-stream)."

from __future__ import annotations

import json
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core.errors import InvalidRequestError, ProviderError
from protocol.common import DONE_SSE
from protocol.openai import (
    openai_error_response,
    parse_openai_chat_request,
    to_openai_chat_response,
    to_openai_chunk_sse,
)

router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, payload: Dict[str, Any]):
    scheduler = request.app.state.scheduler
    try:
        chat_request = parse_openai_chat_request(payload)
    except InvalidRequestError as exc:
        status, body = openai_error_response(exc)
        return JSONResponse(status_code=status, content=body)

    if chat_request.stream:
        async def event_stream():
            try:
                async for chunk in scheduler.stream_chat(chat_request):
                    yield to_openai_chunk_sse(chunk)
                yield DONE_SSE
            except ProviderError as exc:
                status, body = openai_error_response(exc)
                yield f"data: {json.dumps(body, ensure_ascii=False)}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    try:
        response = await scheduler.chat_completion(chat_request)
    except ProviderError as exc:
        status, body = openai_error_response(exc)
        return JSONResponse(status_code=status, content=body)
    return to_openai_chat_response(response)
