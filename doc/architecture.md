# AI VOC 선제 분석 시스템 구현 방향

본 시스템은 VOC(고객 문의/장애 제보)가 발생했을 때,
개발자가 로그 분석 도구에 직접 접근하기 전에
AI가 선제적으로 로그를 수집하고 원인 후보를 제시하는 것을 목표로 한다.

---

## 1. 사용 기술

| 분류 | 기술 / 라이브러리 | 용도 |
|------|------------------|------|
| API 서버 | `fastapi` + `uvicorn` | 비동기 API 서버 |
| Agent 오케스트레이션 | `langgraph` | 상태 기반 멀티턴 에이전트 그래프 |
| LLM (기본) | `ollama` + `qwen2.5:14b` | 로컬 LLM 실행. 외부 API 미사용으로 보안 이슈 없음 |
| LLM (선택) | `anthropic` Claude API | `LLM_PROVIDER=claude` 환경변수로 전환. `langchain-anthropic` 사용 |
| LLM 연동 | `langchain-openai` / `langchain-anthropic` | `LLM_PROVIDER` 값에 따라 llm_factory에서 동적 분기 |
| 스키마 / 상태 정의 | `pydantic` | state, Tool 입출력 타입 정의 |
| 로그 분석 | `splunk-sdk` | Splunk 공식 Python SDK (asyncio.run_in_executor로 비동기 처리) |
| 세션 상태 저장 | `langgraph` SqliteSaver | 대화 세션 간 state 영속성 (LangGraph 내장) |
| 환경변수 관리 | `python-dotenv` | API key, LLM_PROVIDER 등 설정 관리 |

---

## 2. Splunk 검색 규칙

### 2-1. 환경별 eventtype

| 환경 | eventtype |
|------|-----------|
| PRD (운영) | `eventtype="kube:prd"` |
| STP (스테이징) | `eventtype="kube:stp"` |
| DEV (개발) | `eventtype="kube:dev"` |

### 2-2. 도메인별 sourcetype

대부분의 도메인은 `kube:container:{도메인소문자}-fleta` 패턴을 따른다.

### 2-3. 로그 포맷

Spring Boot + Spring Cloud Sleuth 기반 로그 포맷을 따른다.

```
{timestamp} {level} [{appName},{traceId},{spanId}][][] {pid} --- [{thread}] {logger} : {message}
```

**실제 예시:**
```
2026-04-13 22:45:43.296 ERROR [saturn,7d503e79fc35ef6caacae22fce0a99f2,72a018d291425e8b][][] 1 --- [io-8080-exec-86] c.l.f.p.a.e.InnerControllerAdvice : [globalExceptionHandler] th
com.lguplus.fleta.domain.exception.database.DataNotExistsException: 해당 가입자 좌표 정보 미존재
    at com.lguplus.fleta.domain.service.MemberGeoDomainService.lambda$getMemberLocation$0 ...
```

파싱 대상 필드:

| 필드 | 설명 |
|------|------|
| `timestamp` | 로그 발생 시각 |
| `level` | 로그 레벨 (ERROR / WARN / DEBUG) |
| `traceId` | Sleuth trace ID. 동일 요청의 연관 로그 추적에 사용 |
| `logger` | 예외를 처리한 클래스 |
| `exception_class` | 발생한 예외 클래스 전체 경로 |
| `exception_message` | 예외 메시지 (한국어) |

### 2-4. 예외 분류 기준

`InnerControllerAdvice`가 모든 예외를 처리하며 `errors.yml` 기반으로 에러코드를 매핑한다.

| 에러코드 범위 | 분류 | 예시 예외 | 심각도 |
|-------------|------|---------|--------|
| 5000 ~ 5017 | 파라미터 검증 오류 (클라이언트) | ParameterMissingException | 낮음 |
| 1400 | 파라미터 오류 | ParameterInvalidException | 낮음 |
| 8000 ~ 8999 | DB 비즈니스 오류 | DataNotExistsException | 중간 |
| 6022 ~ 6023 | 외부 서비스 연동 오류 | ExternalServiceException | 중간 |
| **9999** | **미정의 시스템 오류** | **그 외 모든 예외** | **높음** |

* 에러코드 **9999는 진짜 시스템 오류**로 분류하며 LLM 분석의 최우선 대상으로 한다
* 5000번대는 클라이언트 잘못이므로 분석 우선순위에서 제외 가능
* `traceId`를 활용하면 동일 요청에서 발생한 연관 로그를 묶어서 분석할 수 있다

### 2-5. SPL 쿼리 형태

eventtype + sourcetype + 시간 범위 + ERROR 레벨 필터를 조합하여 조회한다.

```spl
search eventtype="kube:prd" sourcetype="kube:container:subscriber-fleta"
earliest="04/14/2026:14:00:00" latest="04/14/2026:14:30:00"
| search " ERROR "
| rex "(?<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+) (?<level>\w+) \[(?<app>[^,]+),(?<traceId>[^,]*),(?<spanId>[^\]]*)\]"
| rex "(?<exception_class>com\.lguplus\.\S+Exception): (?<exception_message>[^\n]+)"
| stats count by exception_class, exception_message
| sort -count
```

* 시간 범위는 `time_range` state 값을 기반으로 동적 생성한다
* 사용자가 시간을 모르는 경우 `earliest=-30m latest=now` 를 기본값으로 사용한다
* 동일 `traceId` 기준으로 로그를 묶어 연관 분석에 활용한다

---

## 3. MSA 도메인 목록

LLM은 VOC 내용과 각 도메인의 설명을 대조하여 분석 대상 도메인을 추론한다.

| 도메인 | sourcetype | 설명 |
|--------|-----------|------|
| Multiverse | `kube:container:multiverse-fleta` | 빅데이터에서 제공하는 개인화 추천 정보를 DB에 적재하는 기능 제공 |
| Sokuri | `kube:container:sokuri-fleta` | 전단말 공통 실시간 랭킹 정보 제공 |
| Curation | `kube:container:curation-fleta` | 단말에서 사용자 추천 정보 조회하는 API 제공 |
| Terms | `kube:container:terms-fleta` | 서비스 약관 정보 및 동의 기능 제공 |
| VodLookup | `kube:container:vodlookup-fleta` | VOD 편성·컨텐츠 정보를 제공하기 위한 리드모델 관리 |
| Contents | `kube:container:contents-fleta` | 화제동영상, 배너, 광고 등 VOD 확장 컨텐츠 및 비 VOD 컨텐츠 정보 제공 |
| Programming | `kube:container:programming-fleta` | 컨텐츠 편성 정보 제공 |
| Product | `kube:container:product-fleta` | VOD 상품 관련 정보를 단말에 제공 |
| Channel | `kube:container:channel-fleta` | 실시간 채널 정보를 관리하고 단말에 제공 |
| Subscriber | `kube:container:subscriber-fleta` | 가입자 정보, 쿠폰, 가입 상품 관리 및 단말 제공. 개인화 프로필 관리 기능 포함 |
| Personalization | `kube:container:personalization-fleta` | 찜, 평가, 플레이리스트 등 개인 선택 정보 제공 |
| SearchWord | `kube:container:searchword-fleta` | 검색 기능 제공 |
| Payment | `kube:container:payment-fleta` | 청구서 결제, 휴대폰 결제 등 각종 결제 기능. 포인트 및 멤버십 할인 제공 |
| Coupon | `kube:container:coupon-fleta` | 쿠폰 및 스탬프 발급·사용 기능 제공 |
| Notice | `kube:container:notice-fleta` | 공지, 이벤트 등의 알림 기능 제공 |
| StorytellingSeniorTV | `kube:container:storytellingsenior-fleta` | 스토리텔링 및 시니어TV 서비스 기능 제공 |
| Settings | `kube:container:settings-fleta` | 단말에서 사용되는 각종 설정 정보 제공 |
| Notify | `kube:container:notify-fleta` | SMS, MMS, 푸시 등 메시징 기능 제공 |
| Madecassol | `kube:container:madecassol-fleta` | 스스로 해결 가이드 서비스 |
| Bouncer | `kube:container:bouncer-fleta` | 사용자 정보 인증 서비스 |
| Watpur | `kube:container:watpur-fleta` | 신속한 컨텐츠 탐색을 위한 모바일 웹 서비스 (TV모아) |
| Crepas | `kube:container:crepas-fleta` | BPAS-ADMIN 리팩토링 서비스 |
| Pinkpas | `kube:container:pinkpas-fleta` | BPAS-ADMIN 상품 관련 Inner API 서비스 |
| Scanner | `kube:container:scanner-fleta` | 콘텐츠 탐색 기능을 단말에 제공 |
| YouQuiz | `kube:container:youquiz-fleta` | uGPT 활용 리뷰 평점 제공 서비스 |
| Saturn | `kube:container:saturn-fleta` | MIMAS용 common API 및 스케줄 서비스 |
| Orbit | `kube:container:orbit-fleta` | YouQuiz의 동기/비동기 호출 지원 및 로직 처리 서비스 |
| Contents360 | `kube:container:contents360-fleta` | 페르소나 평점·리뷰 제공 서비스 |
| MIMAS | `kube:container:mimas-fleta` | |
| BPAS | `kube:container:bpas-fleta` | |
| MeCS | `kube:container:mecs-fleta` | |
| Assimilator | `kube:container:assimilator-fleta` | |
| Pacman | `kube:container:pacman-fleta` | |

> **NOTE:** MIMAS, BPAS, MeCS, Assimilator, Pacman 서비스 설명 및 일부 sourcetype은 실제 운영 환경 기준으로 보완 필요

---

## 4. 환경변수 목록

`.env` 파일에 정의하며 `python-dotenv`로 로드한다.

```dotenv
# LLM 설정
LLM_PROVIDER=ollama          # ollama 또는 claude

# Ollama 설정 (LLM_PROVIDER=ollama 일 때)
OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_MODEL=qwen2.5:14b

# Anthropic 설정 (LLM_PROVIDER=claude 일 때)
ANTHROPIC_API_KEY=
CLAUDE_MODEL=claude-sonnet-4-6

# Splunk 설정
SPLUNK_HOST=lguplus-iptv.splunkcloud.com
SPLUNK_PORT=8089
SPLUNK_TOKEN=   # DevOps 팀에 관리자 권한 또는 토큰 발급 요청 필요
```

---

## 5. 멀티턴 질문 전략

로그 조회 전, LLM은 세 가지 정보를 반드시 확정해야 한다.
하나라도 미확정이면 Tool 호출 전에 해당 질문을 먼저 수행한다.

### 4-1. 환경 확정

**질문 방식:** PRD를 기본값으로 제안하고 확인을 받는다.

> "PRD(운영) 환경 기준으로 조회할게요, 맞나요?
> 아니라면 STP(스테이징) 또는 DEV(개발) 중 선택해 주세요."

### 4-2. 오류 발생 시간 확정

**질문 방식:** 사용자 입력에 시간 정보가 없는 경우 질문한다.

> "언제쯤 발생했나요? (예: 오늘 오후 2시경, 30분 전 등)"

* 정확한 시간을 모르는 경우 전후 30분 범위를 기본값으로 사용한다
* 확정된 시간 범위는 state에 저장하여 이후 재조회 시 재사용한다

### 4-3. 도메인 확정

**질문 방식:** VOC 내용으로 도메인을 특정하기 어려운 경우, 후보 3개를 제시한다.

> "어떤 서비스 쪽 문제인지 확인이 필요합니다. 아래 중 해당하는 항목이 있나요?
> - **Programming** : 컨텐츠 편성 정보 제공
> - **Contents** : 화제동영상, 배너 등 VOD 컨텐츠 정보 제공
> - **VodLookup** : VOD 편성·컨텐츠 정보를 단말에 제공
> - 직접 입력 (예: Subscriber, Payment ...)"

* 후보는 VOC 내용과 도메인 설명의 유사도를 기준으로 선별한다
* 사용자가 도메인명을 직접 입력한 경우, 도메인 목록과 대조하여 일치하는 항목으로 확정한다
* 직접 입력값이 목록에 없는 경우 사용자에게 다시 안내한다
* VOC 최초 입력 시 도메인명을 명시한 경우 (예: "subscriber 로그인 오류") 이 단계를 건너뛰고 바로 확정한다

---

## 6. 주요 처리 흐름

1. 사용자가 VOC를 입력한다 (예: "편성 정보가 안 나와요")
2. LLM이 문제 상황을 해석한다
3. **환경 확정** — PRD 기본값으로 제안하고 확인받는다 (4-1 참고)
4. **시간 확정** — 시간 정보가 없으면 질문한다 (4-2 참고)
5. **도메인 확정** — VOC에 도메인명이 명시된 경우 바로 확정, 불명확하면 후보 3개 또는 직접 입력을 제시한다 (4-3 참고)
6. 환경 + 도메인 기반으로 eventtype / sourcetype을 조합하여 Splunk 조회 Tool을 호출한다
7. Tool은 지정된 시간 범위 내 로그를 조회하여 반환한다
8. 조회 결과가 0건이면 사용자에게 시간 범위 조정을 제안한다
9. 로그를 파싱 및 요약하여 에러 유형 및 발생 빈도를 구조화한다
10. LLM은 요약 정보를 기반으로 원인 후보를 추론하고 설명한다
11. 정보가 부족하면 추가 질문 후 재조회한다 (멀티턴 반복)
12. 최종적으로 원인 후보와 근거를 함께 제공한다

---

## 7. LangGraph 노드 구성

```
[START]
   ↓
[llm_node] ── VOC 해석 및 다음 액션 판단
   ↓ (조건 분기)
   ├─ env 미확정    → [ask_env_node]
   ├─ time 미확정   → [ask_time_node]
   ├─ domain 미확정 → [ask_domain_node]
   └─ 모두 확정     → [splunk_node] ── Splunk 조회 (run_in_executor)
                           ↓
                     [analyze_node] ── 로그 파싱·요약 후 LLM 원인 추론
                           ↓
                         [END]
```

각 `ask_*_node`는 사용자에게 질문을 반환하고 다음 입력을 대기한다.
사용자 답변이 들어오면 다시 `llm_node`로 진입하여 state를 업데이트한다.

---

## 8. API 엔드포인트

| 메서드 | 경로 | 설명 |
|--------|------|------|
| `POST` | `/chat/start` | 새 세션 시작. thread_id 발급 및 첫 VOC 입력 |
| `POST` | `/chat/{thread_id}` | 멀티턴 메시지 전송 |
| `POST` | `/webhook/teams` | Microsoft Teams 봇 연동용 웹훅 (확장 시 사용) |

### 요청 / 응답 구조

**POST `/chat/start`**

```json
// 요청
{ "message": "로그인이 안됨" }

// 응답
{
  "thread_id": "abc123",
  "reply": "PRD(운영) 환경 기준으로 조회할게요, 맞나요?\n아니라면 STP(스테이징) 또는 DEV(개발) 중 선택해 주세요."
}
```

**POST `/chat/{thread_id}`**

```json
// 요청
{ "message": "네 PRD 맞아요" }

// 응답
{
  "thread_id": "abc123",
  "reply": "언제쯤 발생했나요? (예: 오늘 오후 2시경, 30분 전 등)"
}
```

**최종 분석 결과 응답**

```json
{
  "thread_id": "abc123",
  "reply": "분석 결과를 안내드립니다.\n\n[추정 원인]\n...\n\n[근거 로그 요약]\n..."
}
```

---

## 9. 핵심 설계 원칙

* 로그 조회 및 데이터 처리는 Tool이 수행하고, LLM은 판단 및 해석 역할만 수행한다
* Splunk 결과는 코드에서 에러 로그 필터링·빈도 집계 후 요약본만 LLM에 전달한다 (토큰 절약)
* 도메인 목록은 `domains.yaml`로 관리하여 코드 배포 없이 추가·수정 가능하게 한다
* 모든 분석 결과는 "추정" 기반으로 표현하며, 근거 데이터를 함께 제공한다
* 멀티턴 구조를 통해 부족한 정보를 단계적으로 보완한다

### domains.yaml 구조

```yaml
domains:
  - name: Subscriber
    sourcetype: kube:container:subscriber-fleta
    description: 가입자 정보, 쿠폰, 가입 상품 관리 및 단말 제공. 개인화 프로필 관리 기능 포함

  - name: Payment
    sourcetype: kube:container:payment-fleta
    description: 청구서 결제, 휴대폰 결제 등 각종 결제 기능. 포인트 및 멤버십 할인 제공

  - name: Bouncer
    sourcetype: kube:container:bouncer-fleta
    description: 사용자 정보 인증 서비스
```

---

## 10. 상태 관리

멀티턴 처리를 위해 요청 간 상태(state)를 LangGraph SqliteSaver로 저장한다.
thread_id를 키로 세션을 구분하며, 별도 인프라 없이 파일 기반으로 동작한다.

| 필드 | 설명 |
|------|------|
| `thread_id` | LangGraph 세션 식별자. SqliteSaver의 저장 키로 사용 |
| `messages` | 대화 히스토리. LLM 맥락 유지에 사용 |
| `env` | 환경 정보 (PRD / STP / DEV) |
| `env_confirmed` | 환경 확정 여부 (boolean) |
| `domain` | 확정된 도메인명 |
| `domain_confirmed` | 도메인 확정 여부 (boolean) |
| `time_range` | Splunk 조회용 시간 범위 (start / end) |
| `time_confirmed` | 시간 범위 확정 여부 (boolean) |
| `user_id` | 사용자 식별 정보 (선택) |
| `log_summary` | 파싱·요약된 로그 결과 |

`env_confirmed`, `time_confirmed`, `domain_confirmed` 세 값이 모두 `true`일 때만 Splunk 조회 Tool을 호출한다.

---

## 11. 확장 방향

* 과거 장애 이력을 활용한 RAG 기반 분석
* Microsoft Teams 봇 연동
* 자동 대응 (재시작, 롤백 등) 기능 추가

---

## 핵심 요약

> **LLM이 판단하고 Tool이 실행하며,
> 멀티턴을 통해 정보를 보완하는 구조로
> VOC 기반 로그 분석을 자동화한다**
