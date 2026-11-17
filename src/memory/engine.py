"""长期记忆的确定性自动捕获与对话上下文组装。

这里不做隐式画像，也不额外调用模型：只有用户明确说出的称呼、偏好、
行为约束和项目事实才会保存。普通问答不会写入记忆。
"""

from __future__ import annotations

import re

from dataclasses import dataclass

from src.memory import service


DEFAULT_CONTEXT_CHARS = 3000
MAX_STABLE_ITEMS = 8
MAX_PROJECT_ITEMS = 6
MAX_CANDIDATES_PER_MESSAGE = 5
MAX_CANDIDATE_CHARS = 500

_CONTEXT_HEADER = (
    "以下是当前用户的长期记忆，仅包含用户明确确认或平台验证的条目。\n"
    "使用规则：当前用户消息中的明确要求优先于旧记忆；只在相关时自然使用，"
    "不要复述无关记忆，也不要声称知道未列出的信息。"
)

_KIND_LABELS = {
    "preference": "偏好",
    "constraint": "约束",
    "project_fact": "项目事实",
}


@dataclass(frozen=True)
class MemoryCandidate:
    text: str
    key: str
    kind: str
    layer: str


def _topic_key(text: str, kind: str, layer: str) -> str:
    """只为语义明确的常见主题归 key；无法判断时保留独立条目。"""

    if re.search(r"叫我|称呼", text):
        return "user.name"

    if re.search(r"回答|回复|语言|中文|英文|日语|英语", text) and re.search(
        r"中文|英文|英语|日语|回答语言|回复语言", text
    ):
        return "response.language"

    if re.search(r"回答|回复|表达|风格|格式|简洁|详细|啰嗦", text):
        return "response.style"

    if kind == "preference" and re.search(
        r"绿茶|红茶|乌龙|普洱|咖啡|奶茶|饮品|饮料|喝茶", text, re.IGNORECASE
    ):
        return "preference.drink"

    if kind == "preference" and re.search(
        r"编程语言|Python|JavaScript|TypeScript|Java\b|C\+\+|Golang|Go语言",
        text,
        re.IGNORECASE,
    ):
        return "preference.programming_language"

    if layer == "project" and re.search(r"目录|路径|仓库位置|代码位置", text):
        return "project.path"

    if layer == "project" and re.search(
        r"技术栈|框架|数据库|使用|采用|基于|Python|PostgreSQL|SQLite|React",
        text,
        re.IGNORECASE,
    ):
        return "project.technology"

    return ""


def _candidate(text: str, *, kind: str, layer: str) -> MemoryCandidate:
    cleaned = " ".join(text.split()).strip(" ，,：:")
    return MemoryCandidate(
        text=cleaned,
        key=_topic_key(cleaned, kind, layer),
        kind=kind,
        layer=layer,
    )


_PREFERENCE_PATTERN = (
    r"我(?:(?:现在|目前|非常|很|最|更|真的?|不|不太)\s*)*"
    r"(?:喜欢|偏好)\s*\S+"
)


def _classify_remembered(text: str) -> MemoryCandidate:
    """“记住……”的内容仍按明确语义分类，不猜隐含主题。"""

    if re.search(r"我[^，,。；;]{0,24}(?:喜欢|偏好)", text):
        return _candidate(text, kind="preference", layer="identity")

    if re.match(r"^(?:以后|今后|从现在开始|请叫我|叫我)", text):
        kind = "preference" if re.match(r"^(?:请)?叫我", text) else "constraint"
        return _candidate(text, kind=kind, layer="identity")

    kind = "constraint" if re.search(r"必须|不要|不能|约束|只允许|禁止", text) else "project_fact"
    return _candidate(text, kind=kind, layer="project")


def _parse_clause(clause: str) -> MemoryCandidate | None:
    text = " ".join(clause.split()).strip(" ，,：:")
    if not text or len(text) > MAX_CANDIDATE_CHARS:
        return None

    # 疑问、反问以及“不要记住”都不是可持久化的事实陈述。
    if any(mark in text for mark in ("？", "?")):
        return None
    if re.match(r"^(?:请)?(?:不要|不用|别)记住", text):
        return None

    remembered = re.match(r"^(?:请)?记住(?:一下)?[\s，,：:]*(?P<text>.+)$", text)
    if remembered:
        return _classify_remembered(remembered.group("text"))

    if re.match(r"^(?:请)?叫我\s*\S+", text):
        return _candidate(text, kind="preference", layer="identity")

    if re.match(r"^(?:" + _PREFERENCE_PATTERN + r")", text):
        # “我喜欢什么”一类内容即使缺少问号也不能当作偏好。
        if re.search(r"(?:什么|哪[些个]|如何|怎么)(?:呢|吗)?$", text):
            return None
        return _candidate(text, kind="preference", layer="identity")

    if re.match(r"^(?:以后|今后|从现在开始)(?:请|要|应该|别|不要|不能|都)?\S+", text):
        return _candidate(text, kind="constraint", layer="identity")

    if re.match(r"^(?:请)?(?:用|使用)(?:中文|英文|英语|日语)(?:回答|回复)", text):
        return _candidate(text, kind="constraint", layer="identity")

    if re.match(r"^(?:回答|回复)(?:时)?(?:请|要|应该|别|不要|不能)\S+", text):
        return _candidate(text, kind="constraint", layer="identity")

    if re.match(r"^(?:这个|本|当前)?项目(?:的|中|里)?\S+", text):
        kind = "constraint" if re.search(r"必须|不要|不能|约束|只允许|禁止", text) else "project_fact"
        return _candidate(text, kind=kind, layer="project")

    return None


def parse_candidates(message: str) -> list[MemoryCandidate]:
    """提取高置信度候选；不依赖模型，也不保存普通陈述或问题。"""

    if not isinstance(message, str) or not message.strip():
        return []

    candidates = []
    for clause in re.split(r"[。；;\n]+", message):
        candidate = _parse_clause(clause)
        if candidate is not None and candidate.text:
            candidates.append(candidate)
        if len(candidates) >= MAX_CANDIDATES_PER_MESSAGE:
            break

    return candidates


def capture_user_message(
    owner_id: str,
    text: str,
    *,
    source_ref: str = "",
) -> list:
    """保存明确表达；同内容去重，同 key 的变化沿用现有版本链。"""

    candidates = parse_candidates(text)
    if not candidates:
        return []

    current = service.list_current(owner_id, limit=100000)["items"]
    known = {
        (item["kind"], item["layer"], item["key"], item["text"])
        for item in current
        if item["trust"] == "user_confirmed"
    }
    saved = []

    for candidate in candidates:
        fingerprint = (
            candidate.kind,
            candidate.layer,
            candidate.key,
            candidate.text,
        )
        if fingerprint in known:
            continue
        item = service.remember(
            owner_id,
            candidate.text,
            key=candidate.key,
            kind=candidate.kind,
            layer=candidate.layer,
            trust="user_confirmed",
            source_ref=source_ref or "chat:auto",
        )
        saved.append(item)
        known.add(fingerprint)

    return saved


def _current_items(owner_id: str) -> list[dict]:
    return [
        item
        for item in service.list_current(owner_id, limit=100000)["items"]
        if item["kind"] != "episode"
        and item["trust"] in ("platform_verified", "user_confirmed")
    ]


def build_context(
    owner_id: str,
    query: str,
    *,
    max_chars: int = DEFAULT_CONTEXT_CHARS,
) -> str | None:
    """稳定偏好始终少量注入，项目事实按当前问题检索，统一预算去重。"""

    current = _current_items(owner_id)
    stable = [
        item for item in current
        if item["layer"] == "identity"
        and item["kind"] in ("preference", "constraint")
    ][:MAX_STABLE_ITEMS]
    selected = list(stable)
    seen = {item["id"] for item in selected}

    if query and query.strip():
        try:
            related = service.recall(
                owner_id,
                query,
                k=MAX_PROJECT_ITEMS,
                layer="project",
                min_trust="platform_verified",
            )["hits"]
        except Exception:
            related = []

        for hit in related:
            item = hit.item
            if item.kind == "episode" or item.id in seen:
                continue
            selected.append({
                "id": item.id,
                "kind": item.kind,
                "layer": item.layer,
                "text": item.text,
            })
            seen.add(item.id)
            if len(selected) >= MAX_STABLE_ITEMS + MAX_PROJECT_ITEMS:
                break

    if not selected:
        return None

    lines = []
    used = len(_CONTEXT_HEADER)
    for item in selected:
        label = _KIND_LABELS.get(item["kind"], item["kind"])
        line = f"\n- [{label}] {item['text']}"
        if used + len(line) > max_chars:
            continue
        lines.append(line)
        used += len(line)

    return _CONTEXT_HEADER + "".join(lines) if lines else None
