# Ingestion Patterns

> Two modes for reflecting user behavior (view, click, purchase) into the graph: **Real-time (Kinesis → Lambda → Neptune)** + **Batch (S3 + Bulk Loader)**.

## Real-time pipeline

```
User behavior occurs
       │
       ▼
┌──────────────────┐
│ Web/Mobile App   │
│  → Kinesis put   │ (or EventBridge → Kinesis)
└────────┬─────────┘
         ▼
┌──────────────────────────────────────┐
│ Kinesis Data Stream                   │
│  - 1-N shards (1M event/d ≈ 1 shard) │
│  - 24h retention                      │
└────────┬─────────────────────────────┘
         ▼  Event source mapping
┌──────────────────────────────────────┐
│ Ingestion Lambda                      │
│  - batchSize: 100                     │
│  - maxBatchingWindow: 5s              │
│  - reservedConcurrency: 50            │
│                                       │
│  1. Parse + validate                  │
│  2. Group by edge type                │
│  3. UNWIND batch upsert to Neptune    │
│  4. Failure → DLQ (SQS)              │
└────────┬─────────────────────────────┘
         ▼
┌──────────────────────────────────────┐
│ Neptune writer endpoint               │
│  - IAM database auth                 │
│  - Batch upsert (UNWIND)             │
└──────────────────────────────────────┘
```

## Event schema

Standard event format (sent by the frontend / app):

```json
{
  "event_id": "evt-abc-123",
  "event_type": "PURCHASED",                    // BOUGHT, VIEWED, CART, RATED, ...
  "timestamp": 1700000000000,                   // ms
  "user_id": "u-456",
  "item_id": "i-789",
  "weight": 5.0,                                // standard weight per behavior (or injected by Lambda)
  "properties": {
    "qty": 2,
    "duration_sec": 180,                        // for VIEWED
    "completion_ratio": 0.85,                   // for WATCHED
    "rating_value": 4                           // for RATED
  },
  "tenant_id": "tenant-A"                       // for multi-tenant
}
```

## Ingestion Lambda — Python

### `backend/lambdas/ingest/handler.py`

```python
"""
Kinesis → Neptune batch ingestion.
- Parse + validate
- Group by edge type
- UNWIND batch upsert
- DLQ on failure
"""
import os
import json
import base64
import logging
from collections import defaultdict
from neptune_client import NeptuneOpenCypherClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

NEPTUNE_ENDPOINT = os.environ['NEPTUNE_ENDPOINT']
NEPTUNE_PORT = os.environ['NEPTUNE_PORT']
BATCH_SIZE = int(os.environ.get('BATCH_SIZE', 100))

# ── standard weight per behavior
EDGE_WEIGHTS = {
    'PURCHASED': 5.0,
    'BOUGHT': 5.0,
    'LIKED': 4.0,
    'CART': 3.0,
    'WATCHED': 2.0,         # actual weight = base × completion_ratio
    'VIEWED': 1.0,           # actual weight is duration-based
    'CLICKED': 0.5,
    'RATED': None,           # rating_value as-is
}

# ── per-industry vertex/edge label override (env var or separate config)
INDUSTRY = os.environ.get('INDUSTRY', 'ecommerce')


def lambda_handler(event, context):
    """
    Kinesis records batch → Neptune upsert.
    """
    records = event['Records']
    logger.info(f"Received {len(records)} records")

    # Parse + group by edge type
    by_edge_type = defaultdict(list)
    failed = []

    for record in records:
        try:
            payload = json.loads(base64.b64decode(record['kinesis']['data']))
            edge_type = payload['event_type'].upper()

            # Compute final weight
            weight = compute_weight(edge_type, payload)
            payload['weight'] = weight

            by_edge_type[edge_type].append(payload)
        except Exception as e:
            logger.error(f"Failed to parse record: {e}")
            failed.append({'record': record, 'error': str(e)})

    # Upsert to Neptune (per edge type, batched)
    neptune = NeptuneOpenCypherClient(NEPTUNE_ENDPOINT, NEPTUNE_PORT)
    for edge_type, events in by_edge_type.items():
        for batch in chunked(events, BATCH_SIZE):
            try:
                upsert_edges(neptune, edge_type, batch)
            except Exception as e:
                logger.error(f"Upsert failed for {edge_type}, batch size {len(batch)}: {e}")
                failed.extend([{'record': e, 'error': str(e)} for e in batch])

    if failed:
        # partial retry via Lambda batchItemFailures
        return {
            'batchItemFailures': [
                {'itemIdentifier': r['record']['kinesis']['sequenceNumber']}
                for r in failed if isinstance(r['record'], dict) and 'kinesis' in r['record']
            ]
        }

    return {'processed': len(records) - len(failed), 'failed': len(failed)}


def compute_weight(edge_type: str, event: dict) -> float:
    """Compute weight per behavior (standardized in the Lambda)."""
    base = EDGE_WEIGHTS.get(edge_type)

    if edge_type == 'RATED':
        return float(event['properties'].get('rating_value', 3))

    if edge_type == 'VIEWED':
        # map duration 0-300s to weight 0.5-2.0
        dur = event['properties'].get('duration_sec', 30)
        return base * min(dur / 60.0, 2.0)        # 30s = 0.5, 60s = 1.0, 120s = 2.0

    if edge_type == 'WATCHED':
        ratio = event['properties'].get('completion_ratio', 0.5)
        return base * ratio                       # 80% completion = 1.6

    if edge_type == 'CART':
        qty = event['properties'].get('qty', 1)
        return base * (1 + 0.1 * (qty - 1))       # increases with quantity

    return base or 1.0


def chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def upsert_edges(neptune, edge_type: str, events: list[dict]):
    """
    UNWIND batch upsert. Idempotent via MERGE.
    """
    # query matching the per-industry schema — this example is e-commerce
    if edge_type in ('BOUGHT', 'PURCHASED'):
        query = """
            UNWIND $events AS e
            MERGE (u:User {id: e.user_id})
              ON CREATE SET u.firstSeenAt = e.timestamp
            MERGE (i:Item {id: e.item_id})
            MERGE (u)-[r:BOUGHT {at: e.timestamp}]->(i)
              ON CREATE SET r.weight = e.weight
              ON MATCH SET r.weight = r.weight + e.weight
        """
    elif edge_type == 'VIEWED':
        query = """
            UNWIND $events AS e
            MERGE (u:User {id: e.user_id})
            MERGE (i:Item {id: e.item_id})
            MERGE (u)-[r:VIEWED {at: e.timestamp}]->(i)
              ON CREATE SET r.weight = e.weight, r.durationSec = e.properties.duration_sec
              ON MATCH SET r.weight = r.weight + e.weight
        """
    elif edge_type == 'CART':
        query = """
            UNWIND $events AS e
            MERGE (u:User {id: e.user_id})
            MERGE (i:Item {id: e.item_id})
            MERGE (u)-[r:CART {at: e.timestamp}]->(i)
              ON CREATE SET r.weight = e.weight, r.qty = e.properties.qty
              ON MATCH SET r.weight = r.weight + e.weight
        """
    elif edge_type == 'RATED':
        query = """
            UNWIND $events AS e
            MERGE (u:User {id: e.user_id})
            MERGE (i:Item {id: e.item_id})
            MERGE (u)-[r:RATED]->(i)
              SET r.value = e.properties.rating_value, r.at = e.timestamp, r.weight = e.weight
        """
    else:
        # Generic fallback
        query = f"""
            UNWIND $events AS e
            MERGE (u:User {{id: e.user_id}})
            MERGE (i:Item {{id: e.item_id}})
            MERGE (u)-[r:{edge_type} {{at: e.timestamp}}]->(i)
              ON CREATE SET r.weight = e.weight
              ON MATCH SET r.weight = r.weight + e.weight
        """

    neptune.run(query, events=events)
```

### Neptune client (with SigV4)

```python
# backend/shared/neptune_client.py
import requests
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


class NeptuneOpenCypherClient:
    """SigV4-signed HTTP client for Neptune openCypher endpoint."""

    def __init__(self, endpoint: str, port: str, region: str = None):
        self.url = f"https://{endpoint}:{port}/openCypher"
        session = boto3.Session()
        self.region = region or session.region_name
        self.credentials = session.get_credentials()

    def run(self, query: str, **params) -> list[dict]:
        body = {'query': query, 'parameters': params}
        request = AWSRequest(
            method='POST',
            url=self.url,
            data=json.dumps(body),
            headers={'Content-Type': 'application/json'},
        )
        SigV4Auth(self.credentials, 'neptune-db', self.region).add_auth(request)
        prepared = request.prepare()

        resp = requests.post(self.url, headers=dict(prepared.headers), data=prepared.body, timeout=30)
        resp.raise_for_status()
        return resp.json().get('results', [])
```

## Batch / Bulk Loader pipeline

100K+ vertices or initial load — the Bulk Loader API is 100x faster than streaming.

```
┌──────────────────┐
│ Batch ETL job    │
│ (Glue / EMR /    │
│  Lambda)         │
└────────┬─────────┘
         ▼
┌──────────────────┐
│ S3 (CSV / JSON)  │
│  - vertices.csv  │
│  - edges.csv     │
└────────┬─────────┘
         ▼
┌──────────────────┐
│ Bulk Loader API  │
│ (Neptune POST)   │
└──────────────────┘
```

### Bulk Loader trigger Lambda

```python
# backend/lambdas/bulk-load/handler.py
import os
import requests
import boto3
from neptune_client import NeptuneOpenCypherClient

neptune_endpoint = os.environ['NEPTUNE_ENDPOINT']
loader_role_arn = os.environ['BULK_LOADER_ROLE_ARN']
region = os.environ['AWS_REGION_NAME']


def lambda_handler(event, context):
    """
    Trigger bulk load job from S3.
    event = { source_s3_uri, format ('csv'|'opencypher') }
    """
    body = {
        'source': event['source_s3_uri'],
        'format': event.get('format', 'csv'),
        'iamRoleArn': loader_role_arn,
        'region': region,
        'failOnError': 'TRUE',
        'parallelism': 'MEDIUM',
        'updateSingleCardinalityProperties': 'FALSE',
        'queueRequest': 'TRUE',                      # queue if another load job is in progress
    }

    resp = requests.post(
        f"https://{neptune_endpoint}:8182/loader",
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    load_id = resp.json()['payload']['loadId']

    return {'statusCode': 200, 'loadId': load_id}


def check_load_status(load_id: str):
    """Poll bulk load progress."""
    resp = requests.get(
        f"https://{neptune_endpoint}:8182/loader/{load_id}?details=true",
        timeout=10,
    )
    return resp.json()
```

## Vertex / Edge schema enforcement (validation)

The Lambda validates the schema before ingestion:

```python
from typing import Literal

VALID_VERTEX_LABELS = {'User', 'Item', 'Category', 'Brand', 'Segment'}    # industry-specific
VALID_EDGE_TYPES = {'BOUGHT', 'VIEWED', 'CART', 'RATED', 'IN_CATEGORY', 'HAS_BRAND'}


def validate_event(event: dict) -> bool:
    """Schema validation — invalid events go to DLQ."""
    if 'event_type' not in event or event['event_type'].upper() not in VALID_EDGE_TYPES:
        raise ValueError(f"Invalid event_type: {event.get('event_type')}")
    if 'user_id' not in event or 'item_id' not in event:
        raise ValueError("Missing user_id or item_id")
    if 'timestamp' not in event:
        raise ValueError("Missing timestamp")
    return True
```

## Throughput planning

| Event/sec | Shard | Lambda concurrent | Neptune writer load |
|---|---|---|---|
| 100/s   | 1 shard      | 5     | low |
| 1K/s    | 2 shards     | 20    | medium |
| 10K/s   | 10 shards    | 100   | high — RDS r6g.large sufficient |
| 100K/s  | 100 shards   | 500   | Provisioned r6g.4xlarge+ required |

→ Throughput estimate: production = 1M event/d ≈ 12 events/sec ≈ 1 shard is sufficient.

## Real-time vs batch trade-off

| Scenario | Real-time | Batch |
|---|---|---|
| Initial load (1M+ vertex) | ❌ slow | ✅ Bulk Loader |
| Real-time recommendation update | ✅ | ❌ |
| Reflect user behavior in recommendations within 5 min | ✅ | ❌ |
| Cost-conscious dev | ❌ Kinesis $11/shard | ✅ EventBridge schedule + S3 |
| ~1K new users daily | hybrid OK | OK |

→ Hybrid recommended: initial bulk load + real-time afterwards.

## Pitfall avoidance

| Pitfall (`shared/reference/constraints.md`) | Handling |
|---|---|
| #5 throughput | batch_size=100, UNWIND, DLQ |
| #6 IAM auth | SigV4 client wrapper |
| #14 Cypher injection | parameterized query |
| #18 timestamp consistency | all unix milliseconds |
| #16 throttling | Lambda batchItemFailures (Kinesis partial retry) |
| Connection pool exhaustion | reservedConcurrency: 50 |
| Schema drift | validate_event() |

## DLQ handling

```python
# DLQ message sample structure
{
  "record": { "kinesis": { "data": "...", "sequenceNumber": "..." } },
  "error": "Invalid event_type: PURCHASE_X",
  "first_failed_at": 1700000000000
}
```

→ A separate DLQ Lambda analyzes periodically (1h cron) + CloudWatch alert. Auto-expires after 14 days.

## Initial load procedure (after deploy)

```bash
# 1. Generate CSV (e.g., Glue ETL or direct export)
# vertices.csv, edges.csv → S3

# 2. Call the bulk loader
curl -X POST https://${NEPTUNE_ENDPOINT}:8182/loader \
  -H "Content-Type: application/json" \
  -d '{
    "source": "s3://my-bucket/vertices.csv",
    "format": "csv",
    "iamRoleArn": "arn:aws:iam::123:role/${PROJECT}-bulk-loader-role",
    "region": "ap-northeast-2",
    "failOnError": true,
    "parallelism": "MEDIUM"
  }'

# 3. Check progress
LOAD_ID="<from response>"
curl https://${NEPTUNE_ENDPOINT}:8182/loader/${LOAD_ID}?details=true
```

## CloudWatch metrics

Default Neptune CloudWatch metrics + additional:

```python
# Lambda publishes custom metrics
import boto3
cloudwatch = boto3.client('cloudwatch')

cloudwatch.put_metric_data(
    Namespace='GraphPersonalization',
    MetricData=[
        {'MetricName': 'IngestRate', 'Value': len(processed_records), 'Unit': 'Count'},
        {'MetricName': 'IngestFailures', 'Value': len(failed), 'Unit': 'Count'},
        {'MetricName': 'IngestLatencyMs', 'Value': elapsed_ms, 'Unit': 'Milliseconds'},
    ],
)
```

→ View IngestRate + Neptune CPU + Lambda duration together on a dashboard.
