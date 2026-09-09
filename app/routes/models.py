"GET /v1/models route."

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/v1/models")
async def list_models(request: Request):
    scheduler = request.app.state.scheduler
    models = await scheduler.list_models()
    data = [
        {
            "id": model.id,
            "object": "model",
            "created": 0,
            "owned_by": model.provider,
        }
        for model in models
    ]
    return {"object": "list", "data": data}
