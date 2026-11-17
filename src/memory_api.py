"""长期记忆 API：分类列表、搜索、来源会话、编辑与历史链。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict

from src.auth_api import get_current_user
from src.memory import service
from src.memory.models import MemoryError
from src.sessions import SessionStore


router = APIRouter(prefix="/api/memory", tags=["Memory"])
Category = Literal["all", "stable", "project", "episode"]

HISTORY_ERRORS = {
    "history_cycle": 409,
    "history_forked": 409,
    "history_broken": 409,
    "history_owner_mismatch": 409,
    "history_too_long": 409,
}


class RememberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    key: str = ""
    kind: str = "project_fact"
    layer: str = "project"


class UpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


def _owner(user: dict) -> str:
    owner = user.get("id") if isinstance(user, dict) else None
    if not isinstance(owner, str) or not owner.strip():
        raise HTTPException(status_code=401, detail="未登录")
    return owner


def _matches_category(item, category: str) -> bool:
    value = item.item if hasattr(item, "item") else item
    kind = value["kind"] if isinstance(value, dict) else value.kind
    return kind in service.CATEGORY_KINDS[category]


def _enrich(row: dict, owner: str, sessions: SessionStore) -> dict:
    data = dict(row)
    kind = data["kind"]
    layer = data["layer"]
    trust = data["trust"]
    deleted = bool(data.get("deleted_at"))
    data["category"] = (
        "stable" if kind in ("preference", "constraint")
        else "project" if kind == "project_fact"
        else "episode"
    )

    trusted = trust in ("platform_verified", "user_confirmed") and not deleted
    data["context_usage"] = (
        "manual" if not trusted or kind == "episode"
        else "always" if layer == "identity" and kind in ("preference", "constraint")
        else "related"
    )
    data["status"] = "deleted" if deleted else "active"
    data["source_session_id"] = None

    source_ref = str(data.get("source_ref") or "")
    if source_ref.startswith("chat:"):
        message_id = source_ref.split(":", 1)[1]
        if message_id and message_id != "auto":
            data["source_session_id"] = sessions.web_session_for_message(message_id, owner)

    return data


def _item(item, owner: str, sessions: SessionStore) -> dict:
    return _enrich({
        "id": item.id, "key": item.key, "kind": item.kind, "layer": item.layer,
        "text": item.text, "trust": item.trust, "source_ref": item.source_ref,
        "valid_from": item.valid_from, "valid_to": item.valid_to,
        "supersedes_id": item.supersedes_id, "created_at": item.created_at,
        "updated_at": item.updated_at, "deleted_at": item.deleted_at,
    }, owner, sessions)


@router.post("/items", status_code=201)
def create_item(
    payload: RememberRequest,
    response: Response,
    user: dict = Depends(get_current_user),
):
    owner = _owner(user)
    response.headers["Cache-Control"] = "no-store"
    try:
        item = service.remember(
            owner, payload.text, key=payload.key,
            kind=payload.kind, layer=payload.layer,
        )
    except MemoryError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    return _item(item, owner, SessionStore())


@router.get("/items")
def list_items(
    response: Response,
    query: str = Query("", max_length=500),
    category: Category = "all",
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    owner = _owner(user)
    response.headers["Cache-Control"] = "no-store"
    text = (query or "").strip()
    sessions = SessionStore()

    if text:
        if offset > 0:
            raise HTTPException(status_code=422, detail="search_pagination_unsupported")
        try:
            result = service.recall(owner, text, k=50 if category != "all" else limit)
        except MemoryError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

        hits = [hit for hit in result["hits"] if _matches_category(hit, category)][:limit]
        return {
            "mode": "search",
            "items": [
                {**_item(hit.item, owner, sessions), "rank": index + 1, "score": hit.score}
                for index, hit in enumerate(hits)
            ],
            "count": len(hits),
            "limit": limit,
            "category": category,
            "search_mode": result["mode"],
        }

    try:
        result = service.list_current(
            owner, limit=limit, offset=offset, category=category
        )
    except MemoryError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    result["items"] = [_enrich(item, owner, sessions) for item in result["items"]]
    result["category"] = category
    return result


@router.get("/items/{item_id}")
def get_item(
    item_id: int,
    response: Response,
    user: dict = Depends(get_current_user),
):
    owner = _owner(user)
    response.headers["Cache-Control"] = "no-store"
    item = service.get_item(owner, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="memory_not_found")
    return _item(item, owner, SessionStore())


@router.patch("/items/{item_id}")
def update_item(
    item_id: int,
    payload: UpdateRequest,
    response: Response,
    user: dict = Depends(get_current_user),
):
    owner = _owner(user)
    response.headers["Cache-Control"] = "no-store"
    try:
        item = service.update_text(owner, item_id, payload.text)
    except MemoryError as error:
        status = 404 if str(error) == "memory_not_found" else 422
        raise HTTPException(status_code=status, detail=str(error)) from None
    if item is None:
        raise HTTPException(status_code=404, detail="memory_not_found")
    return _item(item, owner, SessionStore())


@router.delete("/items/{item_id}", status_code=204)
def delete_item(
    item_id: int,
    response: Response,
    user: dict = Depends(get_current_user),
):
    owner = _owner(user)
    response.headers["Cache-Control"] = "no-store"
    if not service.delete(owner, item_id):
        raise HTTPException(status_code=404, detail="memory_not_found")
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.get("/items/{item_id}/history")
def get_history(
    item_id: int,
    response: Response,
    user: dict = Depends(get_current_user),
):
    owner = _owner(user)
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.history(owner, item_id)
    except service.HistoryError as error:
        status = 404 if error.code == "memory_not_found" else HISTORY_ERRORS.get(error.code, 409)
        raise HTTPException(status_code=status, detail=error.code) from None
