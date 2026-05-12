# AI VOC 선제 분석 — 개발 회고

---

## 0. 한 줄 정리

VOC 자연어 → 슬롯(환경·시간·도메인) 멀티턴 확정 → Splunk SPL 조회 → 예외 빈도 요약 → LLM이 추정 원인 작성.

---

## 1. 전체 구조 — 왜 이렇게 됐나

### 1-1. LangGraph를 선택한 이유

처음에는 단순 `if/else` 라우터로 슬롯을 채울까 고민했다.
LangGraph로 간 이유는 세 가지였다.

1. **세션 상태 영속화**가 공짜로 따라옴 (`AsyncSqliteSaver`).
   사내 인프라 의존성 없이 SQLite 파일 하나로 멀티턴이 유지된다 — POC 단계에서 큰 이점.
2. **조건 분기를 노드 단위로 명시**할 수 있어서, "env 미확정이면 ask_env로" 같은 룰을 그래프 정의 한 곳(`app/graph/workflow.py:32`)에서 본다.
3. 나중에 Splunk 외에 다른 Tool (예: traceId 추적, 과거 사례 RAG)을 추가할 때 노드 하나 더 붙이는 형태로 확장 가능.

### 1-2. Slot filling은 LLM이 아니라 코드가 강제한다

`nodes.py:90` 의 `_merge_env_strict` 가 그 결과물이다.

원래는 LLM이 내보낸 `env_confirmed` 를 그대로 믿었다.
그러나 Ollama 백엔드에서 `with_structured_output`이 환경을 추측만으로 `env_confirmed=true`로 채워 넣는 경우가 반복됐고 (사용자가 환경에 대해 한마디도 안 했는데도),
그 결과 사용자가 "PRD가 맞나요?" 라는 질문을 한 번도 못 보고 바로 Splunk를 치는 일이 발생했다.

해결은 단순했다.
- LLM이 내는 `env_confirmed` 는 **읽지 않는다**.
- 사용자 마지막 발화를 직접 보고 (`_parse_explicit_env`, `_is_affirmative_env_reply`) 코드에서 confirm 한다.
- 단, env 값 자체는 LLM이 더 잘 뽑는 경우가 있으므로 (예: "스테이징에서요" → STP) **값은 받되 confirm 플래그만 코드가 결정**.

이 패턴은 다음과 같은 일반화 가능하다:

> **LLM 출력의 "값" 과 "값이 확정인가" 는 분리해서 다루는 게 안전하다.
> 값은 모델에게 시키고, 확정 여부는 외부 신호(사용자 답변, 검증 통과 여부)로 결정한다.**

domain·time 슬롯도 같은 패턴이 필요한데, 현재는 LLM 신뢰도가 그쪽은 그나마 괜찮아서 (도메인 목록은 폐쇄 집합) 우선 env 만 강제하고 있다 — 차후 같은 사고가 나면 동일하게 분리할 것.

### 1-3. Tool 결과를 통째로 LLM에 안 던진다

Splunk 결과는 행 수가 수천 건이 될 수 있다.
`app/tools/splunk_search.py:14` 의 SPL은 `stats count by exception_class, exception_message | head 50` 으로
**LLM이 보기 전에 빈도 집계 + 상위 50개로 압축**한다.

이유:
- 토큰 비용 (특히 Claude 백엔드일 때).
- LLM이 원본 로그 수천 줄을 보면 환각이 늘어난다 — "있어 보이는" exception을 만들어 답에 섞는다.
- 사람이 봐도 "어떤 예외가 몇 번"이 가장 먼저 알고 싶은 정보이므로, 그 형태로 정제해서 주는 게 자연스럽다.

원칙:

> **Tool은 raw → 정제까지 책임지고, LLM에는 의사결정에 필요한 최소 정보만 넘긴다.**

---

## 2. 외부 의존성 — Splunk 토큰 발급

가장 오래 걸린 부분. 코드와 무관한 일이라서 회고에 남길 가치가 가장 큼.

### 2-1. 무엇이 필요했나

`splunk-sdk` 가 Splunk Cloud에 붙으려면 두 가지 중 하나가 필요하다.

- (a) ID/PW 로그인 — 사람 계정. 자동화에는 부적합 (MFA, 만료, 감사 문제).
- (b) **REST API 토큰** — 서비스 계정 + 권한 범위 명시. 운영용은 이게 정답.

우리가 요청한 것은 (b).
필요한 권한 범위:
- 대상 인덱스: <TODO: 인덱스명 — 예: `main` 또는 도메인 전용 인덱스>
- 필요한 sourcetype: `kube:container:*-fleta` (33개 도메인 전체)
- 필요한 eventtype: `kube:prd`, `kube:stp`, `kube:dev`
- 동작: **읽기 only**. 쓰기·관리 권한은 필요 없음을 명시.

### 2-2. 요청 흐름 (재현용 메모)

1. <TODO: 채널 — Jira `XXX-PROJECT` / Teams DevOps 채널 / 이메일> 로 요청.
2. 요청 양식에 들어가야 했던 것:
   - 사용 목적 ("VOC 1차 분류 자동화 POC")
   - 조회 범위 (위 sourcetype·eventtype 목록)
   - 토큰 만료일 (<TODO: 우리가 제안한 기간 — 예: 90일 / 1년>)
   - 토큰을 어디에 저장할지 (현재는 `.env`, 사내 secret manager 미연동)
   - 보안팀 추가 승인 필요 여부 (<TODO: 우리 케이스에서는 필요했나/아니였나>)
3. 소요: <TODO: 영업일 N일>.
4. 받은 토큰은 `.env` 의 `SPLUNK_TOKEN` 에 들어간다 (`app/config.py:28`).

### 2-3. 차후 비슷한 프로젝트 시작하는 사람에게

- **첫 주에 토큰부터 신청해라.** 코드보다 사람이 느리다. 이걸 늦게 시작하면 코드는 다 됐는데 데모를 못 한다.
- **읽기 권한만 요청해라.** 쓰기까지 요청하면 승인 단계가 한 단계 더 길어진다.
- **만료일을 짧게 시작해라.** 어차피 운영 단계 가면 사내 secret manager 연동 + 회전 정책이 필요해진다. 짧은 만료가 그 강제 트리거 역할을 함.
- **사용 목적 한 줄로 설명할 수 있게 미리 준비.** 보안팀이 가장 먼저 묻는다.

---

## 3. 에러 메시지 그대로 검색해서 풀었던 사례

### 3-1. `with_structured_output` 가 Ollama에서 스키마를 무시

증상: `LlmStatePatch` 스키마를 줬는데 모델이 자유 텍스트로 답해서 Pydantic 검증 단계에서 깨짐.
검색어: 그냥 LangChain이 던진 `OutputParserException: ... could not parse ...` 메시지를 통째로.
해결: 모델별로 `with_structured_output` 의 method (function_calling / json_mode / json_schema) 가 달라서, qwen2.5는 json_mode가 더 안정적이라는 GitHub issue를 참고. + 위(1-2)에서 적은 대로 LLM 출력의 confirm 플래그를 그대로 안 믿는 코드 보정도 함께 들어감.

### 3-2. `JSONResultsReader` 가 dict / Message 객체를 섞어서 내보냄

증상: 모든 row를 dict로 가정하고 처리했더니 어떤 검색에서는 `AttributeError`.
해결: splunk-sdk 깃허브 이슈에서 "result row는 dict, 그 외에는 Message 객체 (`.type`, `.message`)" 라는 동작을 확인.
지금 `splunk_search.py:51` 의 분기가 그 결과 — dict면 row로 모으고, Message 객체면 type에 따라 로그만 남긴다.

### 3-3. `AsyncSqliteSaver` 의 컨텍스트 매니저

증상: 동기 SqliteSaver 예제대로 만들었더니 `RuntimeError: ... coroutine ...`.
해결: AsyncSqliteSaver는 `async with from_conn_string(...)` 으로 열어야 한다는 langgraph repo 코드를 직접 읽고 적용 (`app/main.py:39`).

### 3-4. 한국어 자연어 시간 파싱이 들쭉날쭉

증상: "30분 전" / "오후 2시쯤" 같은 입력을 LLM에게 Splunk `earliest`/`latest` 로 변환시키면 어떤 날엔 잘 되고 어떤 날엔 빈 값으로 옴.
현재 우회: `splunk_node` 에서 비어 있으면 `-30m@m` ~ `now` 기본값 사용.
회고: 이건 LLM에 맡기지 말고 코드 쪽에서 `dateparser` 같은 라이브러리로 처리하는 게 맞다. 다음 회차 작업분.

### 정리

- 새로 나온 라이브러리(LangGraph, langchain-anthropic)는 공식 문서보다 GitHub issue가 더 정확한 경우가 잦았다.
- "이 에러를 어떻게 안 나게 하지" 보다 **"이 에러가 나는 게 본 의도와 맞나"** 를 먼저 본 게 결과적으로 빨랐다 — `with_structured_output` 케이스가 그 예시 (에러는 우리 코드의 의도와 맞지 않아서 결국 다른 패턴으로 우회).

---

## 4. Ollama vs Claude 이중 백엔드

`app/llm_factory.py` 한 파일이 분기를 담당하고, 그래프 노드는 어느 백엔드인지 알지 못한다.
설계가 옳았다고 평가 — LangChain `BaseChatModel` 인터페이스 덕분에 노드 코드가 그대로다.

### 왜 둘 다 끌고 갔나

- **보안:** 사내 VOC와 운영 로그는 외부 API로 나가면 안 된다는 게 출발 가정. → 운영용은 Ollama 가 1순위.
- **품질 비교 baseline 필요:** 그러나 Ollama만 갖고 있으면 "결과가 나쁜 게 모델 한계인지 우리 프롬프트 문제인지" 구분이 안 됨. → Claude 를 비교용으로 함께 끌고 감.
- **운영 단계 옵션 확보:** 만약 사내에서 외부 API 사용 승인이 떨어지면, 코드 변경 없이 `.env` 한 줄로 갈아낄 수 있어야 한다.

### 비교 (체감)

| | Ollama `qwen2.5:14b` | Claude `sonnet-4-6` |
|---|---|---|
| 한국어 슬롯 추출 | OK. 후처리 필요 | 안정. 거의 그대로 사용 가능 |
| `with_structured_output` 신뢰도 | 낮음 (위 3-1) | 높음 |
| 마지막 원인 추론 단계 | 근거 인용을 잘 못함. 빈도 1위 예외만 단순 반복 | 빈도 패턴 + 도메인 설명을 엮어서 추론 |
| 응답 속도 | 로컬 GPU 의존 (<TODO: 측정치>) | 네트워크 의존, 일반적으로 빠름 |
| 비용 | 0 | <TODO: 1회 요청당 평균 토큰·비용> |
| 보안 | 외부 유출 없음 | 외부 전송 — 별도 승인 필요 |

### 결과적인 운영 방침

- **개발·데모: Ollama** (`LLM_PROVIDER=ollama`)
- **품질 검증·외부 발표 자료 만들 때: Claude** 로 한 번 더 돌려서 같은 입력 다른 출력 차이를 확인
- **운영 배포 시:** <TODO: 결정. Ollama는 사내 GPU 확보 필요 / Claude는 사내 외부 API 승인 필요>

---

## 5. MVP 이후 남은 숙제 (우선순위 순)

각 항목에 영향 받는 파일·함수를 함께 적었다. 다음 작업자를 위한 진입점.

### 5-1. traceId 연관 분석 (우선순위: 높음)

현재 `splunk_search.py` 는 `stats count by exception_class, exception_message` 까지만.
하지만 진짜로 사람이 알고 싶은 건 **"1위 예외의 traceId 하나만 골라서, 그 요청 동안 무슨 로그가 더 있었는가"** 다.

작업 안:
1. `splunk_node` 를 두 단계로 분리 — (a) 빈도 집계, (b) 1위 예외의 대표 traceId 1~3개로 `stats by traceId` 재조회.
2. 결과를 LLM 프롬프트에 "이 요청의 로그 흐름" 으로 추가.

### 5-2. `errors.yml` 매핑 (우선순위: 중)

`doc/architecture.md` §2-4 에 적힌 에러코드 분류 (5000번대=클라잘못, 9999=시스템오류) 가 코드에 아직 안 옴.
LLM이 9999에 가중치를 두고 5000번대는 무시할 수 있게 분류 정보를 함께 줘야 한다.

작업 안:
- `errors.yml` 파일 도입 (도메인별 코드 → 분류 → 심각도).
- `splunk_search.py` 에서 SPL에 `case()` 로 분류 컬럼 붙임 또는 우리 쪽에서 후처리.
- 프롬프트에 "심각도 높음만 분석 우선" 룰 명시.

### 5-3. 과거 장애 RAG (우선순위: 중)

현재는 매 요청이 독립. "이전에 비슷한 VOC가 있었나?" 가 안 됨.
다른 프로젝트 (소설 씬 분석) 회고에 적힌 것처럼 **Postgres + pgvector 한 테이블에 원문 + 임베딩 같이 두는 형태가 가장 단순**할 것.

작업 안:
- 분석 결과 + Splunk 요약을 텍스트 + 임베딩으로 저장.
- 새 VOC 들어오면 같은 도메인 안에서 유사 과거 사례를 retrieve 해서 프롬프트에 컨텍스트로 추가.
- 단, **벡터 검색은 같은 도메인 안에서만** 해야 한다 — 다른 도메인 혼입 시 환각 증가.

### 5-4. 시간 파싱 코드로 옮기기 (우선순위: 중)

위 3-4 참고. LLM에 맡기지 말고 `dateparser` 또는 `pendulum` 으로.

### 5-5. Teams 봇 연동 (우선순위: 낮음, 외부 요구 들어오면 상승)

현재 `/webhook/teams` 는 스텁. Adaptive Card 페이로드 파싱 + 응답 카드 생성이 필요.

### 5-6. 사용자 피드백 루프 (우선순위: 낮지만 중요)

"이 추정이 맞았다 / 틀렸다 / 일부만 맞았다" 를 받을 곳이 없음.
이게 없으면 프롬프트·모델 개선의 객관 근거를 못 쌓는다.
가장 가벼운 형태: 분석 결과 답변에 reaction emoji / 1-5 별점 받는 엔드포인트 하나 추가.

### 5-7. 운영 배포 미정

현재는 `uvicorn app.main:app` 로컬 실행.
사내 K8s에 올릴 때:
- Ollama 백엔드라면 GPU 노드가 필요 — <TODO: 사내 GPU 확보 가능 여부>.
- Claude 백엔드라면 외부 API 호출 승인 + 키 회전 — <TODO: 보안팀과 협의 진행 여부>.
- `data/checkpoints.sqlite` 는 K8s pod 재시작 시 휘발됨 → 운영에서는 Postgres 등으로 옮겨야 함 (LangGraph가 PostgresSaver 제공).

---

## 6. 다음 사람을 위한 한 줄들

- **외부 토큰 발급은 첫 주에.**
- **LLM의 "값" 과 "확정 여부" 는 분리해라.** 확정은 코드가 한다.
- **Tool 출력은 정제해서 LLM에 줘라.** 빈도 50건이 raw 5000줄보다 낫다.
- **새 라이브러리 에러는 그대로 검색하는 게 빠르다.** 단, 우회한 자리에 *왜* 우회했는지 주석 한 줄은 남겨라.
- **백엔드 분기는 한 파일(`llm_factory.py`)에 가둬라.** 그래프 노드가 그걸 알면 안 된다.
- **MVP는 돌아간다고 운영이 되는 건 아니다.** traceId·errors.yml·feedback loop 가 없으면 사람을 정말 줄이지는 못한다.
