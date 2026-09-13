# 뉴스 레이더 (news-radar)

한국·해외 기술/산업 뉴스를 수집해 9개 전선(반도체·AI 모델·전기차·우주·바이오·에너지·로봇·양자·기후)으로 분류·점수화하고, "내 일상에 어떤 영향" 카피까지 생성해 과학 채널 영상감을 발굴하는 도구.

**수동 시범 운영 모드.** Anthropic API 키 불필요 — 로컬에 설치된 `claude` CLI(Claude Code 구독 인증)를 subprocess로 호출한다. 매일 자동 X. 사용자가 `python radar.py run`을 직접 실행.

- 주 1~2회 권장. 1회당 약 15~25K 토큰, Max 5x 한도의 8~12%.
- 토큰 절약: 필터 강화(자체 점수 상위 100건만 LLM에 전달), 카피 임계 22점 이상.
- `--dry-run` 플래그로 LLM 호출 없이 예상 토큰 확인 가능.

## API 키 입력

키는 대시보드 우측 상단의 ⚙️ 버튼(설정 모달)에서 입력한다.

- **저장 위치**: 브라우저 `localStorage` + 백엔드 메모리. 서버 디스크에는 절대 기록되지 않는다.
- **백엔드 재시작 시**: 메모리의 키는 사라지지만, 브라우저를 다시 열면 localStorage에서 자동으로 복원돼 백엔드에 동기화된다.
- **subprocess 주입**: `radar.py` 실행 시 `YOUTUBE_API_KEY` / `ANTHROPIC_API_KEY` 환경변수로 전달.
- **수동 운영 전제**: 자동 스케줄러로 돌릴 거면 `.env` 파일을 직접 편집해야 한다 (옵션 추후 추가 예정).
- **YouTube 키 테스트**: 모달 안의 "YouTube 키 테스트" 버튼이 트렌딩 1건(약 1유닛)을 직접 호출해 검증한다.
- **YouTube 카드 v2**: 롱폼/숏폼 분리 카운트, 기회 등급 뱃지(황금자리/레드오션/무관심/함정/중간), 롱·숏 Top 3 영상 링크 리스트.
- **Anthropic 키**: 현재는 `claude` CLI를 사용하므로 입력 불필요.

## 증분 모드 (incremental window)

매 실행 시 "마지막 실행 시각 이후 지금까지"를 동적으로 계산해 그 윈도우의 뉴스만 가져온다. 며칠 만에 돌려도 공백이 생기지 않는다.

- **첫 실행**: 최근 72h
- **이후 매 실행**: 마지막 실행 시각 − 2h(안전 마진 오버랩)
- **24h 넘는 윈도우**: 구글 뉴스 RSS `when:Nd` 시간 검색으로 자동 보강 (분야별 키워드 2개, N=간격일수+1, 최대 14d)
- **안전 장치**: 최소 12h, 최대 14일
- **상태 파일**: `data/_radarstate.json` (analyze 성공 직후 `last_run_at` 갱신)

대시보드 헤더에 `마지막 실행: 67h 전 · 다음 윈도우: 67h`로 표시된다. 그냥 ▶ 실행만 누르면 자동 적용됨.

`server.py` 하나(8091, stdlib only)가 대시보드 HTML·결과 JSON 서빙과 "지금 분석 실행 / Dry-run / 후보 승격 / 아카이브" 액션을 모두 처리한다. 다른 서버는 필요 없다.

## 미니 백엔드 (8091, stdlib only)

`server.py`는 `http.server` 기반의 초소형 액션 서버다. Flask 같은 외부 의존성 없음.

| 엔드포인트 | 동작 |
|---|---|
| `GET /api/health` | 상태 체크 |
| `GET /api/status` | 마지막 실행 진행상황 + 로그 tail |
| `POST /api/run` | `radar.py run` 백그라운드 실행 (body `{dry_run: bool}`) |
| `POST /api/promote` | `radar.py promote --id ...` |
| `POST /api/archive` | `radar.py archive --id ...` |
| `GET /api/daily/<date>` | daily JSON 직접 반환 |
| `GET /api/dates` | 사용 가능한 daily 날짜 목록 |

실행 중 또 `/api/run`이 오면 `409 already_running`. 로그는 메모리 deque(200줄) + `data/_lastrun.json`.

### 수동 실행/정지

```bash
# 실행 (기본 127.0.0.1:8091)
.venv\Scripts\python server.py --port 8091     # Windows
.venv/bin/python server.py --port 8091          # Mac/Linux

# 정지
taskkill /F /IM python.exe                      # Windows (전 python 종료 주의)
pkill -f "server.py --port 8091"                # Mac/Linux
```

`open.bat` / `open.sh`는 8091이 안 떠 있으면 백엔드를 자동 기동한 뒤 브라우저를 연다.

## 폴더 구조

```
news-radar/
├── README.md            # 이 파일
├── CLAUDE.md
├── news-radar.html      # 대시보드 (server.py가 서빙)
├── config.json          # 9개 전선 + 5축 점수 룰 + RSS 소스
├── radar.py             # 메인 CLI
├── setup.py             # venv + deps + .env + 일일 schtasks
├── open.bat / open.sh   # 브라우저만 오픈
├── requirements.txt
├── .env.example
├── src/
│   ├── rss.py           # RSS 수집 (feedparser)
│   ├── filter.py        # 키워드/72h/중복 필터 + 자체 점수 상위 100 랭킹
│   ├── llm.py           # claude CLI subprocess 래퍼
│   ├── render.py        # daily.json 빌더
│   └── store.py         # 파일 IO
└── data/
    ├── _raw/            # 일별 원문 jsonl
    ├── _filtered/       # 필터본 jsonl
    ├── daily/           # 분석 결과 json
    ├── candidates.json  # 18점 이상 누적 풀
    └── archived.json    # 영상 제작 완료 항목
```

## 설치

```bash
cd news-radar
python setup.py
# venv 생성 + 의존성 설치 + .env 부트스트랩
# API 키 불필요. claude CLI가 설치되고 로그인돼 있으면 끝.
```

## 사용

### 수동 1회 실행

```bash
# Windows
.venv\Scripts\python radar.py run

# macOS/Linux
.venv/bin/python radar.py run
```

`run`은 `collect → filter → analyze` 3단계를 차례로 실행한다. 각 단계만 따로 돌릴 수도 있다:

```bash
python radar.py collect          # RSS만 수집
python radar.py filter           # 필터 + 자체 점수 상위 100건 랭킹
python radar.py analyze --date 2026-04-07
python radar.py run --dry-run    # LLM 호출 없이 예상 토큰만 확인
```

### 자동 스케줄링

수동 시범 운영 모드로 전환됐으므로 자동 등록은 하지 않는다. 필요하면 직접 터미널에서 실행.

### 대시보드 보기

`server.py`가 대시보드를 직접 서빙한다:

```
http://localhost:8091/news-radar.html
```

`open.bat` 또는 `./open.sh`는 8091이 안 떠 있으면 백엔드를 기동한 뒤 이 URL을 연다.

대시보드에서 [후보로 승격] / [아카이브] 버튼을 누르면 백엔드(`/api/promote`, `/api/archive`)를 직접 호출한다. 같은 동작을 터미널에서 하려면:

```bash
python radar.py promote --id <item_id>
python radar.py archive --id <item_id>
```

## 토큰 추정 (claude CLI 구독 방식)

- 1회 실행, 필터 후 상위 100건 기준
- 분류 배치(80건/배치): 입력 약 20K + 출력 약 8K
- 카피(5~10건 가정): 1건당 약 3K
- **1회 약 15~25K 토큰. Max 5x 한도의 8~12% 수준.**
- 주 1~2회 권장. `--dry-run`으로 호출 전 예상치 확인 가능.

## config.json 수정

- **전선 추가/제거**: `fronts` 배열 편집. id/name/color/keywords_ko/keywords_en/daily_axes 필수.
- **점수 임계 변경**: `scoring.threshold_candidate`(기본 18, 카피 생성 + 후보 마킹) / `threshold_watch`(기본 15, 관찰).
- **RSS 소스 추가**: `sources`에 `{name, url, lang}` 추가. 안 되는 피드는 stderr에 경고만 찍고 넘어감.
- **모델 변경**: `models.classifier` / `models.copywriter`.

## 트러블슈팅

- **`feedparser 미설치`**: `.venv` 활성화 후 `pip install -r requirements.txt` 재실행.
- **`claude CLI를 찾을 수 없습니다`**: Claude Code가 PATH에 있는지, `claude --version`으로 확인.
- **특정 RSS 소스가 0건**: 일시적 차단 또는 URL 변경. config.json에서 해당 소스 주석 처리하거나 교체.
- **포트 8091 충돌**: `python server.py --port 8092`로 변경(대시보드는 `http://localhost:8092/news-radar.html`). `news-radar.html`의 `BACKEND` 상수도 같은 포트로 맞춘다.
- **카피가 한 건도 안 만들어짐**: 임계값(기본 22) 너무 높을 수 있음. config.json의 `threshold_candidate`를 18~20로 낮춰서 재실행.
- **Hook 톤이 어색**: `src/llm.py`의 `COPYWRITER_SYSTEM` 안에 `channels/science/config/pd-guide.md` 핵심 룰이 박혀 있다. 채널 톤이 바뀌면 그 부분을 동기화할 것.
