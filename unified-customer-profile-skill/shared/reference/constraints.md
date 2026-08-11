# Constraints — Known Limitations & Gotchas

## Manual configuration required at deployment

1. **Connect Instance → Customer Profiles activation**
   - Creating the Connect Instance + CP Domain via CDK is possible
   - But the **association** of the Instance and Domain is manual in the Console
   - Select `Profile creation: "Create inferred profiles only"`
   - Can be automated with a CDK Custom Resource (see associate-connect-domain.ts)

2. **Connect → Data Source Integration (Kinesis mode)**
   - The Kinesis → CP integration is configured in the Console
   - Create an EventBridge Pipe → select that Pipe in the CP integration → specify Object Type Mapping

3. **ER ML Matching Training**
   - Training data needed on the first run (minimum ~100 labeled pairs)
   - Rule-based is usable immediately

## CDK-related constraints

1. **Connect Instance creation order**
   - Creating multiple instances simultaneously fails in the "pending" state
   - Always serialize with `addDependency()`
   ```typescript
   domainStack.addDependency(previousDomainStack);
   ```

2. **CP Domain Custom Resources**
   - Object Type creation is not natively supported by CDK → a Custom Resource Lambda is needed
   - Same for Calculated Attributes
   - References: `custom-resources/upsert-object-type.ts`, `create-calculated-attributes.ts`

3. **ER Schema Mapping**
   - The Glue Table's column names must exactly match the InputSourceConfig of the ER Schema Mapping
   - Watch case sensitivity (all lowercase recommended)

## Entity Resolution constraints

1. **Asynchronous execution**
   - An ER Job is an asynchronous batch. Not real-time matching.
   - StartMatchingJob → GetMatchingJob (polling) → results stored in S3
   - In the demo, a Lambda polls + parses the results

2. **⚠️ Concurrent matching jobs: Region당 1개 (조정 불가)**
   - StartMatchingJob quota: **1 concurrent job per region** — Adjustable: **No**
   - 여러 workflow type(simple, advanced, ml)을 `Promise.all`로 동시 실행하면 **HTTP 200을 반환하지만 job이 시작되지 않는다** (silent failure)
   - `GetMatchingJob`으로 확인하면 job history가 없음 — 에러도 없이 누락
   - **필수 대응**: 순차 실행(for loop) + 실행 전 `/running` 체크 + 폴링 UI
   - UI에 "리전당 하나의 매칭 작업만 동시에 실행 가능합니다" 배너 표시 권장

3. **Input format**
   - Glue Table required (S3 direct reference not allowed)
   - Columns: `variantid` (UNIQUE_ID), PII fields, `sourcechannel`
   - Partitioning: not needed (ER scans the entire table)

4. **Rule combination**
   - Multiple Rules possible in one Workflow (OR relationship)
   - Match Keys within a Rule are an AND relationship
   - e.g. NameAndEmail = (Name AND Email match)

## Customer Profiles constraints

1. **PutProfileObject call rules**
   - JSON that exactly matches the Object Type is required
   - Fields defined in Keys must have values
   - The `_profileObjectType` header is required

2. **SearchProfiles limitations**
   - Only key-based search (no free-text search)
   - To list profiles: use ListProfileObjects

3. **Calculated Attributes**
   - Can only aggregate fields of an existing Object Type
   - Cannot be changed after creation → delete and recreate
   - 20 limit (per domain)

## API Gateway constraints

1. **⚠️ Integration timeout: 29초 (변경 불가)**
   - REST API의 Lambda 통합 최대 timeout은 **29,000ms** (hard limit)
   - Lambda 자체는 최대 900초까지 가능하지만 API Gateway가 29초에 504를 반환
   - **영향받는 기능**: Bedrock 규칙 생성(~35s), 매칭 추천(~30s), 그래프 인사이트(~38s)
   - **필수 패턴 — 시작+폴링**:
     - `POST /api/<feature>/start` → DynamoDB에 `status: PROCESSING` 저장 후 Lambda self-invoke (async)
     - `GET /api/<feature>/status?id=<requestId>` → DynamoDB에서 결과 조회
     - 프론트엔드: 10초 간격 폴링 + 진행 표시 + 타임아웃 안내
   - **캐시 재사용**: 동일 입력에 대한 결과는 DynamoDB에 TTL과 함께 저장 → 재호출 시 즉시 반환

   | 기능 | 실측 응답 시간 | 캐시 PK | 폴링 라우트 |
   |---|---|---|---|
   | AI 규칙 생성 | >29s | `SUGGESTION#{requestId}` | `GET /api/ai-agent/latest-generation` |
   | 매칭 전략 추천 | ~30s | `MATCH_RECO#{requestId}` | `GET /api/matching/recommend-latest` |
   | 그래프 인사이트 | ~38s | `GRAPH_INSIGHT#{requestId}` | `GET /api/graph-rag/insights-status` |

2. **⚠️ Authorizer 401에 CORS 헤더 누락 (Gateway Response 필수)**
   - Cognito Authorizer가 401/403을 반환할 때 **CORS 헤더가 포함되지 않음**
   - 브라우저는 CORS 오류로 처리 → `Failed to fetch` 메시지만 노출 → 401인지 확인 불가
   - 클라이언트의 401 재시도 로직도 `response.status`를 읽을 수 없어 무력화
   - **필수 대응**: API Gateway에 `DEFAULT_4XX`, `DEFAULT_5XX` GatewayResponse 설정
   - CDK 코드: `cdk-stacks.md`의 API Stack 참조

## Frontend constraints

1. **Cognito Hosted UI Callback URL**
   - Both localhost:3000 (development) + Amplify URL (production) must be registered
   - Recommended to register both URLs in advance in CDK

2. **CORS**
   - CORS configuration required at API Gateway
   - Consider preflight requests during Cognito token refresh

3. **⚠️ AuthGate + 단일 userManager 인스턴스**
   - `userManager`를 여러 곳에서 `new`하면 세션 충돌 발생
   - **반드시** `api/auth.ts`에서 singleton export → 전체 앱에서 하나만 사용
   - `AuthGate` 래퍼 컴포넌트로 로그인 여부를 화면 단위로 제어 (인증 실패 시 명확한 UI 표시)

4. **⚠️ 디자인 원칙 (기능이 아닌 시각 품질)**
   - **액센트 1색 원칙**: primary 색상 하나 + neutral 계열만 사용. 원색 여러 개 혼합 금지
   - **색은 정보에만**: badge/status/alert 등 의미를 전달할 때만 색 사용. 장식 목적 금지
   - **장식 그라데이션 금지**: 제목 텍스트 그라데이션, 배경 글로우, 컬러 보더 그라데이션 사용하지 않음
   - shadcn/ui의 CSS variable 테마를 그대로 사용하면 위 원칙이 자연스럽게 충족됨

## Security considerations

1. PII data → KMS encryption + S3 bucket policy
2. API → Cognito token required (no public endpoint)
3. Lambda → placing it inside a VPC increases NAT Gateway cost
4. Neptune → VPC isolation required (no public access)
