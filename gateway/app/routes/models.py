from fastapi import APIRouter, Depends

from app.auth import require_api_key
from app.ollama_client import load_registry

router = APIRouter()


@router.get("/v1/models")
def list_models(_caller: str = Depends(require_api_key)) -> dict:
    registry = load_registry()
    return {
        "models": [
            {"name": name, "description": entry.get("description", "")}
            for name, entry in registry.items()
        ]
    }
