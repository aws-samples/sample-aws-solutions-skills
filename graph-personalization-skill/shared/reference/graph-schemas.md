# Graph Schemas (Industry-Specific)

> Per-industry vertex/edge labels + properties + recommendation query patterns. Discovery question #1 determines this schema. This document is the single biggest specific value of this skill.

## Schema design principles

1. **Vertex label** = domain noun (singular, PascalCase): `User`, `Item`, `Category`
2. **Edge label** = verb/relationship (SCREAMING_SNAKE_CASE): `BOUGHT`, `IN_CATEGORY`
3. **Property** = camelCase: `userId`, `purchasedAt`
4. **Every vertex** has `id` (unique) + `createdAt` + `updatedAt`
5. **Every edge** has `weight` + `at` (timestamp ms)

## E-commerce (default)

```
Vertex labels:
  User                  Customer
    id, segment, country, registeredAt
  Item                  Product
    id, name, price, stockQuantity, status
  Category              Product hierarchy
    id, name, parentId
  Brand                 Manufacturer
    id, name, country
  Segment               Customer segment
    id, name (VIP, Regular, NewUser)

Edge labels:
  (User)-[BOUGHT {at, weight=5.0, qty}]->(Item)
  (User)-[VIEWED {at, weight=1.0, durationSec}]->(Item)
  (User)-[CART {at, weight=3.0}]->(Item)
  (User)-[RATED {at, value=1-5}]->(Item)
  (User)-[IN_SEGMENT {at}]->(Segment)
  (Item)-[IN_CATEGORY {at}]->(Category)
  (Item)-[HAS_BRAND {at}]->(Brand)
  (Category)-[PARENT_OF]->(Category)
```

### Key query: "recommend from similar buyers"

```cypher
MATCH (u:User {id: $userId})-[:BOUGHT]->(:Item)<-[:BOUGHT]-(other:User)
WHERE u <> other
WITH other, count(*) AS sharedItems
WHERE sharedItems >= 3                     -- privacy threshold
MATCH (other)-[r:BOUGHT]->(rec:Item)
WHERE NOT (u)-[:BOUGHT]->(rec)
  AND rec.status = 'active'
WITH rec,
     sum(sharedItems * r.weight * exp(-0.05 * (timestamp() - r.at) / 86400000)) AS score
ORDER BY score DESC LIMIT 10
RETURN rec.id, rec.name, score
```

### Cross-sell: "what people who bought this item also bought"

```cypher
MATCH (item:Item {id: $itemId})<-[:BOUGHT]-(:User)-[:BOUGHT]->(other:Item)
WHERE item <> other
WITH other, count(*) AS coBuyCount
WHERE coBuyCount >= 5
RETURN other.id, other.name, coBuyCount
ORDER BY coBuyCount DESC LIMIT 10
```

## Media (music / video / news)

```
Vertex labels:
  User                  Viewer
    id, ageGroup, region, language
  Content               Video / Track / Article
    id, title, type, durationSec, releasedAt
  Genre                 Genre
    id, name
  Person                Director / Artist / Author
    id, name, role
  Tag                   User tag / keyword
    id, name

Edge labels:
  (User)-[WATCHED {at, weight, completionRatio}]->(Content)
  (User)-[RATED {at, value=1-5}]->(Content)
  (User)-[FOLLOWED {at}]->(Person)
  (User)-[FAVORITE {at}]->(Content)
  (Content)-[HAS_GENRE]->(Genre)
  (Content)-[BY {role: 'director'}]->(Person)
  (Content)-[TAGGED {weight}]->(Tag)
  (User)-[FOLLOWS {at}]->(User)            -- social graph
```

### Key query: "content watched by people you follow"

```cypher
MATCH (u:User {id: $userId})-[:FOLLOWS]->(friend:User)-[w:WATCHED]->(c:Content)
WHERE NOT (u)-[:WATCHED]->(c)
  AND w.completionRatio >= 0.8       -- only items watched >= 80%
WITH c, count(distinct friend) AS friendCount, avg(w.completionRatio) AS avgCompletion
WHERE friendCount >= 3
RETURN c.id, c.title, friendCount, avgCompletion
ORDER BY friendCount DESC, avgCompletion DESC
LIMIT 10
```

### "similar viewing taste + new releases in the same genre"

```cypher
MATCH (u:User {id: $userId})-[:WATCHED]->(seen:Content)-[:HAS_GENRE]->(g:Genre)
WITH u, g, count(*) AS gCount
ORDER BY gCount DESC LIMIT 3                          -- top 3 genres
MATCH (g)<-[:HAS_GENRE]-(rec:Content)
WHERE rec.releasedAt > timestamp() - 30 * 86400000   -- last 30 days
  AND NOT (u)-[:WATCHED]->(rec)
RETURN rec.id, rec.title, g.name AS genre
ORDER BY rec.releasedAt DESC LIMIT 10
```

## B2B SaaS (sales / cross-sell)

```
Vertex labels:
  Account               Customer company
    id, name, industry, size, planTier, mrrUsd
  Feature               Product feature
    id, name, category, isPaidOnly
  Industry              Industry classification
    id, name (FinTech, EdTech, ...)
  Plan                  Pricing plan
    id, name (Starter, Pro, Enterprise)
  Contact               Point of contact
    id, role, email

Edge labels:
  (Account)-[USES {at, weight, freqPerWeek}]->(Feature)
  (Account)-[ON_PLAN {since, mrrUsd}]->(Plan)
  (Account)-[IN_INDUSTRY]->(Industry)
  (Feature)-[REQUIRES_PLAN]->(Plan)
  (Account)-[UPGRADED_FROM {at}]->(Plan)        -- upgrade history
  (Contact)-[WORKS_AT]->(Account)
```

### Key query: "features used by larger customers in a similar industry"

```cypher
MATCH (a:Account {id: $accountId})-[:IN_INDUSTRY]->(ind:Industry)
MATCH (other:Account)-[:IN_INDUSTRY]->(ind)
WHERE a <> other
  AND other.mrrUsd > a.mrrUsd                  -- larger customers
MATCH (other)-[u:USES]->(f:Feature)
WHERE NOT (a)-[:USES]->(f)
  AND f.isPaidOnly = true
WITH f, count(distinct other) AS adopterCount, avg(u.freqPerWeek) AS avgUsage
WHERE adopterCount >= 3
RETURN f.id, f.name, adopterCount, avgUsage
ORDER BY adopterCount DESC, avgUsage DESC
LIMIT 5
```

### "upgrade likelihood score"

```cypher
MATCH (a:Account {id: $accountId})-[:USES]->(f:Feature)-[:REQUIRES_PLAN]->(p:Plan {name: 'Pro'})
MATCH (a)-[:ON_PLAN]->(currentPlan:Plan {name: 'Starter'})
WITH a, count(distinct f) AS proFeaturesUsed
RETURN a.id, proFeaturesUsed,
       CASE WHEN proFeaturesUsed >= 3 THEN 'HIGH'
            WHEN proFeaturesUsed >= 1 THEN 'MEDIUM'
            ELSE 'LOW' END AS upgradeLikelihood
```

## Recruiting

```
Vertex labels:
  Candidate
    id, yearsExperience, location, expectedSalary
  Skill
    id, name, category (technical/soft)
  Company
    id, name, industry, size, fundingStage
  JobPosting
    id, title, level, postedAt, status
  School
    id, name, country

Edge labels:
  (Candidate)-[HAS_SKILL {level=1-5, yearsUsed}]->(Skill)
  (Candidate)-[WORKED_AT {from, to, role}]->(Company)
  (Candidate)-[STUDIED_AT {from, to, degree}]->(School)
  (Candidate)-[APPLIED_TO {at, status}]->(JobPosting)
  (JobPosting)-[REQUIRES_SKILL {minLevel}]->(Skill)
  (JobPosting)-[POSTED_BY]->(Company)
```

### Key query: "candidates similar to this one who got hired"

```cypher
MATCH (c:Candidate {id: $candidateId})-[:HAS_SKILL]->(s:Skill)
WITH c, collect(s) AS candidateSkills

MATCH (similar:Candidate)-[:HAS_SKILL]->(s2:Skill)
WHERE similar <> c AND s2 IN candidateSkills
WITH c, similar, count(distinct s2) AS sharedSkills
WHERE sharedSkills >= 5

MATCH (similar)-[a:APPLIED_TO]->(:JobPosting)
WHERE a.status = 'hired'
WITH similar, sharedSkills, count(*) AS hireCount
WHERE hireCount >= 1

MATCH (similar)-[:WORKED_AT]->(co:Company)
RETURN similar.id, similar.yearsExperience, sharedSkills, hireCount,
       collect(co.name)[..3] AS companies
ORDER BY sharedSkills DESC LIMIT 10
```

## Healthcare (very strict privacy)

> ⚠️ HIPAA / PIPA compliance required. PII must be separately encrypted + audit logging is mandatory.

```
Vertex labels:
  Patient (id is hashed; raw PII lives in a separate system)
    id (hash), ageGroup (10-year band only), gender
  Diagnosis
    id, kcdCode, name, severity
  Medication
    id, atcCode, name, isControlled
  Treatment
    id, name, type
  Hospital
    id, type (university / general / clinic)

Edge labels:
  (Patient)-[HAS_DIAGNOSIS {at, severity}]->(Diagnosis)
  (Patient)-[PRESCRIBED {at, durationDays}]->(Medication)
  (Patient)-[RECEIVED {at}]->(Treatment)
  (Diagnosis)-[TREATED_WITH {efficacyScore}]->(Medication)
  (Medication)-[INTERACTS_WITH {severity}]->(Medication)
  (Diagnosis)-[CO_OCCURS_WITH {strength}]->(Diagnosis)
```

### "effective treatment patterns of similar patients" (privacy-hardened)

```cypher
MATCH (p:Patient {id: $patientHashId})-[:HAS_DIAGNOSIS]->(d:Diagnosis)
WITH p, collect(d.kcdCode) AS pDiagnoses

MATCH (similar:Patient)-[:HAS_DIAGNOSIS]->(d2:Diagnosis)
WHERE similar <> p
  AND d2.kcdCode IN pDiagnoses
  AND similar.ageGroup = p.ageGroup
WITH p, similar, count(distinct d2) AS sharedDiagnoses
WHERE sharedDiagnoses >= 2

-- medications those patients received + efficacy score
MATCH (similar)-[pres:PRESCRIBED]->(m:Medication)
WHERE NOT (p)-[:PRESCRIBED]->(m)
WITH m, count(distinct similar) AS similarPatientCount,
     avg(pres.durationDays) AS avgDuration
WHERE similarPatientCount >= 5                -- privacy threshold ↑

RETURN m.atcCode, m.name, similarPatientCount, avgDuration
ORDER BY similarPatientCount DESC LIMIT 5
```

→ The Bedrock prompt must also avoid exposing patient IDs or exact disease names.

## Custom Schema

When the Discovery answer is "other / define my own", ask the following follow-up:

```
1. 5-10 main entities (vertices):
   e.g. "Doctor, Patient, Appointment, Hospital, Symptom"

2. 5-10 main relationships (edges) + weights:
   e.g. "Doctor -SPECIALIZES_IN-> Specialty (weight: 1.0)"
   e.g. "Patient -BOOKED-> Appointment -WITH-> Doctor"

3. 1-3 recommendation scenarios:
   e.g. "recommend a doctor that fits the patient"
   e.g. "doctors seen by patients with similar symptoms"
```

→ After the follow-up, the skill auto-generates the schema + applies a Cypher template.

## Bulk Loader CSV format

Used for the initial load:

```csv
# vertices.csv
~id,~label,name:String,country:String
u-1,User,"Alice","KR"
u-2,User,"Bob","US"
i-1,Item,"Widget A","Korea"
```

```csv
# edges.csv
~id,~from,~to,~label,weight:Double,at:Long
e-1,u-1,i-1,BOUGHT,5.0,1700000000000
e-2,u-1,i-2,VIEWED,1.0,1700001000000
```

→ The skill auto-generates the per-industry CSV header template.

## Schema migration (hard to change — proceed carefully)

```
Change scenario                          Handling
──────────────────────────────────────────────────────────────
add edge weight                          SET directly
add new vertex label                     no impact on existing data
rename a property                        update all vertices/edges + change code
rename an edge label                     very hard — create a new label, migrate, change code
split a vertex (User → User+Account)     very hard — re-design required
```

→ Proceed to Phase 3 only after the schema is finalized and user-approved in **Phase 2 Design**.
