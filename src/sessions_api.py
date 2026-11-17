"""会话记录 API：网页按用户隔离，QQ 仅站长只读查看。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.auth_api import get_current_user
from src.sessions import SessionError, SessionStore


router = APIRouter(tags=["会话"])


class CreateSessionBody(BaseModel):
    title: str = ""


class PatchSessionBody(BaseModel):
    title: str | None = None
    pinned: bool | None = None


def _store() -> SessionStore:
    return SessionStore()


def _owner(user: dict) -> str:
    owner = user.get("id") if isinstance(user, dict) else None
    if not isinstance(owner, str) or not owner.strip():
        raise HTTPException(status_code=401, detail="未登录")
    return owner.strip()


def _can_read_qq(user: dict) -> bool:
    # QQ 入站本身只接受配置的唯一 OWNER；网页端也只开放给站长。
    # 不把 QQ 号硬映射成网站用户 ID，历史无 owner_id 的 QQ 会话也能保留可见。
    return user.get("role") == "owner"


@router.get("/sessions")
def list_sessions(
    limit: int = 50,
    include_deleted: bool = False,
    user: dict = Depends(get_current_user),
):
    store = _store()
    web = store.list_owned(
        _owner(user), limit=limit, include_deleted=include_deleted
    )
    qq = (
        store.list(limit=limit, source="qq", include_deleted=include_deleted)
        if _can_read_qq(user) else []
    )
    return {
        # sessions 保留给旧客户端；新客户端明确按通道分两组。
        "sessions": web,
        "web_sessions": web,
        "qq_sessions": qq,
    }


@router.post("/sessions")
def create_session(
    body: CreateSessionBody | None = None,
    user: dict = Depends(get_current_user),
):
    title = (body.title if body else "") or ""
    return _store().create("web", title, owner_id=_owner(user))


@router.get("/sessions/{session_id}")
def get_session(
    session_id: str,
    after: int = 0,
    limit: int = 200,
    user: dict = Depends(get_current_user),
):
    store = _store()
    session = store.get_owned(session_id, _owner(user))
    # 即使网站用户 ID 与历史 QQ owner_id 碰巧相同，普通成员也不能读 QQ。
    if session is not None and session["source"] != "web":
        session = None

    if session is None and _can_read_qq(user):
        candidate = store.get(session_id)
        if candidate is not None and candidate["source"] == "qq":
            session = candidate

    if session is None or session["deleted"]:
        raise HTTPException(status_code=404, detail="会话不存在")

    return {
        "session": session,
        "messages": store.messages(session_id, after=after, limit=limit),
        "read_only": session["source"] == "qq",
    }


@router.patch("/sessions/{session_id}")
def patch_session(
    session_id: str,
    body: PatchSessionBody,
    user: dict = Depends(get_current_user),
):
    store = _store()
    owner = _owner(user)
    changed = False

    # change_owned 只匹配网页所属用户；QQ 会话在网页端保持只读。
    if body.title is not None:
        changed = store.change_owned(session_id, owner, "rename", body.title) or changed

    if body.pinned is not None:
        changed = store.change_owned(session_id, owner, "pin", body.pinned) or changed

    if not changed:
        raise HTTPException(status_code=404, detail="会话不存在、只读或无变更")

    return store.get_owned(session_id, owner)


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str, user: dict = Depends(get_current_user)):
    owner = _owner(user)
    if not _store().change_owned(session_id, owner, "delete"):
        raise HTTPException(status_code=404, detail="会话不存在或为只读 QQ 记录")

    return {"ok": True, "soft_deleted": session_id}


@router.get("/sessions/by-prefix/{prefix}")
def sessions_by_prefix(prefix: str, user: dict = Depends(get_current_user)):
    try:
        owner = _owner(user)
        rows = _store().find_by_prefix(prefix, source="web")
        return {"sessions": [row for row in rows if row["owner_id"] == owner]}
    except SessionError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
