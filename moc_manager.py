"""동적 MOC(Map of Content) 관리 — 04-Structure/ 폴더 기반

하드코딩된 MOC 목록 없이, 디스크의 기존 MOC 파일을 동적으로 스캔하고
AI가 제안한 새 MOC를 자동 생성한다.
"""

from pathlib import Path

from config import MARKDOWN_DIR

MOC_DIR = MARKDOWN_DIR.parent / "04-Structure"


def scan_mocs() -> dict[str, str]:
    """04-Structure/MOC_*.md 파일을 스캔하여 {이름: 설명} dict 반환.

    설명은 파일 첫 번째 비어있지 않은 본문 줄(# 제목 제외)에서 추출.
    MOC 파일이 없으면 빈 dict 반환.
    """
    if not MOC_DIR.exists():
        return {}

    mocs: dict[str, str] = {}
    for path in sorted(MOC_DIR.glob("MOC_*.md")):
        name = path.stem  # e.g. "MOC_생성형AI"
        description = ""
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or stripped.startswith("---"):
                    continue
                description = stripped[:100]
                break
        except Exception:
            pass
        mocs[name] = description
    return mocs


def build_moc_catalog_text(mocs: dict[str, str]) -> str:
    """AI 프롬프트에 삽입할 MOC 목록 텍스트 생성."""
    if not mocs:
        return "현재 생성된 MOC가 없습니다. 논문 주제에 맞는 새 MOC를 자유롭게 제안하세요."

    lines = []
    for name, desc in mocs.items():
        if desc:
            lines.append(f"- {name}: {desc}")
        else:
            lines.append(f"- {name}")
    return "\n".join(lines)


def format_moc_links(moc_names: list[str]) -> str:
    """MOC 이름 리스트를 Obsidian 위키링크 형식으로 변환.

    예: ["MOC_AI", "MOC_경영"] → "[[MOC_AI]] | [[MOC_경영]]"
    """
    if not moc_names:
        return ""
    return " | ".join(f"[[{name}]]" for name in moc_names)


def create_new_moc(name: str, description: str = "") -> Path:
    """04-Structure/에 새 MOC 파일을 표준 형식으로 생성.

    이미 존재하면 기존 파일 경로만 반환(덮어쓰지 않음).
    """
    MOC_DIR.mkdir(parents=True, exist_ok=True)

    # MOC_ 접두사 보장
    if not name.startswith("MOC_"):
        name = f"MOC_{name}"

    path = MOC_DIR / f"{name}.md"
    if path.exists():
        return path

    # 주제명 추출: "MOC_생성형AI" → "생성형AI"
    topic = name.removeprefix("MOC_")

    content = f"""\
# MOC: {topic}

{description or f"{topic} 관련 연구 허브"}

---

## 핵심 개념

- [논문 처리 시 자동으로 보강됩니다]

---

## 연구 질문

1. [연구 질문을 추가하세요]

---

#MOC #{topic}
"""
    path.write_text(content, encoding="utf-8")
    print(f"  [MOC] 새 MOC 생성: {name}")
    return path
