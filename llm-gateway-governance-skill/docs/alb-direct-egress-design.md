# 설계 문서: CloudFront 제거 → ALB 직접 노출 + `certMode` 3-모드

> 대상 스킬: `llm-gateway-governance`
> 작성일: 2026-07-12 (v2 — CloudFront 완전 제거로 확정)
> 상태: 설계 확정. 아직 스킬 코드 미수정 — 이 문서는 계획/수정 대상/트레이드오프.

> ⚠️ **SUPERSEDED (2026-07-13, v3)** — 이 문서의 3모드 설계는 이후 **2모드로 단순화되어 스킬에 반영 완료**되었다. 현행 설계:
> - `certMode`는 **`acm` / `http` 2가지**. **`self-signed` 모드는 제거** (`SelfSignedCert` Custom Resource, `ca.pem` 배포, `NODE_EXTRA_CA_CERTS` 신뢰 설정, 397일 수동 갱신 부담 모두 삭제).
> - ALB는 두 모드 모두 **항상 internet-facing**이며, 접근 제어는 **`litellm.albIngressCidrs` SG allowlist**(Discovery 필수 답변)로 일원화. **AWS WAF 미사용**.
> - `http` 모드의 **internal ALB + SSM 포트포워딩/VPN 경로 제거** — 퍼블릭 ALB의 HTTP:80을 SG로 제한해 직접 접속. 평문 전송(가상 키 + 프롬프트)은 GATE-1 명시 승인 항목(0.0.0.0/0이면 별도 승인).
> - 아래 본문 중 self-signed/WAF/SSM 터널 관련 내용은 **역사 기록**으로만 유지한다. 현행 기준은 `shared/reference/constraints.md` · `decision-tree.md` · `shared/patterns/cdk-stacks.md`.

---

## 0. 결정 (2026-07-12)

**CloudFront는 완전히 제거한다.** VPC Origin의 **120초 read timeout은 AWS 하드 리밋**(쿼터 상향 불가)이라 Opus/Fable의 extended thinking 등 장시간 응답에 치명적이고, 어떤 설명·옵션으로도 우회 불가하기 때문이다. 따라서 `edgeMode: cloudfront` 같은 토글조차 두지 않는다.

엣지는 **항상 ALB**다. 사용자는 TLS 전략만 3가지 중 고른다 (신규 `litellm.certMode`):

| certMode | 용도 | TLS | 도메인 | 클라이언트 설정 | 등급 |
|----------|------|-----|--------|----------------|------|
| **`acm`** | 도메인/서브도메인 보유 | ACM 공인, 자동 갱신, HTTPS:443 | 필요 | 불필요(공인 신뢰) | ✅ 권장 / PROD |
| **`self-signed`** | 도메인 없음, TLS는 필요 | 사설 CA import, HTTPS:443 | 불필요 | `ca.pem` 배포(온보딩) | ⚠️ 내부 / PoC |
| **`http`** | 도메인 없음, 격리된 단기 PoC | 없음(HTTP) | 불필요 | 없음 | ⛔ PoC 전용 · 키 평문 |

**세 모드 공통 이득**: ALB `idleTimeout`(최대 4000s, 기본 900s)이 CloudFront의 120초 상한을 대체 → 장시간 응답 504 근본 해소.

---

## 1. 배경 & 근본 원인 (Root Cause)

### 현재(제거 대상) 경로
```
Dev → CloudFront(TLS, us-east-1, CdnStack) → VPC Origin → Internal ALB(HTTP:4000) → LiteLLM(ECS)
```

### 증상 & 원인
- Opus/Fable 등 **응답 지연 시 CloudFront 504**, 게다가 **LiteLLM access log에 요청이 안 남는다**(엣지에서 잘림).
- CloudFront **VPC Origin의 read/response timeout 하드 상한 = 120초**(일반 custom origin의 180초와 달리 쿼터 상향 불가). extended thinking의 토큰 무방출 구간이 120초 초과 시 연결 절단.
- 캐싱도 꺼져 있어(`CACHING_DISABLED`) CloudFront는 "120초 상한 있는 TLS 프록시"일 뿐 → 제거가 합리적.

---

## 2. 목표 아키텍처 (ALB always the edge)

```
[certMode = acm 예시]
Dev (Claude Code / Codex)
  → Internet-facing ALB (HTTPS:443, ACM cert in config.awsRegion, idleTimeout=900s)
      → Target Group (HTTP:4000) → LiteLLM (ECS Fargate, PRIVATE_WITH_EGRESS)   ← 타겟은 프라이빗 유지
  + (권장) AWS WAF WebACL(REGIONAL) attach
  + Route53 A-record alias → ALB
[내부] Internal ALB (HTTP:4000, Lambda SG 전용) ← Token Service 키 발급 (기존 유지)
```

### 모드별 상세

**`acm` (권장/PROD)**
- 서브도메인(예: `llmgw.example.com`) + ACM 공인 인증서(**리전 = `config.awsRegion`**, us-east-1 불필요 → CloudFront 대비 단순화) + Route53 A-record alias.
- 인터넷 페이싱 ALB HTTPS:443, HTTP:80→443 리다이렉트. 자동 갱신·공인 신뢰 → 클라이언트 무설정.

**`self-signed` (도메인 없음, TLS 필요)**
- 사설 CA로 서버 인증서 서명 → ACM `import-certificate` → HTTPS:443 (`fromCertificateArn`).
- **Chicken-and-egg**: ALB DNS는 생성 후 확정 → (a) 2-phase 배포(ALB DNS를 SAN에), 또는 (b) 고정 호스트명 + 개발자 hosts/사내DNS 매핑.
- `ca.pem`을 **온보딩 가이드로 배포**, 클라이언트는 CA만 신뢰(검증 끄지 않음): `NODE_EXTRA_CA_CERTS`(Claude Code), `REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE`(token helper).
- 수동 갱신(ACM import는 자동 갱신 없음), leaf 유효기간 짧게(≤397일). PROD TODO.

**`http` (격리된 단기 PoC 전용)**
- TLS 없이 HTTP만. ⚠️ **가상 키(Bearer)가 평문 전송** → 인터넷 노출 시 키 탈취 → Bedrock 무단 사용/비용/데이터 유출.
- **반드시** SG를 특정 개발자 IP로 제한하거나 SSH 터널/SSM 포트포워딩/VPN으로만 접근. GATE에서 명시적 승인 필수, **절대 기본값·인터넷 오픈 금지**.

### Token Service 내부 경로 — 듀얼 ALB 권장
인터넷 페이싱 ALB의 DNS는 퍼블릭 IP로 resolve → 프라이빗 Lambda의 키 발급 호출이 NAT 헤어핀. 회귀 위험 최소화를 위해:

| 옵션 | 구성 | 결론 |
|------|------|------|
| **A. 듀얼 ALB (권장)** | 기존 내부 ALB(HTTP:4000) → Token Service 전용(SSM·코드 무변경) / 신규 공개 ALB → 개발자용. 둘 다 같은 ECS 타겟 | 내부/외부 분리, 회귀 최소. +ALB 1개(~$16–22/월) |
| B. 단일 공개 ALB + 리스너 2개 | 공개 리스너(443/80, 인터넷) + HTTP:4000(Lambda SG 전용). Lambda는 `http://{albDns}:4000` | ALB 1개. 헤어핀 가능성 → 실측 필요 |

→ **A(듀얼 ALB)** 채택 시 Token Service·SSM 완전 무변경(가장 안전).

---

## 3. 제거/재정의되는 Hard Constraints

| HC | 기존 | 개정 |
|----|------|------|
| **HC#1** | CloudFront 도메인리스 동작 | 삭제·재정의: TLS는 `certMode`로. 도메인리스는 `self-signed` 또는 `http`(도메인 필요는 `acm`만) |
| **HC#5** | 내부 ALB만, CloudFront가 유일 공개면 | 재정의: **공개 ALB가 엣지**(acm/self-signed는 WAF+SG allowlist, http는 SG IP 제한 필수). Token Service용 내부 ALB는 유지 |
| **HC#8** | UI 리다이렉트용 CloudFront Function | 삭제: `X-Forwarded-Proto`(ALB HTTPS 리스너가 주입) + `PROXY_BASE_URL`로 대체 |
| **HC#10** | CloudFront 120s / ALB 150s | 재정의: CloudFront 상한 없음. **ALB `idleTimeout`(기본 900s, 최대 4000s)** 이 장시간 응답 지배 |
| **HC#(신규)** | — | **`http` 모드는 키 평문** → SG IP 제한/터널 필수, GATE 승인, 인터넷 오픈 금지 |

---

## 4. 파일별 수정 계획

> 정본은 `claude-code/.../SKILL.md` 하나. 편집 후 `scripts/sync-skills.sh` → `verify` 필수.

### 4.1 코드 골든 패턴 (`shared/patterns/cdk-stacks.md`)

| 대상 | 수정 |
|------|------|
| **§0-2 schema.ts** | `litellm.certMode: 'acm' \| 'self-signed' \| 'http'` 로 확장(기존 `acm-dns` 대체). `acm`이면 `domainName`/`hostedZoneId`/`hostedZoneName` 필수, `self-signed`면 `certificateArn` 필수 — 런타임 fail-fast. `albIdleTimeoutSeconds?`(900), `albIngressCidrs?`, `publicAlbEnabled` 등 |
| **§0-3 constants.ts** | ALB 포트/타임아웃/WAF 상수 |
| **§0 bin/app.ts** | **CdnStack import·인스턴스화·allStacks·nag에서 완전 제거**. `crossRegionReferences`는 AgentCore/Mantle만 남김 |
| **§1 NetworkStack** | 공개 ALB SG(443 또는 http 시 80, `albIngressCidrs`), 인터넷 페이싱 ALB용 **퍼블릭 서브넷** 분기. ECS 타겟은 `PRIVATE_WITH_EGRESS` 유지 |
| **§4 LiteLLMStack** | 듀얼 ALB: 내부 ALB(HTTP:4000, SSM) 유지 + 공개 ALB 추가. 공개 ALB 리스너를 `certMode`로 분기 — `acm`(HTTPS:443+ACM 리전+Route53 alias+80→443), `self-signed`(HTTPS:443+`fromCertificateArn`+80→443), `http`(HTTP만, SG IP 제한). `idleTimeout=albIdleTimeoutSeconds`. `publicHttpsUrl` 재산출 |
| **§8 CdnStack** | **파일/섹션 삭제** |
| **§9 nag** | 인터넷 페이싱 ALB findings → WAF 연결로 해소 또는 사유 명시 |
| **(신규) WAF** | acm/self-signed 인터넷 페이싱에 REGIONAL WebACL(Common rules + rate-limit) |
| **litellm-gateway.md** | TLS 종료 ALB 뒤 uvicorn `https` 리다이렉트 위해 `--forwarded-allow-ips`/`X-Forwarded-Proto` 신뢰(HC#8 대체) |
| **developer-onboarding.md** | base URL 재정의: `acm`=서브도메인, `self-signed`=ALB DNS/고정호스트+**`ca.pem` 배포·클라이언트 CA env**, `http`=http URL+**키 평문 경고**. 현재 15+곳의 "ALB_DNS=CloudFront" 서술 전면 교체 |

### 4.2 참조 (`shared/reference/`)
- **architecture.md**: mermaid/스택표에서 CloudFront·CdnStack 제거, `certMode` 3-모드 반영, 라이프사이클 6~7단계(ALB 직결) 개정.
- **decision-tree.md**: `certMode` 결정 분기(도메인 有→acm / 도메인 無·TLS 필요→self-signed / 격리 PoC→http).
- **constraints.md**: CloudFront 관련 3개 섹션(§11–20, §50–55, §122–133) 삭제·교체, 신규 §"certMode 3-모드 + http 키 평문 경고" + §"self-signed 2-phase/CA 배포".
- **aws-services.md**: CloudFront 행 삭제, ALB idleTimeout/ACM(리전)/WAF/`http` 경고 행.
- **prerequisites.md**: `acm`→도메인+Route53 필수, `self-signed`→openssl/CA 배포 경로, `http`→SG IP 제한 사전조건.

### 4.3 예시 & eval
- **examples/enterprise-sso.md** → `certMode: acm` + idleTimeout 900s.
- **examples/domainless-poc.md** → `certMode: self-signed`(기본) / `http`(격리) 두 갈래로 재작성(CloudFront 서술 제거).
- **evals**: "acm인데 domainName 누락 시 synth 실패", "self-signed ca.pem 온보딩 포함", "http 모드는 SG IP 제한 없이는 GATE 통과 금지", "120s+ 응답 504 미발생", "Token Service 내부 4000 경로 유지".

### 4.4 SKILL.md (정본 1벌 → sync)
- `description`: CloudFront 언급 제거, `certMode`(acm/self-signed/http) 추가.
- Phase 1 Discovery: "**TLS/도메인?** acm / self-signed / http 중 선택(도메인리스면 self-signed·http, http는 키 평문 경고+SG 제한 승인)".
- Phase 2/3/5 & 스택목록: CdnStack 삭제, 공개 ALB + certMode 리스너 + WAF + ACM(리전) 반영.
- Hard Constraints: HC#1/#5/#8/#10 위 표대로 재정의 + `http` 키 평문 신규 HC.

### 4.5 동기화
```bash
scripts/sync-skills.sh llm-gateway-governance-skill
scripts/sync-skills.sh verify   # md5 3벌 일치
```

---

## 5. 트레이드오프 (CloudFront[제거] → certMode 3-모드)

| 항목 | CloudFront(제거) | `acm` | `self-signed` | `http` |
|------|:---:|:---:|:---:|:---:|
| 장시간 응답 | ❌ 120s 상한 | ✅ idle 900s+ | ✅ | ✅ |
| 도메인 | 불필요 | **필요** | 불필요 | 불필요 |
| TLS/신뢰 | 엣지 공인 | ALB 공인(자동갱신) | 사설 CA(수동갱신) | ❌ 없음 |
| 클라이언트 설정 | 없음 | 없음 | `ca.pem` 신뢰 | 없음 |
| 키 기밀성 | ✅ | ✅ | ✅ | ⛔ 평문 |
| 공개면/DDoS | 엣지 흡수 | ALB(+WAF) | ALB(+WAF) | ALB(SG IP 제한) |
| 운영 부담 | 낮음 | 낮음 | 중(CA 배포·2-phase·갱신) | 낮음(단 위험) |
| 등급 | — | PROD | 내부/PoC | 격리 PoC |

**얻음**: 120초 상한 제거, us-east-1 크로스리전 인증서·CdnStack·Location Function 제거로 단순화, 홉 1개 감소.
**잃음**: CloudFront 무료 도메인리스 TLS·엣지 DDoS 흡수. → `acm`이면 도메인 비용, `self-signed`면 CA 운영, `http`면 보안 위험.

---

## 6. 보안 (필수)
1. **`http` 모드 인터넷 오픈 절대 금지** — 가상 키 평문. SG를 개발자 IP로 제한하거나 SSM 포트포워딩/VPN/터널로만. GATE 명시 승인.
2. **`self-signed`는 검증을 끄지 말 것** — `NODE_TLS_REJECT_UNAUTHORIZED=0`(전면 비활성) 대신 `NODE_EXTRA_CA_CERTS`(CA만 신뢰).
3. **acm/self-signed 인터넷 페이싱엔 WAF + SG allowlist** 권장.
4. **HTTP:4000 내부 리스너는 Lambda SG로만.**
5. Cognito 콜백(루프백)·Token Service(IAM/Cognito authorizer)·Mantle/Guardrail 백엔드 인증은 이 변경과 **무관**(무수정).

---

## 7. 롤아웃 순서
1. 실배포 검증: `acm`(또는 `self-signed`) + 듀얼 ALB → Opus/Fable 120s+ 응답이 504 없이 완료 + LiteLLM 로그 기록 확인.
2. 스킬 반영: golden 코드 → reference/examples/evals → SKILL.md HC → **sync + verify**.
3. 기본값: `certMode` 기본은 `acm`(가장 안전). `http`는 반드시 명시 선택 + 경고.

---

## 8. 검증 체크리스트
- [ ] `npm run typecheck && npx cdk synth --all` (3모드)
- [ ] `acm` + domainName 누락 → fail-fast / `self-signed` + certificateArn 누락 → fail-fast
- [ ] 공개 ALB 리스너가 certMode대로(acm/self-signed=443+cert, http=평문+SG제한)
- [ ] 내부 HTTP:4000(Lambda SG 전용) Token Service 정상
- [ ] ALB idleTimeout 900s, 120s+ 응답 504 미발생
- [ ] LiteLLM `/ui/` 리다이렉트가 https로 정상(CloudFront Function 없이)
- [ ] `self-signed`: ca.pem 온보딩 포함 + 클라이언트 CA env 문서화
- [ ] `http`: SG IP 제한/터널 없이는 GATE 미통과 문구
- [ ] WAF(REGIONAL) 연결(acm/self-signed)
- [ ] CdnStack·CloudFront 잔존 참조 0 (grep)
- [ ] `sync-skills.sh verify` md5 일치
- [ ] Mantle 3-step 회귀(Claude→GPT→Claude) 무영향

---

## 9. 미해결
- 듀얼 ALB(A) vs 단일 ALB(B): 실배포 시 인터넷 페이싱 ALB의 VPC 내부 DNS resolve/헤어핀 실측 후 확정.
- `self-signed` chicken-and-egg: 2-phase(SAN=ALB DNS) vs 고정 호스트명+hosts 매핑 중 온보딩 UX 관점에서 선택.
- `http` 모드를 아예 **내부 ALB + 터널** 조합으로만 강제할지(인터넷 페이싱 자체 차단) 검토.
