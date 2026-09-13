# 뉴스 레이더 (news-radar)

자체 백엔드(server.py, 8091)가 정적 파일 + API를 모두 서빙한다. 씬업대(updash 8080)와 완전 독립.
영상감 발굴용 레이더. **사용자 본인 개인용. 공유 X.**

세 개의 독립 파이프라인이 한 대시보드에 붙어 있다.

| 탭 | 파이프라인 | 진입 명령 | 데이터 |
|---|---|---|---|
| 🚨 떡상 | 화이트리스트 채널 조회수 시계열 감시 | `radar.py newsradar` | `data/_newsradar/<cat>/` |
| 📺 YouTube | 트렌딩·키워드 검색 → 전선별 빈 자리 분석 | `radar.py ytrun` | `data/<channel>/youtube/` |
| 📰 RSS | RSS 수집 → 점수 랭킹 → 분류·카피 | `radar.py run` | `data/<channel>/` |

## 채널(channel) 개념

RSS/YouTube 파이프라인은 **채널 단위로 완전히 분리**된다. 채널 = `config/<id>.json` 한 개 + `data/<id>/` 한 벌.

- 현재 채널: `science`(과학 채널, 기본값) / `economy`(경제) / `psychology`(심리)
- 모든 서브커맨드는 `--channel <id>`를 받는다. 생략 시 `science`.
- 경로는 `radar.py`의 `Paths` 클래스가 전담한다. 새 IO는 반드시 이 객체를 거칠 것.
- 서버는 `config/*.json`을 스캔해 채널 목록을 만든다(`GET /api/channels`).
- 실행 락도 **채널별**이라 다른 채널은 동시 실행 가능(같은 채널만 `409 already_running`).
- ⚠️ 루트 `config.json`은 채널 분리 이전의 **잔재**다. 어떤 코드도 읽지 않는다.

## 운영 모드

- **RSS/YouTube는 수동**. 사용자가 `radar.py run` / `ytrun`을 직접 실행한다. 주 1~2회 권장.
- **떡상 감시기만 자동 스케줄 옵션**이 있다. 대시보드 토글 → Windows 작업 스케줄러(`schtasks`)에 `NewsRadarBreakout` 작업 등록(매일 지정 시각, `StartWhenAvailable`).
- Anthropic API 키 불필요. Claude Code 구독 인증을 그대로 재사용(`claude` CLI subprocess 호출).
- 토큰: RSS 1회 15~25K, YouTube 1회 약 30K. 필터 후 자체 점수로 상위 N건만 LLM에 넘기고, 카피는 임계 통과분만.
- `--dry-run` 플래그로 LLM 호출/저장 없이 예상 규모만 확인 가능(`analyze`/`run`/`ytrun`/`newsradar`).

## 파일 구성

```
news-radar/
├── radar.py              # CLI (아래 서브커맨드 전부)
├── server.py             # 미니 백엔드 (8091, 정적 + API, stdlib only)
├── news-radar.html       # 대시보드 단일 파일 (떡상/YouTube/RSS 3탭)
├── autostart.vbs         # 재부팅 시 server.py 자동 기동 (Startup 등록됨)
├── setup.py              # 설치 (venv + deps + .env 부트스트랩)
├── open.bat / open.sh    # 8091 미기동이면 백그라운드 기동 후 브라우저 오픈
├── cleanup_channels.py   # 떡상 등록 채널 생존 점검·정리 (도구)
├── import_finder.py      # 유튜브파인더 백업 JSON → 떡상 채널 등록 (도구)
├── config.json           # ⚠️ 미사용 잔재 (채널 분리 이전)
├── config/
│   ├── science.json      # 채널 설정 (channel/fronts/scoring/ctr_scoring/sources/models/filter)
│   ├── economy.json
│   ├── psychology.json
│   └── _breakout.json    # 떡상 감시기 전용 (defaults + categories[].channels)
├── src/
│   ├── rss.py / filter.py / render.py / store.py
│   ├── llm.py            # claude CLI 래퍼 + .env 로더
│   ├── youtube.py        # YouTube Data API v3 (키 로테이션 포함)
│   ├── ytanalyze.py      # 전선별 빈 자리 분석 프롬프트
│   ├── breakout.py       # 떡상 시계열·속도·등급 판정
│   └── embed.py          # BGE-M3 로컬 임베딩 (RSS↔YouTube 매칭, 선택 의존성)
└── data/
    ├── <channel>/        # _raw, _filtered, daily, youtube/, candidates.json, archived.json, _radarstate.json
    └── _newsradar/       # 떡상 (채널 무관 공용) — <cat>/track.json, <cat>/daily/, _keystate.json
```

## 셋업

1. Python 3.9+ 설치 확인
2. `claude` CLI 설치·로그인 확인 (Claude Code)
3. `python setup.py` 1회 실행 — venv 생성, 의존성 설치, .env 부트스트랩
4. `open.bat` 더블클릭 (Windows) 또는 `./open.sh`
5. 재부팅 자동 실행: `Startup\NewsRadarServer.lnk` → `autostart.vbs` → `server.py --port 8091`

`requirements.txt` = `feedparser`, `python-dateutil`, `sentence-transformers`, `numpy`. 뒤 두 개는 `src/embed.py`(RSS↔YouTube 매칭)용으로 torch까지 딸려와 설치가 수백 MB다. `open.bat`은 이걸 알고 **필수 2개만 먼저 깔고 임베딩 계열은 백그라운드로** 돌린 뒤 바로 대시보드를 연다. 없어도 나머지 기능은 전부 동작한다.

BGE-M3 모델 자체(~2GB)는 `embed`를 처음 호출하는 순간 별도로 내려받는다.

## 포트

- **8091** (stdlib only 미니 백엔드): 정적 파일 + API 모두 서빙
  - 대시보드: `http://localhost:8091/news-radar.html` (또는 `http://localhost:8091/`)
- 씬업대(8080)와 **완전 독립**. 폴더 위치/이름에 가정 없음.
- `radar.py serve`는 8090 단순 정적 서버(개발용 폴백). 평소엔 `server.py`를 쓴다.

### 수동 실행/정지

```bash
.venv\Scripts\python server.py --port 8091     # Windows 실행 (콘솔에 로그 보임)
.venv/bin/python server.py --port 8091          # Mac/Linux 실행

pkill -f "server.py --port 8091"                # Mac/Linux 정지
```

Windows 정지는 `taskkill /F /IM python.exe`로는 **안 잡힌다**. 자동 기동은 `pythonw.exe`로 돌기 때문이다. 게다가 Windows venv 런처 구조상 `pythonw.exe`가 **부모·자식 2개**로 뜨고 포트를 실제로 잡는 건 자식이다. 포트 소유자를 찾아 죽이는 게 확실하다.

```powershell
Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
  Where-Object { $_.CommandLine -like '*server.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

### 부팅 자동 기동 (`autostart.vbs`)

`Startup\NewsRadarServer.lnk` → `wscript autostart.vbs` → `pythonw server.py --port 8091`.

- 중복 기동 가드는 `http://127.0.0.1:8091/api/health`를 **직접 찔러서** 판정한다. 외부 프로세스를 안 띄우므로 1초 안에 끝나고, 포트만 열린 좀비도 걸러진다.
  - ⚠️ 예전 구현은 `powershell -Command Get-NetTCPConnection`을 동기 실행했는데, 부팅 직후 powershell 콜드 스타트가 수십 초 걸리는 데다 exit 1이 나오면 "이미 떠 있음"으로 오판해 **서버를 아예 안 띄우고 조용히 종료**했다. 되돌리지 말 것.
- 기동 후 최대 40초까지 헬스체크하고 결과를 `data/_autostart.log`에 남긴다. 자동 기동이 실패했을 때:
  - **로그에 그 부팅 시각 줄이 아예 없다** → vbs가 실행되지 않았다(Startup 항목·작업관리자 사용 안 함·빠른 시작 문제). 작업 스케줄러 로그온 트리거로 이중화할 것.
  - **`health FAILED` 줄이 있다** → vbs는 돌았고 서버가 안 떴다. `python server.py`를 콘솔로 띄워 예외를 볼 것.

## API 키

대시보드 우측 상단 ⚙️ 모달에서 입력. 키는 브라우저 `localStorage`(`newsradar_api_keys`) + 백엔드 **메모리**에만 보관, 서버 디스크에는 기록 X. 백엔드 재시작 시 키는 사라지지만 브라우저에서 자동 복원. `radar.py` 실행 시 환경변수로 주입.

**YouTube 키는 여러 개 등록해 순차 소진**할 수 있다.

- 인식 순서: `YOUTUBE_API_KEYS`(쉼표 구분) → `YOUTUBE_API_KEY`, `YOUTUBE_API_KEY_2`, `YOUTUBE_API_KEY_3`, …
- 429/403(쿼터 소진) 또는 400(무효 키)이면 자동으로 다음 키 폴백.
- 소진된 키는 `data/_newsradar/_keystate.json`에 기록되고, 서버가 읽어 대시보드에 표시(`GET /api/keys/usage`). UTC 날짜가 바뀌면 초기화.
- 자동 스케줄러(schtasks)로 도는 떡상 감시기는 브라우저를 안 거치므로 `.env` 직접 편집이 필요하다.

## 서브커맨드

```
collect / filter / analyze / run     RSS 파이프라인 (--channel --date [--dry-run])
ytrun                                YouTube 레이더 1회 (--channel [--dry-run])
ytmatch                              기존 yt daily에 RSS 임베딩 매칭 부착 (--date --days)
newsradar                            떡상 감시기 (--category 없으면 전체 순회, [--dry-run])
breakout-refs                        떡상 영상 토픽으로 레퍼런스 검색 (--video-id, 저장 없이 JSON)
resolve-channel                      채널 URL/@핸들/검색어 → channel_id (JSON 출력, server가 호출)
promote / archive / unpromote / unarchive    후보·제외 풀 이동 (--id)
cand-search / cand-save-refs / cand-start    후보 → 레퍼런스 → 프로젝트 생성
serve                                단순 정적 서버 (8090, 폴백)
```

## RSS 파이프라인 (`run`)

수집 → 필터 → 자체 점수 랭킹 → claude CLI 분류·점수화 → 임계 통과분만 카피 생성 → `data/<ch>/daily/{date}.json`.

- `scoring.threshold_candidate` / `threshold_watch`로 후보·관찰 구분.
- `scoring.candidate_quota_per_front`(0=무제한)로 **전선별 카피 쿼터**를 건다. 한 전선이 카피 예산을 독식하는 걸 막는 장치.
- `ctr_scoring` 8축은 카피 생성 프롬프트에 함께 넘어간다.

### 증분 모드 (incremental window)

매 실행 시 "마지막 실행 시각 이후 지금까지"를 동적으로 계산해 그 윈도우의 뉴스만 가져온다.

- 첫 실행 최근 72h → 이후 마지막 실행 − 2h(안전 마진 오버랩)
- 24h 넘는 간격이면 구글 뉴스 시간 검색(`when:Nd`)으로 자동 보강 (분야별 키워드 2개)
- 안전 장치: 최소 12h / 최대 14일
- 상태 파일: `data/<channel>/_radarstate.json` (`run` 성공 후 `last_run_at` 갱신)
- `compute_window()`는 `cmd_collect`/`cmd_filter`/`/api/status`에서 멱등하게 재계산

## YouTube 레이더 (`ytrun`)

트렌딩 2카테고리 + 분야별 키워드(분야당 2개) × 7일 검색 → 통계 보강 → 중복 제거 → 전선별 그룹 → claude CLI 빈 자리 분석.

- **쿼터**: 1회 약 1,500~2,000 유닛 (10K 한도의 15~20%). 검색 100유닛/회, 트렌딩·통계 1유닛/회.
- **데이터**: `data/<ch>/youtube/daily/{date}.json`, raw는 `data/<ch>/youtube/_raw/{date}.jsonl`.
- **카드 v2**: 롱폼(>180초) / 숏폼(≤180초) 분리, 빈 자리 분석은 롱폼 기준. 기회 등급 뱃지(gold/red/gray/trap/mixed) + 롱·숏 Top 3 링크.
- **RSS 연결**: 분석 결과에 최근 3일치 RSS 기사를 BGE-M3 임베딩 코사인 유사도로 top 5 부착(`src/embed.py`). `ytrun` 안에서 자동 수행되고, 사후 부착은 `ytmatch`.
- RSS와 **같은 채널 락**을 공유한다(동시 실행 불가).

## 떡상 감시기 (`newsradar`)

화이트리스트 채널의 업로드를 훑어 **video_id별 조회수 시계열**을 쌓고, 속도로 등급을 매긴다. 채널(science/economy/…) 개념과 무관한 공용 트랙이다.

- **설정**: `config/_breakout.json` — `defaults` + `categories[]`(분류별 `breakout` 값이 우선 병합).
  - 분류: media(언론사) / economy / psychology / history / science / yadam(야담)
  - 분류별 `auto_collect: false`면 전체 순회(자동)에서 제외되고 수동 지정 실행만 받는다.
- **저장**: `data/_newsradar/<cat>/track.json`(시계열), `<cat>/daily/{date}.json`(리더보드), `<cat>/daily/index.json`.
- **등급 판정** (`src/breakout.py`): 절대 조회수가 아니라 **하루 증가량(raw speed)** 기준.
  - 🔥 `hot` — raw ≥ `score_base` (하루 10만+ 증가)
  - 📈 `rising` — raw ≥ `score_base` × 0.3
  - 🧊 `base` — 그 미만 (식었거나 미미)
  - ⏳ `watch` — 추적 풀엔 있으나 `track_threshold` 미달 (승급 대기)
  - 숏폼(`min_long_sec` 이하)은 메인 리더보드에서 제외.
- **속도 계산의 두 가지 보정** — 건드릴 때 주의:
  1. 측정점마다 실제 시각(`at`)을 남기고 경과시간으로 나눠 24h 환산한다. `date`만 쓰면 7시간 증가분이 '하루치'로 둔갑해 급상승이 base로 묻힌다. 촘촘한 재감지 폭주 방지로 간격 하한 2시간.
  2. 업로드 3일 이내(`NEW_WINDOW_DAYS`) 영상은 `delta`와 추정속도(총조회수÷나이) 중 **큰 값**을 쓴다. 업로드 직후 폭발이 첫 측정 이전에 끝나면 delta에 안 잡혀 곧장 base로 강등되기 때문. 오래된 영상은 추정속도가 과거 실적까지 평균내 거품이 되므로 delta만 쓴다.
- **효율(조회수÷구독자)**을 함께 계산한다. 속도지수(절대량)만 보면 대형채널 일상 조회가 위로 올라와 '채널 힘'과 '콘텐츠 힘'이 안 갈린다. 구독자수 보강은 50채널당 1유닛이라 비용 미미.

### 채널 목록 관리

- `python cleanup_channels.py` — 등록 채널 생존 점검. `dead`(삭제·정지) / `empty`(영상 0개) / `renamed`(채널명 변경) 분류. **기본은 조회만**, `--fix-names` `--remove-dead` `--remove-empty` `--remove <id…>`로 반영(`.bak` 자동 백업). 50채널당 1유닛.
  - 주제를 갈아탄 채널(야담→쇼핑 등)은 API로 판별 불가 → renamed 목록을 사람이 보고 `--remove`로 지정하는 설계.
- `python import_finder.py 백업.json --folder <파인더폴더> --category <분류id>` — 유튜브파인더가 채널을 localStorage에 두므로 백업 JSON 경유로만 옮길 수 있다. `--list`로 폴더·분류 목록 확인, `--dry-run` 지원.
- 대시보드에서도 분류·채널 추가/삭제 가능(`POST /api/newsradar/category`, `/channel`). 채널 추가 시 `radar.py resolve-channel`로 URL·핸들·검색어를 channel_id로 해석한다.

## 후보 파이프라인

RSS/YouTube 카드에서 마음에 든 항목을 골라 실제 제작으로 넘기는 경로.

```
카드 → promote → candidates.json
     → cand-search   (제목을 검색 키워드로 축약해 유튜브에서 같은 주제 영상 검색)
     → cand-save-refs (사용자가 고른 레퍼런스 video_id 저장)
     → cand-start    (프로젝트 폴더 생성 + scripts/collect.py 실행)
```

- 후보 풀 `data/<ch>/candidates.json`, 제외 풀 `data/<ch>/archived.json`. 되돌리기는 `unpromote`/`unarchive`.
- YouTube 항목 id 규약: `yt:{date}:{front_id}` — yt daily의 `analyses[].front_id`로 매칭.
- `cand-search`/`breakout-refs`는 제목에서 언론사명·서술어를 떼고 35자로 축약해 검색한다. 롱폼(>60초) + 조회수 1000+ 만 남기고 조회수 내림차순.
- ⚠️ `cand-start`는 **repo 루트(`ROOT.parent`) 기준으로 바깥을 건드린다**: `../channels/<channel>/projects/NN_mmdd_키워드/`를 만들고 `../scripts/collect.py`를 300초 타임아웃으로 실행한다. news-radar 폴더만 떼어 옮기면 이 명령은 동작하지 않는다.

## 미니 백엔드 (server.py)

stdlib only (`http.server`), Flask 등 추가 의존성 없음. CORS 전부 허용(개인용). 실행 로그는 메모리 deque(200줄) + `data/<ch>/_lastrun.json`.

```
GET   /api/health, /api/status, /api/channels
GET   /api/keys, /api/keys/usage
GET   /api/dates, /api/daily/<date>
GET   /api/youtube/dates, /api/youtube/<date>
GET   /api/newsradar/categories, /api/newsradar/schedule
GET   /api/newsradar/<cat>/channels | /latest | /dates | /date/<d>
POST  /api/keys, /api/keys/test          DELETE /api/keys
POST  /api/run, /api/ytrun, /api/newsradar
POST  /api/newsradar/schedule            (schtasks 토글)
POST  /api/newsradar/category, /api/newsradar/channel   (분류·채널 CRUD)
POST  /api/promote, /api/archive, /api/unpromote, /api/unarchive
POST  /api/cand-search, /api/cand-save-refs, /api/cand-start
```

- 대부분의 GET은 `?channel=<id>`를 받는다. 유효하지 않으면 `science`로 폴백.
- 떡상 API만 채널 파라미터가 없다(공용 트랙).
- 경로 세그먼트(분류 id·날짜)는 `_valid_seg`로 `..`, 슬래시를 막는다.

## 알려진 문제

- `radar.py breakout-refs`가 `data/_newsradar/track.json`(flat)을 읽는데, 현재 `newsradar`는 `data/_newsradar/<cat>/track.json`에만 쓴다. 분류 도입 이전 경로가 남은 것으로, 이 명령은 지금 항상 "track에 video_id 없음"으로 끝난다.
- `server.py`의 `_list_channels()`가 `config/*.json`을 필터 없이 훑어서, 떡상 전용 설정인 `_breakout.json`이 **채널 목록에 `_breakout`이라는 채널로 섞여 나온다**(`GET /api/channels`). `_` 접두사 제외가 필요하다.
- `README.md`는 채널 분리·떡상 감시기 이전 내용이라 낡았다.
