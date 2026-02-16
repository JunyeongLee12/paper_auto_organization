"""Zotero 연동 모듈

워크플로우:
    Zotero에 논문 추가 (브라우저 커넥터 or PDF 드래그)
        → Zotero가 CrossRef/DOI로 서지정보 자동 수집
        → zotero_sync.py가 60초 폴링으로 새 아이템 감지
        → PDF 로컬 경로 탐색 → extractor.extract_one()
        → summarizer.summarize_paper(paper, biblio=zotero_meta)
              (Stage 1 건너뜀, Zotero 서지정보 직접 사용)
        → markdown_gen.generate_markdown(summary, pdf_filename, zotero_key)
        → post_note(): 분석 결과를 Zotero 아이템 노트로 역방향 저장

상태 파일: SCRIPT_DIR/zotero_state.json
    {
        "last_version": 12345,
        "processed_keys": ["ABC123", "DEF456"],
        "processed_titles": ["normalized title 1", "normalized title 2"]
    }

중복 감지:
    - Key 기반: processed_keys 목록으로 동일 Zotero 아이템 재처리 방지
    - 제목 기반: processed_titles로 동일 논문의 다른 키 중복 방지
      (Zotero Connector 중복 저장, 다른 형식으로 같은 논문 등)
    - HTML 태그 제거, 소문자 변환, 공백 정규화로 제목 정규화
"""

import hashlib
import json
import re
import sys
import threading
import time
from pathlib import Path

try:
    from pyzotero import zotero
except ImportError:
    print("[오류] pyzotero 패키지가 필요합니다: pip install pyzotero")
    sys.exit(1)

from config import (
    MARKDOWN_DIR,
    SCRIPT_DIR,
    ZOTERO_API_KEY,
    ZOTERO_LIBRARY_ID,
    ZOTERO_NOTE_SYNC,
    ZOTERO_POLL_INTERVAL,
    ZOTERO_STORAGE,
)
from extractor import extract_one
from markdown_gen import generate_markdown
from normalize_tags import normalize_tag
from summarizer import summarize_paper

STATE_FILE = SCRIPT_DIR / "zotero_state.json"

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _normalize_title(title: str) -> str:
    """제목을 정규화하여 중복 비교용 키로 변환.

    - HTML 태그 제거 (<i>Technovation</i> → Technovation)
    - 소문자 변환
    - 연속 공백 → 단일 공백
    - 앞뒤 공백 제거
    """
    cleaned = _HTML_TAG_RE.sub("", title)
    return re.sub(r"\s+", " ", cleaned).strip().lower()


class ZoteroSync:
    """Zotero 라이브러리와 논문 분석 파이프라인을 연결하는 클래스."""

    def __init__(self, library_id: str, api_key: str, storage_dir: Path):
        self.zot = zotero.Zotero(library_id, "user", api_key)
        self.storage_dir = storage_dir
        self.state = self.load_state()
        self.obs_watcher = None  # ObsidianWatcher 참조 (충돌 방지용)

    # ── 상태 관리 ──────────────────────────────────────────────────────────────

    def load_state(self) -> dict:
        """처리 상태 파일 로드. 없으면 초기 상태 반환."""
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {"last_version": 0, "processed_keys": [], "processed_titles": []}

    def save_state(self):
        """처리 상태를 파일에 저장."""
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    # ── 새 아이템 감지 ─────────────────────────────────────────────────────────

    def get_new_items(self) -> list[dict]:
        """마지막 확인 이후 추가된 새 아이템 목록 반환.

        Zotero API의 `since` 파라미터로 효율적인 변경 감지.
        학위논문(thesis, dissertation 타입)은 제외.

        중복 감지:
        - processed_keys: Zotero 아이템 키 기반 (동일 아이템 재처리 방지)
        - processed_titles: 정규화된 제목 기반 (동일 논문 다른 키 중복 방지)
        """
        last_version = self.state.get("last_version", 0)
        processed_keys = set(self.state.get("processed_keys", []))
        processed_titles = set(self.state.get("processed_titles", []))

        try:
            # since=버전 이후 변경된 아이템만 가져오기
            items = self.zot.items(since=last_version, itemType="-attachment || note")
        except Exception as e:
            print(f"  [오류] Zotero API 호출 실패: {e}")
            return []

        # 라이브러리 현재 버전 갱신
        try:
            lib_version = self.zot.last_modified_version()
            self.state["last_version"] = lib_version
        except Exception:
            pass

        new_items = []
        updated_items = []
        skip_types = {"thesis", "dissertation"}

        for item in items:
            key = item.get("key", "")
            item_type = item.get("data", {}).get("itemType", "")
            title = item.get("data", {}).get("title", "")
            norm_title = _normalize_title(title)

            # attachment/note 제외
            if item_type in ("attachment", "note"):
                continue
            # 학위논문 제외
            if item_type in skip_types:
                continue
            # 이미 처리된 아이템: 변경 감지용 업데이트 큐에 추가
            if key in processed_keys:
                updated_items.append(item)
                continue
            # 동일 제목 중복 제외 (제목 기반)
            if norm_title and norm_title in processed_titles:
                print(f"  [중복 스킵] 동일 제목 이미 처리됨: {title[:60]}")
                # 처리된 키로 등록하여 다음 폴링에서도 스킵
                self.state["processed_keys"].append(key)
                continue

            new_items.append(item)

        # 기존 아이템 변경 감지 → 서지정보만 업데이트
        if updated_items:
            updated_count = 0
            for item in updated_items:
                if self.update_existing_markdown(item):
                    updated_count += 1
            if updated_count:
                print(f"  → 기존 논문 {updated_count}개 서지정보 업데이트")

        return new_items

    # ── PDF 경로 탐색 ──────────────────────────────────────────────────────────

    def get_pdf_path(self, item_key: str) -> Path | None:
        """Zotero 아이템의 로컬 PDF 경로를 반환.

        Zotero storage 구조: {storage_dir}/{attachment_key}/*.pdf
        """
        try:
            children = self.zot.children(item_key)
        except Exception as e:
            print(f"  [경고] children 조회 실패 ({item_key}): {e}")
            return None

        for child in children:
            child_data = child.get("data", {})
            if child_data.get("itemType") != "attachment":
                continue
            if child_data.get("contentType") != "application/pdf":
                continue

            att_key = child.get("key", "")
            att_dir = self.storage_dir / att_key

            if not att_dir.exists():
                continue

            pdf_files = list(att_dir.glob("*.pdf"))
            if pdf_files:
                return pdf_files[0]

        return None

    # ── 서지정보 변환 ──────────────────────────────────────────────────────────

    def build_biblio(self, item: dict) -> dict:
        """Zotero 아이템 메타데이터를 시스템 서지정보 dict로 변환.

        Returns:
            {title, author, year, journal, abstract, tags, zotero_key}
        """
        data = item.get("data", {})
        key = item.get("key", "")

        # 저자 목록 생성
        creators = data.get("creators", [])
        authors = []
        for c in creators:
            if c.get("creatorType") != "author":
                continue
            last = c.get("lastName", "")
            first = c.get("firstName", "")
            name = f"{last}, {first}".strip(", ") if last else first
            if name:
                authors.append(name)
        author_str = "; ".join(authors)

        # 연도 추출
        year = ""
        date_str = data.get("date", "")
        year_match = re.search(r"((?:19|20)\d{2})", date_str)
        if year_match:
            year = year_match.group(1)

        # 저널/출처명 (여러 필드 순서대로 시도)
        journal = (
            data.get("publicationTitle")
            or data.get("bookTitle")
            or data.get("proceedingsTitle")
            or data.get("publisher")
            or ""
        )

        # Zotero 태그 → 정규화
        raw_tags = [t.get("tag", "") for t in data.get("tags", []) if t.get("tag")]
        normalized_tags = [normalize_tag(t) for t in raw_tags if t]
        # 중복 제거 (순서 유지)
        seen: set[str] = set()
        tags: list[str] = []
        for t in normalized_tags:
            if t not in seen:
                seen.add(t)
                tags.append(t)

        return {
            "title": data.get("title", ""),
            "author": author_str,
            "year": year,
            "journal": journal,
            "abstract": data.get("abstractNote", ""),
            "tags": tags,
            "zotero_key": key,
            "doi": data.get("DOI", ""),
            "volume": data.get("volume", ""),
            "issue": data.get("issue", ""),
            "pages": data.get("pages", ""),
            "issn": data.get("ISSN", ""),
            "url": data.get("url", ""),
            "language": data.get("language", ""),
            "publisher": data.get("publisher", ""),
        }

    # ── 역방향: 분석 결과 → Zotero 노트 ──────────────────────────────────────

    def post_note(self, item_key: str, summary: dict):
        """분석 결과를 Zotero 아이템 노트로 저장 (역방향 동기화).

        Args:
            item_key: Zotero 아이템 키
            summary: summarize_paper() 반환값
        """
        def _h(tag: str, text: str) -> str:
            return f"<{tag}>{text}</{tag}>"

        def _list_html(value) -> str:
            if isinstance(value, list):
                items = "".join(f"<li>{item}</li>" for item in value if item)
                return f"<ul>{items}</ul>"
            return f"<p>{value}</p>" if value else ""

        def _text_html(value) -> str:
            if isinstance(value, list):
                items = "".join(f"<li>{line}</li>" for line in value if line)
                return f"<ul>{items}</ul>" if items else ""
            if not value:
                return ""
            # 마크다운/줄바꿈이 섞인 문자열을 Zotero 노트에서 읽기 쉽도록 단락 분리
            blocks = [b.strip() for b in str(value).split("\n\n") if b.strip()]
            return "".join(f"<p>{b.replace(chr(10), '<br>')}</p>" for b in blocks)

        note_html = (
            _h("h2", "핵심 주장")
            + _list_html(summary.get("key_claims", ""))
            + _h("h2", "연구 방법")
            + f"<p>{summary.get('method', '')}</p>"
            + _h("h2", "주요 발견")
            + _list_html(summary.get("findings", ""))
            + _h("h2", "내용 발췌")
            + _text_html(summary.get("excerpts", ""))
        )

        note_item = {
            "itemType": "note",
            "parentItem": item_key,
            "note": note_html,
            "tags": [{"tag": "auto-analyzed"}],
        }

        try:
            self.zot.create_items([note_item])
            print(f"  [Zotero] 노트 저장 완료 → {item_key}")
        except Exception as e:
            print(f"  [경고] Zotero 노트 저장 실패 ({item_key}): {e}")

    # ── 기존 마크다운 서지정보 업데이트 ────────────────────────────────────────

    def find_markdown_by_key(self, zotero_key: str) -> Path | None:
        """zotero_key frontmatter로 마크다운 파일 탐색."""
        for md_path in MARKDOWN_DIR.glob("@*.md"):
            try:
                text = md_path.read_text(encoding="utf-8")
            except Exception:
                continue
            if f"zotero_key: {zotero_key}" in text:
                return md_path
        return None

    def update_existing_markdown(self, item: dict):
        """기존 마크다운의 서지정보 섹션만 업데이트. AI 분석 섹션은 보존."""
        key = item.get("key", "")
        md_path = self.find_markdown_by_key(key)
        if md_path is None:
            return False

        biblio = self.build_biblio(item)
        try:
            text = md_path.read_text(encoding="utf-8")
        except Exception:
            return False

        # 서지정보 섹션 필드별 업데이트 (빈 줄 패턴 매칭)
        field_patterns = [
            ("저자",    biblio.get("author", "")),
            ("연도",    biblio.get("year", "")),
            ("저널/출처", biblio.get("journal", "")),
            ("출판사",  biblio.get("publisher", "")),
            ("권\\(Vol\\)", biblio.get("volume", "")),
            ("호\\(Issue\\)", biblio.get("issue", "")),
            ("페이지",  biblio.get("pages", "")),
            ("DOI",     biblio.get("doi", "")),
            ("ISSN",    biblio.get("issn", "")),
            ("URL",     biblio.get("url", "")),
            ("언어",    biblio.get("language", "")),
        ]

        changed = False
        for field_name, new_val in field_patterns:
            if not new_val:
                continue
            # 기존 값이 있는 줄은 건드리지 않고, 빈 줄만 업데이트
            pattern = rf'(\*\*{field_name}\*\*:[ \t]*)$'
            replacement = rf'\g<1>{new_val}'
            new_text, n = re.subn(pattern, replacement, text, flags=re.MULTILINE)
            if n:
                text = new_text
                changed = True

        # frontmatter doi 업데이트
        doi_val = biblio.get("doi", "")
        if doi_val:
            new_text, n = re.subn(
                r'^(doi:[ \t]*)$', rf'\g<1>{doi_val}', text, flags=re.MULTILINE
            )
            if n:
                text = new_text
                changed = True

        if changed:
            # 자체 수정으로 등록하여 ObsidianWatcher의 피드백 루프 방지
            if self.obs_watcher:
                self.obs_watcher.mark_self_modified(md_path)
            md_path.write_text(text, encoding="utf-8")
            print(f"  [업데이트] 서지정보 갱신: {md_path.name}")

        return changed

    # ── 단일 아이템 처리 ───────────────────────────────────────────────────────

    def process_item(self, item: dict):
        """Zotero 아이템 1개를 분석하고 마크다운 + 노트를 생성."""
        key = item.get("key", "")
        title = item.get("data", {}).get("title", key)
        print(f"\n[Zotero] 처리 중: {title[:60]}")

        # PDF 경로 탐색
        pdf_path = self.get_pdf_path(key)
        if pdf_path is None:
            print(f"  [경고] 로컬 PDF 없음 — 텍스트 없이 서지정보만 사용")
            paper = {"file_name": f"{key}.pdf", "full_text": "", "metadata": {}, "page_count": 0}
        else:
            print(f"  PDF: {pdf_path.name}")
            try:
                paper = extract_one(pdf_path)
            except Exception as e:
                print(f"  [오류] PDF 추출 실패: {e}")
                paper = {"file_name": pdf_path.name, "full_text": "", "metadata": {}, "page_count": 0}

        # Zotero 서지정보 빌드
        biblio = self.build_biblio(item)

        # 요약 (Stage 1 건너뜀)
        print("  AI 분석 중...")
        summary = summarize_paper(paper, biblio=biblio)

        # 마크다운 생성
        pdf_filename = paper.get("file_name", f"{key}.pdf")
        md_path = generate_markdown(summary, pdf_filename, zotero_key=key)

        # 역방향: Zotero 노트 저장
        if ZOTERO_NOTE_SYNC:
            self.post_note(key, summary)

        # 처리 완료 기록 (키 + 정규화 제목 모두 저장)
        self.state["processed_keys"].append(key)
        norm_title = _normalize_title(title)
        processed_titles: list = self.state.setdefault("processed_titles", [])
        if norm_title and norm_title not in processed_titles:
            processed_titles.append(norm_title)
        self.save_state()

        return md_path


# ── Obsidian → Zotero 파일 감시 ───────────────────────────────────────────────

class ObsidianWatcher:
    """Obsidian 마크다운 변경 감지 → Zotero 업데이트.

    수정 감지 기준:
      - 파일 mtime 변경 + 내용 해시 비교
    충돌 방지:
      - 자체 수정(Zotero→Obsidian 업데이트) 직후 3초간 해당 파일 이벤트 무시
    """

    SELF_MODIFY_COOLDOWN = 3.0  # 자체 수정 후 무시 시간(초)

    def __init__(self, zot, markdown_dir: Path):
        self.zot = zot
        self.markdown_dir = markdown_dir
        # {파일경로: (mtime, content_hash)}
        self._snapshots: dict[str, tuple[float, str]] = {}
        # 자체 수정 중인 파일 집합 {파일경로: 수정완료시각}
        self._self_modified: dict[str, float] = {}
        self._lock = threading.Lock()

    def _hash(self, text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    def mark_self_modified(self, path: Path):
        """자체 수정(Zotero→Obsidian 업데이트) 완료 후 호출하여 이벤트 무시 등록."""
        with self._lock:
            self._self_modified[str(path)] = time.time()

    def _is_self_modified(self, path_str: str) -> bool:
        with self._lock:
            ts = self._self_modified.get(path_str)
            if ts and time.time() - ts < self.SELF_MODIFY_COOLDOWN:
                return True
            if ts:
                del self._self_modified[path_str]
            return False

    def scan(self) -> list[Path]:
        """변경된 마크다운 파일 목록 반환."""
        changed = []
        for md_path in self.markdown_dir.glob("@*.md"):
            path_str = str(md_path)
            try:
                mtime = md_path.stat().st_mtime
                text = md_path.read_text(encoding="utf-8")
            except Exception:
                continue

            content_hash = self._hash(text)
            prev = self._snapshots.get(path_str)

            if prev is None:
                # 초기 스냅샷 등록
                self._snapshots[path_str] = (mtime, content_hash)
                continue

            prev_mtime, prev_hash = prev
            if mtime != prev_mtime and content_hash != prev_hash:
                if not self._is_self_modified(path_str):
                    changed.append(md_path)
                self._snapshots[path_str] = (mtime, content_hash)

        return changed

    def push_to_zotero(self, md_path: Path) -> bool:
        """마크다운 변경사항을 Zotero에 반영."""
        try:
            text = md_path.read_text(encoding="utf-8")
        except Exception:
            return False

        # frontmatter에서 zotero_key 추출
        fm_match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        if not fm_match:
            return False
        fm = fm_match.group(1)

        key_m = re.search(r'^zotero_key:\s*(.+)$', fm, re.MULTILINE)
        if not key_m or not key_m.group(1).strip():
            return False
        zotero_key = key_m.group(1).strip()

        # 태그 파싱
        tags_m = re.search(r'^tags:\s*\[(.+?)\]', fm, re.MULTILINE)
        tags = []
        if tags_m:
            tags = [t.strip().strip('"') for t in tags_m.group(1).split(",") if t.strip()]
            tags = [t for t in tags if t not in ("literature", "paper")]

        # 서지정보 섹션에서 확장 필드 파싱
        def get_field(label):
            m = re.search(rf'\*\*{re.escape(label)}\*\*:[ \t]*(.+)', text)
            if m:
                val = m.group(1).strip().rstrip("\r")
                if val and not val.startswith(">") and val != "-":
                    return val
            return ""

        # Zotero 현재 아이템 조회
        try:
            item = self.zot.item(zotero_key)
        except Exception as e:
            print(f"  [ObsidianWatcher] Zotero 조회 실패 ({zotero_key}): {e}")
            return False

        data = item.get("data", {})
        updates: dict = {}

        # 태그 업데이트 (변경된 경우)
        current_tags = {t.get("tag", "") for t in data.get("tags", [])}
        new_tags_set = set(tags)
        if new_tags_set != current_tags:
            updates["tags"] = [{"tag": t} for t in tags]

        # 확장 서지정보 필드 업데이트
        field_map = [
            ("저자",          None),           # 저자는 creators 구조 다름, 스킵
            ("저널/출처",      "publicationTitle"),
            ("출판사",         "publisher"),
            ("권(Vol)",       "volume"),
            ("호(Issue)",     "issue"),
            ("페이지",         "pages"),
            ("DOI",           "DOI"),
            ("ISSN",          "ISSN"),
            ("URL",           "url"),
            ("언어",           "language"),
        ]
        for label, zot_field in field_map:
            if not zot_field:
                continue
            md_val = get_field(label)
            zot_val = data.get(zot_field, "")
            if md_val and md_val != zot_val:
                updates[zot_field] = md_val

        if not updates:
            return False

        try:
            updated_data = dict(data)
            updated_data.update(updates)
            self.zot.update_item({
                "key": zotero_key,
                "version": data.get("version", 0),
                "data": updated_data,
            })
            print(f"  [Obsidian→Zotero] {md_path.name[:50]} → {list(updates.keys())}")
            return True
        except Exception as e:
            print(f"  [오류] Zotero 업데이트 실패 ({zotero_key}): {e}")
            return False


# ── 메인 폴링 루프 ─────────────────────────────────────────────────────────────


def watch_zotero():
    """Zotero 라이브러리를 주기적으로 폴링하여 새 논문을 자동 처리.
    동시에 Obsidian 파일 변경도 감지하여 Zotero에 역방향 동기화.
    """
    if not ZOTERO_LIBRARY_ID or not ZOTERO_API_KEY:
        print(
            "[오류] Zotero 설정이 없습니다.\n"
            "  환경변수 또는 config.py에 설정하세요:\n"
            "    ZOTERO_LIBRARY_ID  — zotero.org/settings 의 Your user ID\n"
            "    ZOTERO_API_KEY     — zotero.org/settings/keys 에서 생성"
        )
        sys.exit(1)

    print("=" * 60)
    print("Zotero 양방향 연동 모드 (Ctrl+C로 종료)")
    print(f"  Library ID : {ZOTERO_LIBRARY_ID}")
    print(f"  Storage    : {ZOTERO_STORAGE}")
    print(f"  폴링 간격  : {ZOTERO_POLL_INTERVAL}초")
    print(f"  역방향 노트: {'ON' if ZOTERO_NOTE_SYNC else 'OFF'}")
    print(f"  Obsidian 감시: {MARKDOWN_DIR}")
    print("=" * 60)

    sync = ZoteroSync(ZOTERO_LIBRARY_ID, ZOTERO_API_KEY, ZOTERO_STORAGE)
    obs_watcher = ObsidianWatcher(sync.zot, MARKDOWN_DIR)
    sync.obs_watcher = obs_watcher  # 충돌 방지 연결

    # 초기 스냅샷 구축 (기존 파일은 변경 대상 제외)
    obs_watcher.scan()

    try:
        while True:
            print(f"\n[{time.strftime('%H:%M:%S')}] Zotero 라이브러리 확인 중...")
            new_items = sync.get_new_items()

            if new_items:
                print(f"  → 새 논문 {len(new_items)}개 발견")
                for item in new_items:
                    sync.process_item(item)
            else:
                print("  → 새 논문 없음")

            sync.save_state()

            # Obsidian → Zotero 변경 감지
            changed_files = obs_watcher.scan()
            if changed_files:
                print(f"  [Obsidian] 변경 파일 {len(changed_files)}개 감지")
                for md_path in changed_files:
                    obs_watcher.push_to_zotero(md_path)

            time.sleep(ZOTERO_POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\n\nZotero 연동 종료.")
        sync.save_state()
