# 논문 정리 자동화

PDF 논문을 자동으로 분석하여 Obsidian 마크다운 노트를 생성하고, Zotero와 양방향 연동하는 자동화 도구입니다.

---

## 주요 기능

| 기능 | 설명 |
|------|------|
| PDF → 마크다운 | PDF에서 텍스트 추출 → Gemini AI가 서지정보 + 심층 분석(API 사용, Gemini 말고 다른 AI 쓰셔도 됩니다.) → Obsidian 마크다운 자동 생성 |
| 폴더 감시 모드 | 논문 PDF 폴더를 실시간 감시하여 새 파일 추가 시 자동 처리 |
| Zotero 연동 모드 | Zotero 라이브러리를 폴링하여 새로 추가된 논문을 자동 처리 + 분석 결과를 Zotero 노트로 역동기화 |
| 일괄 생성 | 이미 추출된 JSON 데이터에서 누락된 마크다운만 일괄 생성 |

### 처리 파이프라인

```
PDF 논문
  ├─ [수동/감시 모드] PDF 텍스트 추출 (PyMuPDF)
  │     ↓
  │   Stage 1: Gemini Lite → 서지정보 추출 (제목/저자/연도/저널)
  │     ↓
  │   Stage 2: Gemini → 심층 분석 (핵심주장/방법론/발견/발췌)
  │     ↓
  │   Obsidian 마크다운 노트 생성
  │
  └─ [Zotero 모드] Zotero API 폴링 → 새 아이템 감지
        ↓
      Zotero 서지정보 활용 (Stage 1 생략)
        ↓
      Stage 2: Gemini → 심층 분석
        ↓
      마크다운 생성 + Zotero 노트 역동기화
```

---

## 1단계: 사전 준비

### 1-1. Python 설치

Python **3.10 이상**이 필요합니다.

- **Windows**: [python.org](https://www.python.org/downloads/) 에서 다운로드 → 설치 시 **"Add Python to PATH" 체크**
- **macOS**: `brew install python`
- **Linux/WSL**: `sudo apt install python3 python3-pip`

설치 확인:
```bash
python --version   # Python 3.10+ 확인
```

> Windows에서 `python`이 안 되면 `python3` 또는 `py`로 시도하세요.

### 1-2. Gemini API 키 발급(다른 AI 쓰고 싶으면 다른 거 쓰셔도 됩니다.)

1. [Google AI Studio](https://aistudio.google.com/apikey) 접속
2. Google 계정으로 로그인
3. **"Create API Key"** 클릭
4. 생성된 키를 복사해 둡니다 (예: `AIzaSy...`)

> 무료 요금제로 분당 15회 요청 가능합니다. 기본 설정(4초 딜레이)이면 제한에 걸리지 않습니다.

### 1-3. Zotero API 설정

> Zotero 연동이 필요 없다면 이 단계를 건너뛰어도 됩니다.
> 단, `.env`에 값은 넣어야 스크립트가 실행됩니다 (임의 값이라도 입력).

1. [Zotero 설정 페이지](https://www.zotero.org/settings) 접속
2. **Your user ID** (숫자)를 메모 → `ZOTERO_LIBRARY_ID`로 사용
3. [API Keys 페이지](https://www.zotero.org/settings/keys) 에서 **"Create new private key"**
   - **Allow library access**: 체크
   - **Allow write access**: 체크 (역동기화에 필요)
4. 생성된 키를 복사 → `ZOTERO_API_KEY`로 사용

---

## 2단계: 설치

### 2-1. 프로젝트 폴더 다운로드

이 폴더 전체를 원하는 위치에 복사합니다.

```
논문 정리 자동화/
  ├── config.py          # 설정 (자동으로 .env 읽음)
  ├── main.py            # 메인 실행 스크립트
  ├── extractor.py       # PDF 텍스트 추출
  ├── summarizer.py      # Gemini AI 분석
  ├── markdown_gen.py    # 마크다운 생성
  ├── zotero_sync.py     # Zotero 연동
  ├── .env.example       # 환경변수 템플릿
  ├── requirements.txt   # 패키지 목록
  └── ...
```

### 2-2. 패키지 설치

터미널(명령 프롬프트)에서 프로젝트 폴더로 이동 후 실행:

```bash
cd "프로젝트 폴더 경로"
pip install -r requirements.txt
```

설치되는 패키지:

| 패키지 | 용도 |
|--------|------|
| PyMuPDF | PDF 텍스트 추출 |
| watchdog | 폴더 실시간 감시 |
| requests | Gemini API 호출 |
| google-generativeai | Gemini 관련 유틸 |
| pyzotero | Zotero API 연동 |

### 2-3. 환경변수 설정 (.env 파일)

`.env.example` 파일을 같은 폴더에 `.env`로 복사한 후 값을 채웁니다.

**Windows (명령 프롬프트):**
```cmd
copy .env.example .env
notepad .env
```

**macOS / Linux:**
```bash
cp .env.example .env
nano .env
```

`.env` 파일 작성 예시:

```env
# ── 필수 경로 ──────────────────────────────────────
# 논문 PDF가 저장된 폴더 (절대 경로)
PDF_DIR=C:/Users/홍길동/Documents/논문

# Obsidian 마크다운 출력 폴더
MARKDOWN_DIR=C:/Users/홍길동/Documents/Obsidian Vault/02-Literature

# Zotero storage 폴더
ZOTERO_STORAGE=C:/Users/홍길동/Zotero/storage

# ── API 키 ─────────────────────────────────────────
GEMINI_API_KEY=AIzaSy_여기에_본인_키_입력
ZOTERO_LIBRARY_ID=1234567
ZOTERO_API_KEY=abcdef1234567890
```

> **경로 형식 안내**
> - **Windows**: `C:/Users/사용자명/Documents/논문` (슬래시 `/` 사용 권장, 역슬래시 `\`도 가능)
> - **macOS**: `/Users/사용자명/Documents/논문`
> - **Linux/WSL**: `/home/사용자명/Documents/논문` 또는 `/mnt/c/Users/사용자명/Documents/논문`

### 2-4. 폴더 준비

- **PDF 폴더** (`PDF_DIR`): 논문 PDF를 저장할 폴더. 없으면 미리 생성하세요.
- **마크다운 폴더** (`MARKDOWN_DIR`): Obsidian Vault 내부 폴더. 없으면 미리 생성하세요.
- 두 폴더 모두 실제로 존재해야 합니다.

---

## 3단계: 실행

프로젝트 폴더에서 아래 명령을 실행합니다.

### 방법 A: 수동 일괄 처리

```bash
python main.py
```

- PDF 폴더의 **아직 처리하지 않은** PDF를 모두 찾아 분석
- 각 논문마다 마크다운 파일 자동 생성
- 처리 결과는 `extracted_papers.json`에 기록

### 방법 B: 폴더 감시 모드 (추천)

```bash
python main.py --watch
```

- PDF 폴더를 실시간 감시
- 새 PDF를 넣으면 자동으로 텍스트 추출 → AI 분석 → 마크다운 생성
- `Ctrl+C`로 종료

### 방법 C: Zotero 연동 모드

```bash
python main.py --zotero
```

- Zotero 라이브러리를 60초마다 확인
- 새로 추가된 논문 감지 시:
  - Zotero의 서지정보를 활용 (AI 서지정보 추출 생략 → 더 정확)
  - PDF가 로컬에 있으면 텍스트 추출 후 심층 분석
  - 마크다운 생성 + 분석 결과를 Zotero 노트로 저장
- `Ctrl+C`로 종료

### 방법 D: 누락 마크다운 일괄 생성

```bash
python main.py --all
```

- 이미 추출된 JSON 데이터 중 마크다운이 없는 항목만 생성
- 이전에 처리가 중단된 경우에 유용

---

## 4단계: 결과 확인

### 생성되는 마크다운 파일

마크다운 출력 폴더에 `@연도_논문제목.md` 형식으로 생성됩니다.

```
02-Literature/
  ├── @2024_Artificial-intelligence-and-knowledge-management.md
  ├── @2023_Deep-learning-for-natural-language-processing.md
  └── ...
```

### 마크다운 노트 구조

```markdown
---
title: "논문 제목"
year: 2024
tags: [AI, Knowledge-Management]
doi: 10.1234/example
---

# 논문 제목

## 서지정보 (Citation)
- 저자, 연도, 저널, DOI 등

## 초록/요약 (Abstract)
## 핵심 주장 (Key Claims)
## 연구 방법 (Method)
## 주요 발견 (Findings)
## 내용 발췌 (Excerpts)
## 나의 생각 (My Thoughts)
## 연결 (Links)
```

### JSON 데이터 파일

PDF 폴더에 `extracted_papers.json`이 자동 생성됩니다. 이 파일에 추출 결과가 누적되며, 중복 처리를 방지합니다.

---

## 백그라운드 자동 실행 (선택)

PC를 켤 때마다 자동으로 실행되도록 설정할 수 있습니다.

### Windows: 작업 스케줄러

1. `Win+R` → `taskschd.msc` 입력 → 확인
2. 오른쪽 **"기본 작업 만들기"** 클릭
3. 이름: `논문 폴더 감시` (또는 원하는 이름)
4. 트리거: **"컴퓨터 시작 시"**
5. 동작: **"프로그램 시작"**
   - 프로그램: `python` (또는 `C:\Users\사용자명\AppData\Local\Programs\Python\Python312\python.exe` 전체 경로)
   - 인수: `main.py --watch`
   - 시작 위치: 프로젝트 폴더 경로 (예: `C:\Users\사용자명\Documents\논문 정리 자동화`)
6. Zotero 연동도 필요하면 같은 방식으로 `main.py --zotero`용 작업 추가

### Linux / WSL: systemd 서비스

1. 서비스 파일 생성:

```bash
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/paper-watch.service << 'EOF'
[Unit]
Description=논문 PDF 폴더 감시 서비스

[Service]
Type=simple
WorkingDirectory=/스크립트/폴더/경로
ExecStart=/usr/bin/python3 main.py --watch
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
```

2. 서비스 등록 및 시작:

```bash
systemctl --user daemon-reload
systemctl --user enable --now paper-watch.service
```

3. Zotero 연동도 필요하면 `zotero-sync.service`를 같은 방식으로 생성 (`--watch` → `--zotero`로 변경)

4. 부팅 시 자동 시작 (WSL):

```bash
sudo loginctl enable-linger $USER
```

### macOS: launchd

```bash
cat > ~/Library/LaunchAgents/com.paper-watch.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.paper-watch</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/python3</string>
        <string>main.py</string>
        <string>--watch</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/스크립트/폴더/경로</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.paper-watch.plist
```

---

## 문제 해결

### 실행 시 "환경변수 미설정" 오류

```
[오류] 환경변수 'PDF_DIR' 미설정 — 논문 PDF가 저장된 폴더 경로
```

→ `.env` 파일이 프로젝트 폴더에 있는지, 값이 채워져 있는지 확인하세요.

### Gemini API 오류

```
API_KEY_INVALID
```

→ `.env`의 `GEMINI_API_KEY` 값이 정확한지 확인하세요. [Google AI Studio](https://aistudio.google.com/apikey)에서 재발급 가능합니다.

### Gemini 타임아웃

```
Timeout 120s
```

→ 긴 논문은 분석에 시간이 걸립니다. `.env`에서 `GEMINI_TIMEOUT=180` 등으로 늘릴 수 있습니다.

### Zotero 연결 실패

→ `ZOTERO_LIBRARY_ID`와 `ZOTERO_API_KEY`를 다시 확인하세요.
→ API 키 생성 시 **Allow library access**와 **Allow write access**가 체크되어 있어야 합니다.

### PDF 텍스트 추출 실패

→ 스캔된 이미지 PDF는 텍스트 추출이 안 됩니다. OCR이 적용된 PDF를 사용하세요.

### `pip install` 오류

→ Python 버전이 3.10 이상인지 확인하세요.
→ Windows에서 권한 오류가 나면 `pip install --user -r requirements.txt`를 사용하세요.

---

## 보조 스크립트

메인 실행(`main.py`) 외에 데이터 관리용 보조 스크립트가 포함되어 있습니다.

| 스크립트 | 용도 |
|----------|------|
| `reprocess.py` | 특정 논문을 지정하여 재분석 |
| `regenerate_excerpts.py` | 기존 논문의 발췌(Excerpts) 섹션만 재생성 |
| `regenerate_excerpts_skipped.py` | 발췌 생성이 누락된 논문만 보완 |
| `normalize_tags.py` | 한글 태그를 영문으로 일괄 변환 |
| `migrate_biblio_fields.py` | 서지정보 필드 형식 일괄 마이그레이션 |
| `migrate_excerpts_format.py` | 발췌 섹션 형식 일괄 마이그레이션 |
| `crossref_enrich.py` | CrossRef API로 서지정보 보강 |
| `repair_zotero.py` | Zotero 메타데이터 불일치 복구 |
| `obsidian_to_zotero.py` | Obsidian 마크다운 → Zotero 역방향 동기화 |

---

## 빠른 시작 요약

```bash
# 1. 패키지 설치
pip install -r requirements.txt

# 2. 환경변수 설정
cp .env.example .env
# .env 파일을 열어 경로와 API 키 입력

# 3-A. PDF 폴더에 논문 넣고 수동 실행
python main.py

# 3-B. 또는 폴더 감시 모드로 상시 실행
python main.py --watch

# 3-C. 또는 Zotero 연동 모드
python main.py --zotero
```
