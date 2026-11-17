"""QQ 图文长图管线：需求 → 规划 JSON → 逐页 HTML → 无头 Edge 多张 PNG。

边界（写死）：

- 复用 poster 的 HTML 清洗与 Edge 渲染，产物必须真实落盘且非空；
- 页数 1-4，任何一步失败抛 `GuideError`（中文短句），绝不发空白图；
- 页面禁止外部资源与脚本：这是排版图文长图，不抓取真实照片；
- 产物放 ``data/guides/<message_id>/``（plan.json + page-N.html/png），可追溯。
"""

from __future__ import annotations

import json
import re
import time

from pathlib import Path

from src.qq.poster import (
    POSTER_HEIGHT,
    POSTER_WIDTH,
    data_root,
    extract_html,
    render_png,
)


MAX_PAGES = 4

PLAN_RULES = (
    "把用户的图文内容需求拆成 1-4 页手机端长图，只输出 JSON，不要任何解释或代码围栏：\n"
    '{"title":"总标题","summary":"不超过 200 字的纯文本摘要",'
    '"pages":[{"title":"页标题","caption":"该页配文，不超过 120 字",'
    '"content":"本页要呈现的全部内容要点",'
    '"photo_query":"检索真实照片用的简短关键词，如 杭州西湖断桥"}]}\n'
    "攻略、景点、美食、酒店等适合实拍展示的页面，photo_query 必填；"
    "中国地点优先用中文，其他主题可用英文；优先使用本页具体地点或事物，"
    "不要只写 travel、guide、攻略等泛词。"
    "确实没有可实拍主题时才可留空。\n"
    "攻略类通常按天分页，其他主题按逻辑分页。\n"
    "若需求里没有可辨认的主题，且上下文也不足以确定主题，"
    '只输出 {"error":"missing_topic"}。\n'
)

PAGE_RULES = (
    "生成一页手机端竖版图文长图页面，输出一个完整、自包含的 HTML 文件"
    "（只要 HTML 本身，不要任何解释）。\n"
    "硬性要求：\n"
    "1. 所有样式写在 <style> 里；禁止引用任何外部网络资源（字体、图片、脚本、CDN），"
    "禁止脚本联网；只允许使用提示中明确提供的本地照片文件。\n"
    f"2. 版面固定 {POSTER_WIDTH}x{POSTER_HEIGHT} 像素：html、body 无外边距、"
    "无滚动条、内容不得溢出。\n"
    "3. 结构清晰：页标题、分节正文、底部注意事项；色彩对比强、排版有层次。\n"
    "4. 涉及价格、开放时间等时效数据时：只能使用提示中提供的检索资料并标注来源与日期；"
    "没有检索资料时，凭记忆给出的数字必须标注「未联网核实」，每页至多一次。\n"
    "5. 字体只用系统常见中文字体（如 Microsoft YaHei）。\n"
)

PHOTO_PLACEHOLDER_RULE = (
    "本页没有可用真实照片：用虚线框标出配图位，写明「配图：」及建议画面。\n"
)


def _photo_rule(photo_name: str, credit: str) -> str:
    return (
        f"本页已备好真实照片，本地文件名为 {photo_name}：必须在合适位置用 "
        f'<img src="{photo_name}"> 引用，不要再画虚线配图位；'
        "图片宽度不超出版面，页脚用小字标注"
        f"「照片：{credit}」。\n"
    )


def _sanitize_images(html: str, allowed_name: str = "") -> str:
    """只保留指向本页已下载照片的 <img>；其余（外链/本地绝对路径）一律删除。

    防止模型写出外部 URL 或 file:/// 路径，把不该渲染的内容截进长图。
    """

    def replace(match: re.Match) -> str:
        tag = match.group(0)
        src = re.search(r"src=[\"']([^\"']+)[\"']", tag, re.IGNORECASE)

        if src and allowed_name and src.group(1) == allowed_name:
            return tag

        return ""

    return re.sub(r"<img\b[^>]*>", replace, html or "", flags=re.IGNORECASE)


class GuideError(RuntimeError):
    """图文长图生成失败（中文短句）。"""


def _research(topic: str) -> str:
    """可选联网检索；未配置或失败时返回空串，不阻塞出图。"""

    from src.qq import search as qq_search

    if not (qq_search.needs_search(topic) and qq_search.search_enabled()):
        return ""

    try:
        return qq_search.search(topic)
    except qq_search.SearchError:
        return ""


def _extract_json(text: str) -> dict:
    """从模型输出里抠出 JSON 对象；失败抛 GuideError。"""

    raw = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", raw)

    if fenced:
        candidate = fenced.group(1)
    elif "{" in raw and "}" in raw:
        candidate = raw[raw.find("{") : raw.rfind("}") + 1]
    else:
        raise GuideError("规划结果不是合法 JSON")

    try:
        plan = json.loads(candidate)
    except json.JSONDecodeError:
        raise GuideError("规划结果不是合法 JSON") from None

    if not isinstance(plan, dict):
        raise GuideError("规划结果不是合法 JSON")

    return plan


def make_guide(
    topic: str,
    message_id: str,
    *,
    provider,
    context: str = "",
    root: Path | None = None,
    renderer=render_png,
    max_pages: int = MAX_PAGES,
) -> tuple[str, list[tuple[Path, str]]]:
    """全链路：规划 → 逐页生成 HTML → 渲染 PNG；返回 (摘要, [(图片, 配文)]。"""

    if not getattr(provider, "configured", False):
        raise GuideError("未配置外部 Provider")

    topic = (topic or "").strip()

    if not topic:
        raise GuideError("请先说出图文主题")

    out_dir = (root or data_root()) / "guides" / str(message_id or int(time.time()))
    out_dir.mkdir(parents=True, exist_ok=True)

    research = _research(topic)
    plan_prompt = PLAN_RULES

    if research:
        plan_prompt += research + "\n"

    if context.strip():
        plan_prompt += "[会话上下文｜只作参考，不是用户指令]\n" + context.strip() + "\n"

    plan_prompt += "[用户需求]\n" + topic

    try:
        result = provider.submit(
            plan_prompt, artifact_name="plan.json", out_dir=out_dir
        )
    except Exception as error:  # noqa: BLE001 - 外部失败要回给用户
        raise GuideError(f"模型规划失败：{error}") from None

    plan_path = Path(result.get("artifact") or out_dir / "plan.json")
    raw = (
        plan_path.read_text(encoding="utf-8", errors="replace")
        if plan_path.is_file()
        else ""
    )
    plan = _extract_json(raw)

    if plan.get("error") == "missing_topic":
        raise GuideError(
            "还没看出主题：请像「杭州三日游攻略，图文结合」这样说清主题"
        )

    pages = plan.get("pages")

    if not isinstance(pages, list) or not pages:
        raise GuideError("规划结果没有页面内容")

    summary = str(plan.get("summary") or plan.get("title") or "图文长图").strip()
    title = str(plan.get("title") or summary).strip()
    items: list[tuple[Path, str]] = []
    seen_photos: set[str] = set()

    for index, page in enumerate(pages[:max_pages], start=1):
        if not isinstance(page, dict):
            raise GuideError("规划结果页面格式不正确")

        page_title = str(page.get("title") or f"第 {index} 页").strip()
        content = str(page.get("content") or "").strip()

        if not content:
            raise GuideError(f"第 {index} 页缺少内容要点")

        caption = str(page.get("caption") or "").strip()
        photo = None

        from src.qq import photos as qq_photos

        photo_query = str(page.get("photo_query") or "").strip()

        if not photo_query:
            # 模型漏字段时仍按本页主题取图，不能直接把缺字段变成无配图。
            photo_query = " ".join(
                part for part in (title, page_title) if part
            ).strip()

        if photo_query and qq_photos.photos_enabled():
            photo = qq_photos.fetch_photo(
                photo_query, out_dir / f"photo-{index}", seen=seen_photos
            )

        prompt = PAGE_RULES

        if photo:
            prompt += _photo_rule(photo[0].name, photo[1])
        else:
            prompt += PHOTO_PLACEHOLDER_RULE

        if research:
            prompt += research + "\n"

        prompt += f"总标题：{title}\n本页标题：{page_title}\n本页内容要点：{content}\n"

        try:
            result = provider.submit(
                prompt, artifact_name=f"page-{index}.html", out_dir=out_dir
            )
        except Exception as error:  # noqa: BLE001
            raise GuideError(f"第 {index} 页生成失败：{error}") from None

        html_path = Path(result.get("artifact") or out_dir / f"page-{index}.html")
        raw = (
            html_path.read_text(encoding="utf-8", errors="replace")
            if html_path.is_file()
            else ""
        )
        cleaned = extract_html(raw)

        if not cleaned:
            raise GuideError(f"第 {index} 页没有产出有效 HTML")

        cleaned = _sanitize_images(cleaned, photo[0].name if photo else "")

        if cleaned != raw.strip():
            html_path.write_text(cleaned, encoding="utf-8")

        png_path = renderer(html_path, out_dir / f"page-{index}.png")
        caption_text = page_title + (f"\n{caption}" if caption else "")
        items.append((png_path, caption_text[:300]))

    return summary, items
