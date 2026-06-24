# Architecture — Unified Customer Profile

## System overview

A system that ingests multi-channel customer data, identifies the same customer with
Entity Resolution, and manages unified customer profiles.

## Core data flow

```
[Channel data] → [Ingestion] → [S3/Glue Table]
       ↓                                     ↑
[Existing DB (RDS/Aurora)] → [Glue Connection/Crawler]
                                     │
                              [ETL Transform]
                       (normalization/cleansing/quality filter)
                                     │
                                     ▼
                            [Entity Resolution]
                              ↙     ↓     ↘
                         Simple  Advanced   ML
                              ↘     ↓     ↙
                         [Match results (Golden Record)]
                                     ↓
                      [Customer Profiles Import]
                                     ↓
                         [360° Unified Profile]
                            ↙          ↘
                     [Calculated        [Knowledge
                      Attributes]        Graph (optional)]
```

## Role of each layer

### Layer 1: Foundation
- **KMS Key**: single key for all data encryption
- **SQS DLQ**: central queue for all asynchronous failures
- **WHY**: guarantees security defaults + operational visibility at the infrastructure level

### Layer 2: Storage
- **S3 Bucket**: CSV uploads, Glue crawl target, ER output storage
- **WHY**: the intermediate store for all data exchange. Glue + ER are S3-based

### Layer 3: Profiles
- **Connect Instance**: the container for the Customer Profiles feature (note: instance quota)
- **CP Domain**: the logical separation unit for profile data
- **Object Types**: schema definitions for transaction/event data
- **Calculated Attributes**: automatic calculation of aggregate metrics (SUM, AVG, COUNT, MIN, MAX, etc.)
- **WHY**: an AWS managed service providing profile CRUD + automatic aggregation

### Layer 4: Matching
- **Glue Database/Table**: schema definition for ER input data
- **Entity Resolution Workflows**: the unit of matching execution
  - Simple: exact match (1 match key)
  - Advanced: fuzzy matching (composite rule)
  - ML: learning-based probabilistic matching
- **DynamoDB Tables**: match results, accuracy metrics, AI suggestions, rule change history
- **WHY**: ER is a stateless batch job → cache results in DDB for instant lookup from the API

### Layer 5: Ingestion
- **CSV Mode**: S3 upload → Lambda processing → load into Glue Table
- **Kinesis Mode**: real-time stream → EventBridge Pipe → load directly into CP
- **Parquet Mode**: S3 Parquet upload → Glue Table direct reference (no transformation needed, compression and schema built in)
- **Glue Connection Mode**: existing DB (RDS, Aurora, DynamoDB) → JDBC Connection + Crawler → Glue Table auto-created
- **Hybrid Mode**: Glue Connection (initial migration) + Kinesis (new events)
- **WHY**: customer data sources vary, so support all input paths. Demo uses CSV, production uses Glue+Kinesis

### Layer 5.5: ETL Transform (optional)
- **Inline (Lambda)**: for small/simple transformations, process directly inside the Lambda handler
- **Glue Job (PySpark)**: large-volume/complex transformations (Korean-English name normalization, phone number standardization, relay email handling)
- **Step Functions Pipeline**: full orchestration of Ingest → ETL → ER → CP Import
- **WHY**: raw data quality directly drives ER accuracy. After normalization, ER precision can improve by 10-30%
- **Cognito User Pool**: user management + Hosted UI
- **Cognito Authorizer**: token verification at API Gateway
- **WHY**: AWS-native OIDC. No additional infrastructure needed

### Layer 6: Auth
- **Cognito User Pool**: user management + Hosted UI
- **Cognito Authorizer**: token verification at API Gateway
- **WHY**: AWS-native OIDC. No additional infrastructure needed

### Layer 7: API
- **API Gateway REST API**: exposes all backend functionality
- **Lambda Handlers**: per-domain handlers (matching, profiles, accuracy, ai-agent, etc.)
- **WHY**: Serverless + auto-scaling + Cognito integration

### Layer 8: Graph (optional)
- **Neptune Cluster**: graph DB (openCypher/Gremlin)
- **Graph Sync Lambda**: sync CP profiles → Neptune nodes/edges
- **GraphRAG Lambda**: natural-language query → Cypher conversion → result interpretation
- **WHY**: relationship-based insights (social networks, influence analysis). Optional because cost is high

### Layer 9: Cross-Domain (optional)
- **Multi-domain CP**: independent profile domains per industry/business unit
- **Platform CP**: unified view across domains
- **Cross-Domain ER**: matches profiles from different domains
- **WHY**: ecosystem integration like airline+hotel+travel agency. Optional because complexity is high

## Design Decisions

| Decision | Choice | Alternatives | Rationale |
|------|------|------|------|
| IaC | CDK (TypeScript) | CloudFormation, Terraform | Type safety + CDK Custom Resources for detailed CP/ER configuration |
| Data exchange | S3 | DynamoDB, RDS | Native data source for Glue+ER |
| Match result storage | DynamoDB | S3+Athena | Millisecond lookup, served directly from the API |
| Frontend | React+Cloudscape | Amplify Studio | AWS style consistency + freedom to customize |
| AI | Bedrock Claude | SageMaker | Serverless, no separate model management needed |
| Auth | Cognito | None/Custom | Managed + OIDC standard + native API GW integration |
| Deployment | CDK Deploy | CI/CD Pipeline | Prioritize demo simplicity. Add a Pipeline for production |

## CDK stack dependencies

```
Foundation ─┐
            ├─→ Profiles ─┐
Storage ────┤             │
            ├─→ Matching  ├─→ API
            ├─→ Ingestion │
            └─→ Auth ─────┘
                          │
                [Optional]│
                Graph ────┘
```
