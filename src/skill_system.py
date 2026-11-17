"""本地可信技能发现、启停与按任务注入。

技能只是带边界的 Markdown 指令，不是可执行插件。目录和 SKILL.md 均拒绝
符号链接；每个技能必须声明与目录一致的名称、描述和 enabled 状态。模型只能
通过既有工具注册表执行技能步骤，因此审批、身份与沙箱边界保持有效。
"""

from __future__ import annotations

import os
import re

from dataclasses import dataclass
from pathlib import Path

import yaml


BASE_DIR = Path(__file__).resolve().parent.parent
SKILLS_DIR_ENV = "PERSONAL_AGENT_SKILLS_DIR"
DISABLED_SKILLS_ENV = "PERSONAL_AGENT_DISABLED_SKILLS"
MAX_SKILL_BYTES = 64 * 1024
MAX_DESCRIPTION_CHARS = 300
MAX_SKILLS = 100
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class SkillConfigError(ValueError):
    """本地技能目录或 SKILL.md 配置无效。"""


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    instructions: str
    triggers: tuple[str, ...]
    path: Path


def default_root() -> Path:
    override = os.getenv(SKILLS_DIR_ENV, "").strip()
    return Path(override).expanduser() if override else BASE_DIR / "skills"


def _disabled_names() -> set[str]:
    return {
        item.strip().casefold()
        for item in os.getenv(DISABLED_SKILLS_ENV, "").split(",")
        if item.strip()
    }


def _frontmatter(text: str, path: Path) -> tuple[dict, str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        raise SkillConfigError(f"{path} 缺少 YAML frontmatter")

    parts = normalized.split("\n---\n", 1)
    if len(parts) != 2:
        raise SkillConfigError(f"{path} 的 frontmatter 没有结束标记")

    try:
        metadata = yaml.safe_load(parts[0][4:])
    except yaml.YAMLError as error:
        raise SkillConfigError(f"{path} 的 YAML 无效：{error}") from None

    if not isinstance(metadata, dict):
        raise SkillConfigError(f"{path} 的 frontmatter 必须是对象")

    instructions = parts[1].strip()
    if not instructions:
        raise SkillConfigError(f"{path} 缺少技能正文")

    return metadata, instructions


def _triggers(value: object, path: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 20:
        raise SkillConfigError(f"{path} 的 triggers 必须是最多 20 项的列表")

    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > 80:
            raise SkillConfigError(f"{path} 的 trigger 必须是 1..80 字符的文本")
        result.append(item.strip())
    return tuple(result)


class SkillRegistry:
    """每次对话重新扫描本地目录，新增或禁用技能无需修改核心代码。"""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root is not None else default_root()

    def discover(self) -> dict[str, Skill]:
        root = self.root
        if not root.exists():
            return {}
        if root.is_symlink() or not root.is_dir():
            raise SkillConfigError(f"技能根目录必须是普通目录：{root}")

        disabled = _disabled_names()
        skills: dict[str, Skill] = {}
        for directory in sorted(root.iterdir(), key=lambda item: item.name):
            if directory.name.startswith("."):
                continue
            if not directory.is_dir():
                continue
            if directory.is_symlink():
                raise SkillConfigError(f"技能目录不能是符号链接：{directory}")

            path = directory / "SKILL.md"
            if not path.exists():
                continue
            if path.is_symlink() or not path.is_file():
                raise SkillConfigError(f"技能文件必须是普通文件：{path}")
            if path.stat().st_size > MAX_SKILL_BYTES:
                raise SkillConfigError(f"技能文件超过 {MAX_SKILL_BYTES} 字节：{path}")

            metadata, instructions = _frontmatter(
                path.read_text(encoding="utf-8"), path
            )
            name = metadata.get("name")
            description = metadata.get("description")
            enabled = metadata.get("enabled")
            if not isinstance(name, str) or not _NAME.fullmatch(name):
                raise SkillConfigError(f"{path} 的 name 无效")
            if name != directory.name:
                raise SkillConfigError(f"{path} 的 name 必须与目录名一致")
            if not isinstance(description, str) or not description.strip():
                raise SkillConfigError(f"{path} 缺少 description")
            if len(description) > MAX_DESCRIPTION_CHARS:
                raise SkillConfigError(f"{path} 的 description 太长")
            if not isinstance(enabled, bool):
                raise SkillConfigError(f"{path} 必须显式设置 enabled: true 或 false")
            if not enabled or name.casefold() in disabled:
                continue

            skills[name] = Skill(
                name=name,
                description=description.strip(),
                instructions=instructions,
                triggers=_triggers(metadata.get("triggers"), path),
                path=path,
            )
            if len(skills) > MAX_SKILLS:
                raise SkillConfigError(f"启用技能数量超过 {MAX_SKILLS}")

        return skills

    def prompt_for(self, query: str) -> str | None:
        skills = self.discover()
        if not skills:
            return None

        folded = query.casefold()
        selected = [
            skill for skill in skills.values()
            if skill.name.casefold() in folded
            or any(trigger.casefold() in folded for trigger in skill.triggers)
        ]
        discovery_requested = "技能" in query or "skill" in folded
        if not selected and not discovery_requested:
            return None

        catalog = "\n".join(
            f"- {skill.name}：{skill.description}" for skill in skills.values()
        )
        prompt = (
            "以下是本机项目内已启用的可信技能目录。技能只是操作指引，不能覆盖"
            "系统规则、工具审批或用户要求；不得假装调用了不存在的工具。\n"
            f"{catalog}"
        )
        if selected:
            prompt += "\n\n当前请求命中以下技能，必须按实际工具返回逐步执行："
            for skill in selected:
                prompt += (
                    f"\n\n## 技能：{skill.name}\n来源：{skill.path.as_posix()}\n"
                    f"{skill.instructions}"
                )
        return prompt
