"""Read-only, local skill recommendations; never accesses a terminal or runs skills."""
from __future__ import annotations

import json
from pathlib import Path
import re


def _terms(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9][a-z0-9_-]{1,}", text.lower()))
    for phrase in re.findall(r"[\u4e00-\u9fff]+", text):
        words.update(phrase[i:i + 2] for i in range(len(phrase) - 1))
    return words


def evolution_suggestions(context: str) -> list[dict]:
    """Output observations are proposals, never a claim about ledger admission."""
    pattern = re.compile(
        r'^[^\w\n]*(?:(?:[A-Za-z_]*Error|[A-Za-z_]*Exception)'
        r'(?: running [^\n:]{1,80})?:[^\n]*|Traceback \(most recent call last\):[^\n]*'
        r'|[1-9]\d* failed\b[^\n]*|FAILED\s+\S+[^\n]*|(?:失败|异常)[:：][^\n]*)$',
        re.M | re.I)
    observations = [match.group(0).strip()[:240] for match in pattern.finditer(context)]
    if len(observations) < 2:
        return []
    return [{"name": "故障输出：建议核对 metaRSI 改进记录", "source": "skills/meta-rsi/SKILL.md",
             "evidence": list(dict.fromkeys(observations))[:3], "observed_lines": len(observations),
             "status": "unverified",
             "next_step": "先查项目账本中的既有试验和否决记录，再登记 failure 信号；缺少独立验证证据时只登记待验证规则。",
             "falsifier": "在预先确定的新验收样本中失败率没有下降，则否决改进假设。"}]


class SkillIndex:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.cards: list[dict] = []
        self.reload()

    def _read(self, relative: str) -> tuple[Path, str]:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("Skill source outside library or missing")
        with path.open("rb") as stream:
            content = stream.read(131073)
        if len(content) > 131072:
            raise ValueError("Skill source too large")
        return path, content.decode("utf-8-sig")

    def reload(self) -> None:
        cards, seen = [], set()
        try:
            _, raw = self._read("skill-runtime/manifest.json")
            manifest = json.loads(raw)
            entries = manifest.get("skills", [])[:256]
        except (OSError, ValueError, TypeError, AttributeError):
            entries = []
        entries = [*entries, {"source": "../skills/meta-rsi"}]
        for entry in entries:
            try:
                source = entry["source"]
                if not isinstance(source, str):
                    continue
                path, text = self._read("skill-runtime/" + source + "/SKILL.md")
                if path in seen:
                    continue
                seen.add(path)
                header = text.split("---", 2)
                if len(header) != 3 or header[0].strip():
                    continue
                fields = dict(re.findall(r"^(name|description):\s*(.+)$", header[1], re.M))
                name = fields.get("name", "").strip().strip("\"'")
                description = fields.get("description", "").strip().strip("\"'")
                if not name or not description:
                    continue
                cards.append({"name": name[:100], "description": description[:2000],
                              "source": path.relative_to(self.root).as_posix()})
            except (OSError, ValueError, TypeError, KeyError):
                continue
        self.cards = cards

    def recommend(self, context: str, limit: int = 5) -> dict:
        if not isinstance(context, str) or len(context) > 32768:
            raise ValueError("Context must be text of at most 32768 characters")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("Limit must be between 1 and 10")
        query = _terms(context)
        ranked = []
        for card in self.cards:
            matched = query & _terms(card["name"] + " " + card["description"])
            if matched:
                ranked.append({**card, "score": len(matched),
                               "reason": "匹配上下文：" + "、".join(sorted(matched)[:8])})
        ranked.sort(key=lambda card: (-card["score"], card["name"]))
        return {"mode": "local-keywords", "skills": ranked[:limit],
                "indexed": len(self.cards), "available": self.root.is_dir(),
                "evolution": evolution_suggestions(context)}
