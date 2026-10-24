"""
Visual skills: skills/*.md, each with a frontmatter `description` (when to use it)
and a body (how to draw it). Dependency-free so both the lesson writer, which
decides *when*, and the drawer, which needs the *how*, can import it.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

SKILLS_DIR = Path(__file__).parent / "skills"


class Skill(BaseModel):
    name: str
    description: str
    body: str


def load_skills(directory: Path = SKILLS_DIR) -> dict[str, Skill]:
    skills = {}
    for path in sorted(directory.glob("*.md")):
        _, front, body = path.read_text(encoding="utf-8").split("---", 2)
        meta = dict(line.split(":", 1) for line in front.strip().splitlines())
        skill = Skill(name=meta["name"].strip(), description=meta["description"].strip(), body=body.strip())
        skills[skill.name] = skill
    return skills


SKILLS = load_skills()


def menu() -> str:
    """One line per visual kind: what a planner sees when choosing (descriptions only)."""
    return "\n".join(f"- {s.name}: {s.description}" for s in SKILLS.values())
