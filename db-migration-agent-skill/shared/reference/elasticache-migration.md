# Cache Migrations — Redis OSS / Valkey / Memcached → Amazon ElastiCache

> **Load at Phase 1–2 as soon as the source is Redis OSS, Valkey or Memcached** (or an
> ElastiCache Redis OSS cluster moving to Valkey), and again at **Phases 6–8**. A cache
> engagement runs the **same** phases, gates, authorizations, dashboard cadence, rehearsal
> minimum, soak and rollback rules as a database engagement; this file supplies the
> cache-specific content for each of them. Where it says "same as SQL", the SQL procedure
> in the referenced file applies unchanged.
>
> **Facts current as of 2026-10-28** (engine versions, the 2026-10-20 Serverless online
> migration GA). Versions, families and features churn — verify live before committing a
> target: `aws elasticache describe-cache-engine-versions --engine valkey|redis|memcached`
> (read-only). Items marked **UNVERIFIED** are hypotheses: test them in the rehearsal or
> state them as risks; never present them as facts at a gate.

## Contents

1. Scope and routing · 2. Discovery deltas · 3. Read-only assessment · 4. Blocker catalog ·
5. Method decision matrix · 6. Target choice · 7. Execution runbooks · 8. Validation ·
9. Soak · 10. Client discovery and repoint · 11. Cutover and rollback · 12. Memcached ·
13. Redis OSS → Valkey licensing/compatibility · 14. Korean context · 15. Cost/IAM ·
Sources

---

## 1. Scope and routing

| In scope | Notes |
|---|---|
| Self-managed **Redis OSS** (≤ 7.2.x) or **Valkey** on EC2, on-premises or another cloud's VMs → **ElastiCache for Valkey** or **ElastiCache for Redis OSS**, node-based or Serverless | Valkey is the default recommendation (§6) |
| Self-managed **Memcached** → **ElastiCache for Memcached** (node-based or Serverless) | No data migration exists; warm-up method (§12) |
| **ElastiCache Redis OSS → ElastiCache Valkey** engine change (in place) | §7.7 |
| **Redis Community Edition 7.4+/8.x** sources (not "Redis OSS" — source-available licenses) | Logical key-by-key paths only (§4 V1) |
| Another cloud's **managed** Redis/Memcached (no OS, usually no `PSYNC`) | Logical paths (§5 C4) — §14 for Korean providers |

**Out of scope** (hard constraint 10 still applies): a self-managed target ("Redis on EC2",
a Redis container on ECS/EKS) — stop at Phase 0 exactly as for SQL. **Amazon MemoryDB is out
of scope as a target** for this skill: the verified facts behind this file cover ElastiCache
only, MemoryDB is not a source for either ElastiCache online-migration path, and a
"durable primary datastore" requirement has an in-scope answer to evaluate first —
ElastiCache Valkey 9.0's durability option (Multi-AZ transaction log, sync/async). If the
customer explicitly wants MemoryDB, say this skill has no MemoryDB provisioning,
validation or cutover content, offer what still transfers (assessment, client discovery,
`cache_compare.py`, the gates and record), and do not improvise MemoryDB runbooks.
**AWS DMS is never a cache-to-cache tool**: Redis/Valkey/ElastiCache is not a DMS source
(Redis OSS is a DMS *target* only, for relational/NoSQL → Redis hashes).

**How each phase maps here**

| Phase | Cache-specific content |
|---|---|
| 1 Discovery | §2 + template `shared/templates/discovery-questions-cache.md` → working file `discovery-questions.md` (same #3–#18 numbering) |
| 2 Assessment | §3 commands, §4 blocker catalog, sizing in §6 |
| 3 Method | §5 matrix (top-down, first match) + rollback gate §11 |
| 4–5 Provision | §6 target choice + `cdk-stacks.md` §ElastiCache |
| 6 Execute | §7 runbooks (+ `execution-control.md` supervision, rehearsal §7.9) |
| 7 Validate | §8 + `shared/scripts/cache_compare.py` |
| 7.5 Clients | §10 |
| 7.7 Soak | §9 |
| 8 Cutover | §11 |
| 9 Post | §11.4 |

---

## 2. Discovery deltas (#3–#18 for caches)

Phase 1's two routing questions are unchanged in form. **#1** additionally records the
engine **and exact version** (`INFO server` → `redis_version`, `valkey_version` when
present), deployment shape (standalone, primary/replica, Sentinel, Redis Cluster, a proxy
such as twemproxy/Envoy in front), and whether it is a managed product. **#2** additionally
records Valkey vs Redis OSS, node-based vs Serverless, and cluster mode — usually left
open until §6 with a recommended default. Once #1 shows a cache engine, generate the
working file **`discovery-questions.md`** from the **cache template**
`shared/templates/discovery-questions-cache.md` instead of the SQL template — same filename,
so the resume re-read, chat backfill and GATE 1 steps in SKILL.md Phase 1 apply unchanged.
It keeps **every number's slot**
so plan rows, `#9`, `#16c/#16d` and the GATE 1 wording stay valid:

| # | SQL meaning | Cache variant (what changes) |
|---|---|---|
| 3 | DB size | `used_memory`/`used_memory_peak` per node, key count per db, **largest keys** (3a) |
| 4 | Downtime + production status | Same, plus **4c: is it a cache (rebuildable from a backing store) or the primary datastore?** — decides the whole method/rollback family |
| 5 | RPO on rollback | Asked as "what is lost if the cache comes up empty, or loses writes since cutover?" — for a pure cache the RPO is often "nothing, at the cost of a cold-cache load spike" |
| 6 | Bandwidth | Same (RDB upload, RedisShake stream, online-migration replication traffic) |
| 7 | Schema objects | **Server-side features**: Lua (`EVAL`/`EVALSHA`), Functions (7.0+), modules, keyspace notifications, pub/sub (incl. sharded), streams + consumer groups, blocking commands, `MULTI`/`WATCH` |
| 8 | App modifiability | **Client libraries + versions, cluster-awareness, TLS support** (8a) — CMD → CME or adding TLS/RBAC is a client change |
| 9 | How the app finds the host | **Unchanged** (incl. writable repoint/revert access). Add: endpoint form (IP, DNS, Sentinel master name, proxy) |
| 10 | CDC consumers | Replicas of the source, keyspace-notification consumers, other sync jobs, scripts reading `dump.rdb` |
| 11 | Compliance | Same + in-transit TLS / AUTH / RBAC mandates |
| 12 | Korean security appliances | Usually none on caches — ask; DB access-control appliances rarely cover Redis |
| 13 | Multiple DBs on host | **Logical databases in use** (`SELECT n`) **and** multiple instances on one host (ports) — CME targets have db0 only |
| 14 | Cross-region/account | Same |
| 15 | KMS | At-rest encryption + key (node-based: changing either means replacement; Serverless: always encrypted) |
| 16 | Engagement parameters | Same four (16a–16e). 16c soak and 16d rollback have cache options (§9, §11) |
| 17 | Third-party tools | Monitoring agents/exporters, Sentinel, proxies, backup scripts, HA tooling |
| 18 | Test suite | Same |

Cache-only facts not covered by a numbered item go on the closest item's sub-letter
(3a largest keys, 4c data role, 7a eviction policy and persistence, 8a client libraries,
11a AUTH/ACL/TLS state) — never renumber.

---

## 3. Read-only assessment (Phase 2)

Credentials stay out of argv (hard constraint 4): `redis-cli`/`valkey-cli` read the
password from **`REDISCLI_AUTH`** (both CLIs; verified with `--user` for an ACL user on
redis 7.2 and valkey 8.1). Run against a **replica** where one exists — `--bigkeys`,
`--memkeys` and `MEMORY USAGE` scans add load. Prefer a dedicated assessment user, e.g.
`ACL SETUSER cache_assess on >… ~* &* -@all +@read +info +ping +client|list +cluster|slots
+cluster|info +cluster|nodes +slowlog|get +latency|latest +memory|stats +memory|usage
+module|list +function|list +config|get -keys` — creating it is a source write (A2).

```bash
H=10.0.1.20 P=6379                         # each node of a cluster, primaries AND replicas
export REDISCLI_AUTH=...                   # fetched on-host (source-assessment.md §Credential Handling)
C="redis-cli -h $H -p $P"                  # add --user <acl-user>, --tls --cacert ca.pem as needed
$C INFO server        # redis_version / valkey_version / server_name, os, arch, uptime, tcp_port
$C INFO memory        # used_memory(_peak), maxmemory, maxmemory_policy, mem_fragmentation_ratio
$C INFO persistence   # rdb_last_bgsave_status, aof_enabled, rdb_last_cow_size (fork cost)
$C INFO replication   # role, connected_slaves, master_repl_offset
$C INFO keyspace      # dbN:keys=..,expires=.. — per logical db; cross-check: $C -n N DBSIZE
$C INFO clients       # connected_clients, blocked_clients, pubsub_clients (7.x)
$C INFO stats         # instantaneous_ops_per_sec, keyspace_hits/misses, evicted_keys, expired_keys,
                      # total_connections_received (delta = connection churn), pubsub_channels
$C INFO commandstats  # which command families are actually used (EVAL*, FCALL, *SUBSCRIBE, BLPOP,
                      # MULTI, KEYS, MOVE/SWAPDB, SELECT, cross-slot MGET/MSET...) → §4 rows
$C INFO cluster; $C CLUSTER INFO; $C CLUSTER SHARDS   # cluster sources (SHARDS: Redis 7.0+)
$C CONFIG GET maxmemory; $C CONFIG GET maxmemory-policy; $C CONFIG GET databases
$C CONFIG GET notify-keyspace-events; $C CONFIG GET appendonly; $C CONFIG GET save
$C CONFIG GET protected-mode; $C CONFIG GET bind; $C CONFIG GET port; $C CONFIG GET tls-port
#   Name parameters explicitly. NEVER `CONFIG GET *` or `CONFIG GET requirepass|masterauth` —
#   they return secrets. AUTH presence: an unauthenticated `PING` answering NOAUTH proves it.
$C MODULE LIST        # any module = §4 row M1
$C FUNCTION LIST      # 7.0+ server-side libraries (not carried by every method)
$C ACL USERS          # names only (6.0+); ACL LIST shows hashes — keep it out of the record
$C MEMORY STATS
$C --bigkeys -i 0.01  # largest key per type (SCAN-based, throttled); --memkeys for bytes
$C LATENCY LATEST; $C SLOWLOG GET 128          # baseline for Phase 9 comparison
$C CLIENT LIST        # client inventory seed — §10 (repeat over a business cycle)
# Renamed commands: check the config FILE for rename-command lines (not exposed via CONFIG GET).
```

**Memcached** (`printf 'stats\r\n' | nc -q1 $H 11211`, or `openssl s_client` for TLS):
`stats` (version, curr_items, bytes, limit_maxbytes, evictions, get_hits/get_misses,
curr_connections, total_connections), `stats settings` (item_size_max, maxconns,
binding_protocol, udpport, sasl, ext_* = extstore), `stats slabs` / `stats items` (item
size distribution vs the target's `max_item_size`), and `stats conns` where the server
supports it (client addresses).

Record in the plan, per node: version, memory peak, key counts per db, largest keys,
eviction policy, persistence, cluster layout (shards, slots), AUTH/ACL/TLS state, modules,
functions, command-family usage, connection counts. Populate the dashboard Phase 2 steps
the same way as SQL (blocker scan, inventory/sizing, replication readiness, throughput,
baseline) and `migration_objects.keyspaces` with `status:"pending"` per db.

---

## 4. Blocker and adjustment catalog

Blockers stop method selection until resolved (or the method changes); adjustments need a
recorded decision.

| # | Condition (how detected) | Why it matters | Resolution |
|---|---|---|---|
| V1 | **Source is Redis CE 7.4+ / 8.x** (`redis_version` ≥ 7.4, no `valkey_version`) | Valkey reads RDB/AOF compatible with Redis OSS 7.2; "RDB files produced by Redis CE 7.4 and later are not compatible" (RDB 12+ is reserved for foreign formats). ElastiCache does not expose `rdb-version-check` (absent from the `valkey8`/`valkey9` default parameter groups, checked live 2026-10-28). Both online-migration paths are replication-based: node-based **UNVERIFIED** for 7.4+ (expect failure); Serverless source range ends at Redis OSS 7.2 | **Logical** path only: RedisShake (reads Redis 2.8–8.4.x) or dual-write / rebuild. Inventory 7.4+/8-only features first: hash-field TTL (`HEXPIRE`; Valkey has it from 9.0), Redis 8 built-in JSON/search/time series/probabilistic types (row M1) |
| V2 | **Valkey 9 snapshot going to Valkey 8 / Redis OSS** | Valkey 9.0 writes RDB 80 — older engines can't load it | One-way: snapshot-based rollback from a Valkey 9 target is impossible; plan rollback per §11 |
| M1 | **Modules loaded** (`MODULE LIST` non-empty: RedisJSON, RediSearch, RedisBloom, RedisTimeSeries, custom) | ElastiCache does not load customer modules. It does have **native JSON** (its own implementation) and **Bloom filters on Valkey 8.1+** ("not RDB compatible with other non-Valkey based bloom offerings"), vector search on Valkey 8.2 (valkey-search), full-text search on 9.0. RDB compatibility of module-typed values with these is **UNVERIFIED** — assume none | Per type: re-create through the native commands (application/backfill job), or keep that workload elsewhere. Time series/custom modules: no ElastiCache equivalent → redesign. `cache_compare.py` reports module-typed keys as `unsupported_type` (null, needs review) |
| D1 | **Logical dbs other than 0 in use** (`INFO keyspace` db1+, `SELECT` in commandstats) and a **cluster-mode** target (CME node-based, or any Serverless) | "When cluster mode is enabled, only database 0 is available" — except Valkey 9.0+ `cluster-databases` (Serverless 1024 on 9.0+) | CMD node-based target (keeps `databases`, default 16); or Valkey 9 with `cluster-databases`; or consolidate (key prefixes) as an app change; or one cache per db. Node-based online migration additionally requires the **same `databases` count** on both sides |
| X1 | **Multi-key commands / Lua / `MULTI` touching several keys** with a CME target | Allowed only when all keys hash to one slot → `CROSSSLOT` errors | Hash tags `{tag}` in key names (an app + data change — re-keying), or a CMD target. Detect: commandstats (MGET/MSET/DEL n>1/SUNIONSTORE/EVAL with several keys), code review |
| C1 | **Client library is not cluster-aware** (CME/Serverless target) | Must follow `MOVED`/`ASK`; Serverless is always cluster mode | Upgrade/configure the client (cluster mode on) — rehearse; or CMD node-based target |
| T1 | **Client can't do TLS** (or can't do ACL `AUTH user pass`) | Serverless: TLS always on, RBAC only. Node-based: AUTH token and RBAC both require TLS. Node-based Memcached TLS is creation-time only (1.6.12+); Serverless Memcached TLS mandatory | Upgrade the client; for node-based you may keep TLS off (no AUTH possible then) only with an explicit compliance decision (#11) |
| R1 | **Command families ElastiCache restricts** used by apps/ops tooling: `CONFIG`, `SAVE`/`BGSAVE`, `BGREWRITEAOF`, `DEBUG`, `MIGRATE`, `REPLICAOF`/`SLAVEOF`, `SYNC`/`PSYNC`, `SHUTDOWN`, `ACL SETUSER/DELUSER/LOAD/SAVE`, most `CLUSTER` admin verbs | Calls fail on the target | Replace with API equivalents (parameter groups, snapshots, users/user groups); remove from runbooks/cron. **Serverless additionally lacks** `KEYS`, `MONITOR`, `CLIENT LIST/KILL`, `MEMORY …`, `OBJECT …`, `SLOWLOG`, `FUNCTION`/`FCALL`, `PSUBSCRIBE`, `MOVE`, `SWAPDB`, `WAIT`; `FLUSHALL/FLUSHDB` flush the whole cache and can't be in a transaction |
| K1 | **Keyspace notifications used** (`notify-keyspace-events` non-empty, `__keyspace@` / `__keyevent@` subscribers) | Serverless: **not supported**. Node-based: the default parameter group has it empty | Node-based target with the same value in a custom parameter group; Serverless → redesign |
| E1 | **Eviction policy differs** — source `noeviction` (Redis default) vs ElastiCache default **`volatile-lru`** (node-based `default.valkey8` checked live; Serverless fixed `volatile-lru`, not modifiable) | A primary-datastore workload can silently lose TTL'd keys under memory pressure; a full cache returns errors instead of evicting (or vice versa) | Set `maxmemory-policy` in the custom parameter group to the source's value; Serverless can't change it — record the acceptance or choose node-based |
| P1 | **Primary-datastore durability expectations** (AOF `appendfsync always/everysec`, RDB saves as the backup) | ElastiCache replication is asynchronous unless Valkey 9 durability is configured; backups are snapshots | Decide at GATE 2: Valkey 9.0 durability (sync/async), snapshot retention, Multi-AZ — state RPO plainly |
| S1 | **Serverless limits**: key name > 4 KiB, slot > 32 GiB, request > 39,999 args, Lua script > 4 MiB, `maxclients` 65,000, `proto-max-bulk-len` 512 MiB, 5,000 GiB/cache | Hard errors; a > 4 KiB key **permanently fails** Serverless online migration | `--bigkeys`/key-length scan before choosing Serverless; node-based otherwise |
| N1 | **Node-based online migration prerequisites unmet**: source AUTH enabled, `protected-mode yes`, `bind` excludes the VPC, `sync`/`psync`/`info`/`config`/`command`/`cluster` renamed, shard count differs, source not on EC2, target TLS on / not Multi-AZ / data tiering / Global Datastore | `test-migration`/`start-migration` refuses or fails | Different row in §5, or an A2-authorized source change (e.g. disabling AUTH temporarily is a security decision — present it, never assume it) |
| N2 | **Serverless online migration prerequisites unmet**: source not cluster mode, Valkey 9 source, > 32 GB `used_memory` per node, > 166 shards, incomplete slot coverage, `PSYNC`/`REPLCONF` renamed, target not empty / public endpoint / not IPv4 / subnets missing in an AZ | `prepare-migration` fails (rolled back, event names the reason) | Different row in §5 |
| L1 | **Lua scripts loaded by SHA** (`EVALSHA`) | Serverless online migration does not replicate the script cache (`NOSCRIPT`); no method copies the script cache reliably | App falls back to `EVAL` on `NOSCRIPT` (most clients do) or re-`SCRIPT LOAD` on the target before cutover — test it |
| PS1 | **Pub/sub / blocking commands** | Messages are not data — nothing is migrated; subscribers and blocked clients are disconnected at cutover | Clients reconnect and resubscribe — verify in rehearsal; Serverless uses sharded pub/sub internally |
| ST1 | **Streams with consumer groups** | Group state (PEL, last-delivered-id) travels with replication/RDB, but a logical re-write (RedisShake scan) may not carry it — **UNVERIFIED** per tool version | Prefer replication-based methods; validate with `cache_compare.py` (compares group names + last-delivered-id; PEL not compared) |
| DT1 | **Target wants data tiering (r6gd)** | No online migration, no S3 export of backups | Seed by RDB, or migrate to r6g/r7g first |
| MC1 | **Memcached**: binary protocol / UDP / SASL in use, items larger than 1 MiB, extstore (`ext_*`) | Serverless Memcached: no binary protocol, TLS required (no UDP); SASL is not documented for ElastiCache (**UNVERIFIED** — assume none); `max_item_size` default 1,048,576 B; no extstore | Text protocol + TLS client; raise `max_item_size` in a node-based parameter group (restart) or shrink items; `stats slabs` sizes |
| EOL | **ElastiCache Redis OSS 4.x/5.x** source or target | Paid **Extended Support since 2026-02-01**; v6 standard support ends 2027-01-31 | Never land on 4/5/6 as a new target; prefer Valkey (§6) |

---

## 5. Method decision matrix

**Front gates (before the rows):** (1) **Version gate** — a Redis CE 7.4+/8 source (V1)
disqualifies C2, C3 and C5; a Valkey 9 source disqualifies C2. (2) **Data-role gate** — #4c
"primary datastore" disqualifies C6. (3) **Rollback gate** — every row's rollback is in
§11; ElastiCache cannot replicate *out*, so "reverse replication" (#16d A) is never
available after cutover — revisit #5/#16d before GATE 2 exactly as
`method-selection.md`'s rollback-direction gate requires. (4) **Authorization gate** —
any row needing a source change (ACL user creation, `BGSAVE`, temporary AUTH removal,
`notify-keyspace-events`, `REPLICAOF` on a scratch node) needs its own A2 block.

Walk top-down; take the **first** row whose conditions all hold. Present the rejected rows
and why at GATE 2.

| # | Source | Target | Downtime | Conditions (exact) | Method | Limits / notes |
|---|---|---|---|---|---|---|
| C1 | ElastiCache **Redis OSS** (node-based or Serverless) | ElastiCache **Valkey**, same cache | Seconds | Redis OSS 5.0.6+ for minimal downtime (< 5.0.6 / v4: 30–60 s failover during DNS propagation); single-node CMD must first become a replication group; Valkey parameter group with the same static params; AWS CLI ≥ 1.35.2 / 2.18.2 | **In-place engine change** `modify-replication-group --engine valkey` (§7.7) — the engine change on the production cache **is the cutover** (Phase 8); Phase 6 rehearses it on a snapshot-restored clone | Endpoint DNS unchanged, node IPs change. Rollback **only Valkey 7.2 → Redis OSS 7.1**; none from 8.x/9.x |
| M1 | Memcached (any) | ElastiCache Memcached | Any | — | **Provision → warm → repoint** (§12) | No data path exists (no backup/export for node-based Memcached) |
| C6 | Redis OSS / Valkey / CE, **pure cache** (#4c rebuildable, #5 "empty cache is acceptable") | Any ElastiCache | Any | Backing store can absorb the cold-cache load (estimate from `keyspace_hits` rate × backing-query cost; agree a ceiling), or a warm-up job exists | **Cold start / warm-up** (§7.6), optionally dual-write for a warm start | Cheapest and lowest-risk; rollback = repoint back (watch stale data, §11) |
| C2 | Self-managed **Valkey 7.2–8.1 / Redis OSS 4.0–7.2 on EC2**, **cluster mode** | **Serverless Valkey** 7.2 / 8.1 / 9.0 (empty, IPv4, no public endpoint), version ≥ source | Zero (no client errors) | N2 all clear; ≤ 32 GB `used_memory` per node; ≤ 166 shards; full slot coverage; no resharding during migration; AUTH/ACL via Secrets Manager (CMK) + migration ACL user; TLS via `verify-ca` + PEM ≤ 5,000 chars; app users exist on source **and** target | **Serverless online migration** (§7.2) | App repoints to the migration endpoint **before** the sync — a separately authorized production stage (§7.2 Stage B). Production target **contents cannot be validated before cutover** (deferred validation, §7.2) — the customer accepts that at GATE 2. **No rollback after cutover.** Free migration usage for the first 180 days per account |
| C3 | Self-managed **Valkey / Redis OSS on EC2** (private IP, VPC-reachable) | **Node-based** Valkey, or Redis OSS ≥ 5.0.5 CMD / ≥ 5.0.6 CME | Near-zero (seconds at `complete-migration` + repoint) | N1 all clear: source **no AUTH**, `protected-mode no`, `bind` allows the nodes, same `databases`, admin/replication commands not renamed, all nodes same port, fork headroom; target **no in-transit encryption**, **Multi-AZ on**, **same shard count**, not Global Datastore, no data tiering | **Node-based online migration** (§7.1) | `start-migration` **deletes all target data**; target readable, not writable during migration. Validation runs with target TLS **off**; the TLS/RBAC transition and its client proof are cutover steps (§7.1, §11) |
| C4 | Any Redis/Valkey reachable over the network but C2/C3 don't fit — on-prem/other cloud, AUTH must stay on, CE 7.4+/8, shard mismatch, another provider's managed Redis | Any ElastiCache | Near-zero | Migration host in the target VPC with a path to the source; `PSYNC` allowed → `sync_reader`; no `PSYNC` (managed providers) → `scan_reader` (+ keyspace notifications for increments — a source config change) | **RedisShake** (MIT; v4.x; §7.4) | Not resumable — a restart re-syncs from scratch. Scan mode is not point-in-time. Third-party tool: the customer accepts it at GATE 2 |
| C5 | Redis OSS ≤ 7.2 / Valkey ≤ 8.x (or Valkey 9 → Valkey 9 target) | Node-based (S3 seed); Serverless (snapshot restore — S3-ARN acceptance **UNVERIFIED**) | Minutes–hours (writes paused from `BGSAVE` to repoint) **or** loss of the delta accepted | RDB version compatible (V1/V2); bucket in the target Region, DNS-compliant name, `{region}.elasticache-snapshot.amazonaws.com` read grant, SSE-KMS → key grant + **S3 Bucket Keys off**; CME seed needs `NodeGroupConfiguration` per shard; **a CMD target cannot be seeded from a CME RDB**; data must fit node memory | **RDB → S3 seed** (§7.3) | Point-in-time. Bandwidth math (Phase 2 throughput) applies to the upload. Can reshard / change node type / go CMD → CME on seed |
| C7 | Any, app team can change code | Any | Zero | App writes to both (source authoritative), reads switched at cutover | **Dual-write** (§7.5) — usually an **overlay** on C4/C5/C6 | The only lossless rollback after cutover; costs an app change and a reconciliation of writes that failed on one side |

No row fits (e.g. CE 8 with Redis-8-only types and no app change possible) → stop and
present the gap; never improvise.

---

## 6. Target choice (Phase 4)

- **Engine — Valkey by default.** ElastiCache Redis OSS tops out at **7.1** (there is no
  ElastiCache Redis OSS 7.2+; Redis 7.2-compatible = Valkey 7.2); Valkey is **20% cheaper
  node-based, 33% cheaper Serverless**, with a 100 MB Serverless minimum vs 1 GB, and reserved
  nodes carry over within the family. Stay on Redis OSS only for a documented reason
  (a validated client/vendor certification), and never on 4.x/5.x/6.x (EOL row).
- **Version.** Valkey **7.2** if an in-place rollback to Redis OSS 7.1 matters (C1);
  **8.1+** for Bloom and the new hash table (up to 20% less memory); **8.2** vector search;
  **9.0** durability, `cluster-databases`, hash-field TTL, public endpoints (Serverless);
  9.1 per-db ACL. **Valkey 9 is one-way for snapshots** (V2). Parameter groups don't cross
  majors: Valkey 8+ groups are incompatible with Redis OSS 7.2.4, Valkey 9 groups with
  Valkey 8.x; Valkey 8's `extended-redis-compatibility` makes `HELLO`/`INFO` report Redis
  7.2.4 for clients that sniff the version.
- **Node-based vs Serverless**

  | | Node-based | Serverless |
  |---|---|---|
  | Cluster mode | CMD (multi-db) or CME | Always CME (one virtual node), db0 only (9.0+: `cluster-databases`) |
  | Config | Parameter groups (eviction, keyspace events, `databases`) | Not modifiable (`volatile-lru`, no keyspace events) |
  | Auth | None, AUTH token, or RBAC (both need TLS) | RBAC only, TLS always |
  | Commands | Restricted set (R1) | R1 **plus** the Serverless list |
  | Online migration | C3 (no TLS target, same shards) | C2 (cluster-mode source, AUTH/TLS ok) |
  | Scaling | Node type / shards / replicas | ECPU + storage, doubles every 2–3 min; pre-scale minimum ECPU ≥ 60 min before a known peak |
  | Billing | Node-hours | GB-hours + ECPUs + snapshot storage; configured minimums billed even if idle |

- **CMD vs CME.** Keep CMD when D1/X1/C1 apply and one shard fits (node memory, single
  primary throughput); CME for horizontal scale. CMD → CME later is online
  (`ClusterMode disabled → compatible → enabled`) but still a client change.
- **Sizing (node-based).** Data `used_memory_peak` × growth ÷ (1 − `reserved-memory-percent`,
  default **25%**) per shard, plus fork headroom for snapshots/sync; replicas ≥ 1 with
  Multi-AZ (required for C3). Graviton node types (r7g/m7g/r8g) by default. **Data tiering**
  (r6gd, "over 60 percent savings" at max utilization) only when DT1's limits are acceptable.
- **Parameter group family** — verified live 2026-10-28 (`describe-cache-engine-versions`):
  `valkey7` (7.2), `valkey8` (8.0/8.1/8.2), `valkey9` (9.0/9.1), `redis7` (7.0/7.1),
  `redis6.x`, `redis5.0`, `redis4.0`, `memcached1.6` (1.6.6–1.6.22), `memcached1.5`,
  `memcached1.4`; defaults `default.<family>` and `default.<family>.cluster.on`. Always a
  custom group carrying the source's `maxmemory-policy`, `notify-keyspace-events`,
  `databases` (requires-reboot), and timeouts.
- **Security.** At-rest encryption + KMS key are replacement-only on node-based (decide
  now; #15). Prefer **RBAC** (`UserGroupIds` — "No interruption" to change) over an AUTH
  token: **adding** an `AuthToken` to an existing replication group is a **replacement**.
  VALKEY-engine user groups attach only to Valkey caches and need no `default` user;
  REDIS-engine groups must include a `default` user. RBAC passwords 16–128 chars, not
  `, " / @`. Min TLS 1.2 (Valkey 7.2+/Redis OSS 6+); no mTLS.

---

## 7. Execution runbooks (Phase 6)

Every long step runs under the tested supervisor (`execution-control.md`) or the remote-job
contract in SKILL.md §Dashboard cadence — record the job handle (migration/replication
group id, RedisShake PID/host), probe within 15 s, publish at least every 5 minutes.
Publish one step per shard/db where the method exposes it. Record the copy boundary
(RDB time, `start-migration` time, RedisShake start) in the plan.

### 7.1 Node-based online migration (C3)

1. **Prerequisite evidence** (each recorded): source `INFO server`/`CONFIG GET` values for
   N1; SG on the source allows inbound 6379 from the target's subnets; target created by
   CDK with `TransitEncryptionEnabled: false` explicitly, Multi-AZ, same shard count and
   `databases`; alarms live (incl. `MasterLinkHealthStatus`).
2. `aws elasticache test-migration --replication-group-id $RG --customer-node-endpoint-list "Address='10.0.1.20',Port=6379"`
   — validates without moving data. Fix every reported issue before continuing.
3. ⚠️ `start-migration` **wipes the target**. Publish `start-migration: in_progress`, then
   `aws elasticache start-migration --replication-group-id $RG --customer-node-endpoint-list "Address='10.0.1.20',Port=6379"`
   (one element even for CME; may point at a source replica with chained replication).
4. Progress loop: `describe-replication-groups` status `migrating`; target `INFO
   replication` (`master_link_status:up`, offsets); CloudWatch `MasterLinkHealthStatus`
   = 1; `CurrItems` per node. No percentage exists — report observed state + elapsed.
5. Verify: `cache_compare.py` with `"expect_replication": true` (§8) — key counts, sample,
   link. During live replication use a small `key_count_tolerance_pct` and record why.
   **Temporary validation boundary (C3 only):** the target must keep in-transit encryption
   off until `complete-migration`, so Phase 7 validates **data, parameters and topology over
   a non-TLS connection** and cannot yet prove TLS/RBAC logins on this target. Record that
   boundary in the GATE 3 evidence ("TLS/RBAC client proof deferred to cutover steps
   C3-T1…T4"); prove the TLS/RBAC client configuration itself on the rehearsal target
   (§7.9), where the transition is exercised end to end.
6. Hold until Phase 8. **`complete-migration` is the cutover**, not Phase 6 work:
   `aws elasticache complete-migration --replication-group-id $RG` ("typically completes
   within minutes"; event `Complete Migration successful for <id>`; `migrating → available`).
   `--force` "stops without ensuring that data is in sync" — never without an A4-scoped
   decision.
7. **The TLS/RBAC transition is part of the cutover runbook** (A4 / customer-executed,
   timings measured in rehearsal), never an untracked "later" change. Without replacement:
   **C3-T1** CDK `transitEncryption: true, transitEncryptionMode: 'preferred'` (both "No
   interruption") — plaintext and TLS clients both accepted; **C3-T2** repoint/flip every
   client to TLS and prove each one (§8 per-user proof: TLS handshake, `AUTH` once RBAC is
   attached, cluster mode); **C3-T3** `transitEncryptionMode: 'required'`; **C3-T4** attach
   `UserGroupIds` (requires TLS; whether `preferred` already allows it is **UNVERIFIED** —
   rehearse) and re-prove every application user. Whether clients first repoint in
   plaintext (shortest write pause, TLS minutes later) or wait for T1 (longer pause) is a
   GATE 4 choice with rehearsal timings. Never add `AuthToken` later (replacement).
   **Resume:** a broken link before completion → fix the cause, re-run `start-migration`
   (target wiped again — re-validate). **Abort:** clients never left the source; end with
   `complete-migration --force` on the unused target and discard/reseed it (inference — the
   target never served traffic).

### 7.2 Serverless online migration (C2)

1. Pre-create the empty Serverless Valkey cache (CDK, IPv4, no public endpoint, a subnet
   in every AZ ElastiCache Serverless uses in the Region), its RBAC user group with **every
   application user** that exists on the source (client AUTH is forwarded to the source
   during migration and answered by the target after cutover).
2. Source credentials (if AUTH/ACL): migration ACL user on **every source node** (A2 — a
   source write): `ACL SETUSER migration_user on '>…' -@all +ping +info +hello +cluster|nodes
   +cluster|shards +cluster|slots +cluster|info +acl|list +acl|dryrun +psync +sync +replconf
   +multi +exec +client|pause +client|kill +client|unpause`. Secret
   `{"username":…,"password":…}` (or `{"password":…}`) in Secrets Manager encrypted with a
   **customer-managed KMS key** (not the AWS-managed SM key), resource policy granting
   `secretsmanager:GetSecretValue` to `elasticache.amazonaws.com`, key policy grant; the
   caller also needs `GetSecretValue` + `kms:Decrypt`. Never change that user mid-migration.
3. `aws elasticache prepare-migration --serverless-cache-name $SC --customer-node-endpoint-list "Address='10.0.1.20',Port=6379" [--source-authentication-configuration SecretManagerConfig={SecretArn=$ARN}] [--source-tls-configuration SourceTlsMode=verify-ca,SourceTlsCertificate="$(cat ca.pem)"]`
   → state `preparing` → `ready-to-sync`; read the migration endpoint from
   `describe-serverless-caches` (`Migration` object). A failed prepare rolls itself back —
   read the cache events.
4. **Staged authorization and evidence.** The service proxies production traffic through
   the target endpoint *before* the data sync, and that endpoint serves **source** reads until
   cutover — so comparing through it compares the source with itself. Present this sequence
   at GATE 2; each stage needs its own recorded block, and the customer accepts the deferred
   validation explicitly:

   | Stage | What happens | Authorization | Evidence available at this stage |
   |---|---|---|---|
   | A — rehearsal (Phase 6) | Full C2 run on a **clone**: a scratch cluster-mode EC2 copy of the source + a scratch Serverless target; test client repointed, `start-migration`, `complete-migration`, then `cache_compare.py` between the **frozen clone source** and the clone target's endpoint; rollback repoint exercised | A3 (scratch infra) + A2 for any source-side read access | Real target-content evidence (on the clone), measured timings, client compatibility with the Serverless command set |
   | B — production proxy repoint (Phase 8, step 1) | `prepare-migration` on the production target, then **every** inventoried client repointed to the migration endpoint (traffic still served by the source) | Mode 2: customer; Mode 3: A4 part 1. Requires the client inventory complete (7.5) — a client left on the source bypasses the endpoint and makes the target diverge | Each client connected through the endpoint (`CurrConnections`/`NewConnections`, app health); command rejections show up now |
   | C — production sync + observation (soak, Phase 7.7) | `start-migration` (manual cutover) → `syncing` | Mode 2: customer (or agent under an explicit A-block, since no data is served from the target yet); Mode 3: A4 part 2 | **Not** target contents. Only: state stays `syncing`, no failure events, `CurrItems`/`BytesUsedForCache`/`NetworkBytesIn` on the target tracking source `DBSIZE`/`used_memory`, `ThrottledCmds` 0, app errors through the endpoint |
   | D — cutover (Phase 8, step 2) | `complete-migration` (cuts over at the next fully synced window) | Mode 2: customer; Mode 3: GATE 4 / A4 part 3 | Migration success event; then **deferred validation**: immediately after cutover run `cache_compare.py` source → target with `recheck_mismatches` — differences are either sync gaps or post-cutover writes and are **reviewed, not auto-passed**; per-user login proof on the target |

   GATE 3 for C2 therefore accepts **rehearsal (clone) evidence + Stage B/C operational
   evidence**, stated as such; it never claims production target contents were validated.
   Pipelining is serialized, the reader endpoint isn't supported, the client limit is
   10,000 connections, scripts must be re-`SCRIPT LOAD`ed **through the migration endpoint**,
   and writes that bypass the endpoint permanently diverge the target.
5. Stage C commands: `start-migration --serverless-cache-name $SC` (manual cutover default;
   `--auto-cutover` only if GATE 4 accepted it; `--clear-target-cache-data` is irreversible
   and needs an explicit decision) → `initiating-sync` → `syncing`. No migration metrics:
   watch `BytesUsedForCache`, `CurrItems`, `NetworkBytesIn` (dimension `clusterId`) and
   events. **A failed step moves the state backward** — the only failure signal besides
   events. Billing for migration infrastructure continues after `stop-migration`; only
   `complete-migration` ends it.
6. Stage D: `complete-migration --serverless-cache-name $SC` (+2–10 s tail latency;
   pub/sub, blocking and in-flight-write connections closed). `stop-migration` while
   `syncing` returns to `ready-to-sync` with traffic still served by the source — the clean
   abort (then repoint clients back to the source directly). **After cutover there is no
   rollback** other than moving traffic back yourself (§11). Any migration error is
   permanent (only exit: force complete). A key name > 4 KiB fails it permanently during the
   initial load (S1): force complete, move traffic back to the source, flush the target,
   prepare again.

### 7.3 RDB → S3 seed (C5)

1. A2 block: `BGSAVE` on a **replica** (fork + disk on that node), or `LASTSAVE` +
   existing RDB if its age is acceptable; record the copy boundary = save time. For a
   point-in-time copy, the writer freeze (A4 / customer) starts **before** `BGSAVE`.
2. Locate the file (`CONFIG GET dir` + `CONFIG GET dbfilename`), confirm
   `rdb_last_bgsave_status:ok`; checksum (`sha256sum`) before and after upload. One RDB per
   shard for a cluster source.
3. `aws s3 cp dump.rdb s3://$BUCKET/seed/dump.rdb` (bucket from `cdk-stacks.md`
   §ElastiCache — same Region, DNS-compliant, ElastiCache read grant, Bucket Keys off with SSE-KMS).
4. Create the target with `SnapshotArns=["arn:aws:s3:::$BUCKET/seed/dump.rdb"]` (CDK prop —
   replacement-only, so it belongs in the first deploy) and, for CME, `nodeGroups`
   (`NodeGroupConfiguration`: per-shard slots + replica count, one RDB per source shard — also
   first-deploy only; `cdk-stacks.md` §cache-stack.ts refuses a CME seed without it).
   Watch events; `restore-failed` (e.g. data larger than node memory, "unsupported version")
   → delete and recreate (not resumable).
5. Verify with `cache_compare.py --full-scan` (static copy: exact counts expected unless TTLs
   expired in between — the report separates `ttl`/`missing_on_target` from value errors).
   **Readiness:** this is an offline copy → `method-readiness.md` §Offline logical copy
   (final frozen copy at cutover, no soak).

### 7.4 RedisShake (C4)

- Run on a migration host inside the target VPC (EC2, SSM-managed), never a laptop.
  RedisShake is MIT-licensed (`tair-opensource/RedisShake`, v4.x; reads Redis 2.8–8.4.x and
  Valkey 8–9; lists ElastiCache/MemoryDB). RIOT is archived — don't propose it.
- Config: `shake.toml` with `[sync_reader]` (`PSYNC`; `cluster = true` for a cluster
  source; prefer a replica) or `[scan_reader]` (no `PSYNC`; optional keyspace-notification
  increments — a source config change, A2), and `[redis_writer]` (`cluster = true` for
  CME/Serverless targets, TLS on, RBAC username). **Passwords:** RedisShake reads them from
  the TOML; env-var configuration exists in its Docker examples but password support is
  **UNVERIFIED** — so the agent writes the TOML **without** password lines and the operator
  adds them in a customer-managed copy (chmod 600) on the migration host (hard
  constraint 4); never paste them into chat or SSM commands.
- `[filter]` can drop dbs/keys/command groups (e.g. skip `@pubsub`); `[advanced]
  empty_db_before_sync` wipes the target — leave it off unless authorized.
- **Not resumable:** "RedisShake 4.x does not support resumable transfer … will perform a
  full resync from the beginning when restarted" — a crash means a clean target + restart
  (record it as a recovery step). Supervise it with `execution_step.py` (local) — the
  verifier is `cache_compare.py`.
- During sync, compare with a live tolerance; before cutover, freeze writers, wait until the
  writer's applied offset stops moving (RedisShake log/status), then a final
  `cache_compare.py` with zero tolerance.

### 7.5 Dual-write (C7)

App writes every mutation to source **and** target (source authoritative, target
best-effort with an error counter); reads stay on the source. Backfill existing keys with
C4/C5/C6 after dual-write starts (so nothing written before the backfill is missed); verify
with `cache_compare.py`; cutover switches **reads**; keep dual-write through the rollback
window. Failed secondary writes must be logged and reconciled, or the "lossless" rollback
is not lossless.

### 7.6 Cold start / warm-up (C6)

Provision; optionally pre-warm (replay the hottest keys from the backing store, a
shadow-read job, or C7 for a period); repoint in a low-traffic window; watch backing-store
load, `CacheHitRate`/`CacheMisses`, `Evictions`, latency. Abort criterion: backing-store
CPU/latency beyond the agreed ceiling → repoint back (§11).

### 7.7 ElastiCache Redis OSS → Valkey in place (C1)

The engine change modifies the **production-serving cache itself** (new nodes, failover,
write pause), so it is **the cutover**: it never runs in Phase 6. Mode 2 — the customer
executes it from the handed-over runbook; Mode 3 — only under GATE 4 / A4.

1. **Phase 6 — rehearse on a clone.** `create-snapshot` of production (a read-only action
   on the cache, recorded), restore it **twice** into scratch replication groups (A3):
   clone-A stays Redis OSS, clone-B gets the exact production change —
   `modify-replication-group --replication-group-id $CLONE_B --engine valkey --engine-version 8.1 --cache-parameter-group-name $PG --apply-immediately`
   — with a test client writing through it to measure the write pause and the
   failover/DNS behaviour. For a single-node CMD cluster, rehearse the "put it in a
   replication group first" step too.
2. **Phase 7 — validate the rehearsal.** `cache_compare.py --full-scan` clone-A (source) vs
   clone-B (target): same snapshot, so counts and contents must match exactly; plus the
   Valkey parameter group's effective values, per-user login with the app's client on
   clone-B, `INFO server` (`valkey_version`). This proves the change on the production
   data's snapshot, **not** on the live cache — say so at GATE 3. No soak (no second live
   copy exists); the static alternative applies.
3. **Phase 8 — production change** (customer / A4): manual snapshot immediately before
   (rollback point), then the step-1 command against `$RG`
   (Serverless: `modify-serverless-cache --serverless-cache-name $SC --engine valkey --major-engine-version 8`).
   New Valkey nodes replicate from the Redis OSS primaries, then failover + DNS update;
   reads stay available, writes pause for seconds (5.0.6+; 30–60 s from < 5.0.6).
   Post-change verification: `INFO server`, key counts vs the pre-change snapshot's,
   app health, client connections (`CurrConnections`).
4. Rollback: only **Valkey 7.2 → Redis OSS 7.1** (in place or snapshot restore), with
   REDIS-engine users/user groups. From 8.x/9.x the pre-change snapshot restored into a new
   Redis OSS cluster is the only path — new endpoint (a client repoint), writes since the
   snapshot lost. State this RPO at GATE 4.
5. Via CDK: changing `Engine` `redis → valkey` on the `CfnReplicationGroup` is "No
   interruption" in CloudFormation's terms and needs the new parameter group in the same
   change; run the CLI in the window and back-port to CDK (no drift, `cdk-stacks.md`
   §Post-stabilization).

### 7.8 Memcached — §12.

### 7.9 Rehearsal minimum for caches (same bar as `execution-runbooks.md` §Migration Rehearsal)

On a clone (a scratch EC2 copy of the source from its RDB, or a staging cache) and a
scratch target: freeze writers → final sync (`complete-migration` / RedisShake drain /
final RDB) with a **measured** pause → repoint a test client → verify (`cache_compare.py`,
app health, target connections via `CLIENT LIST` or `CurrConnections`/`NewConnections` on
Serverless) → **rollback exercised** (repoint back, stale-data handling of §11 applied).
Serverless online migration's repoint-before-sync order must be rehearsed exactly as it
will run (§7.2 Stage A); C3 rehearsals include the TLS/RBAC transition C3-T1…T4; C1
rehearsals are the clone-A/clone-B pair of §7.7.

---

## 8. Validation (Phase 7)

Before validation: the production parameter group is the one attached (eviction policy,
keyspace events, `databases` verified with `describe-cache-parameters` on the actual group),
TLS/RBAC final, Multi-AZ/replicas/snapshot retention as approved — **except** where the
method makes that impossible before cutover, which GATE 3 must state:

| Method | What Phase 7 can validate on production | Deferred to cutover steps |
|---|---|---|
| C3 node-based online migration | Data (counts, sample), parameters, topology, replication link — over non-TLS | TLS/RBAC transition + per-user proof (C3-T1…T4, §7.1) |
| C2 Serverless online migration | Nothing about target contents (endpoint serves the source until cutover) | Content comparison and login proof right after `complete-migration` (§7.2 Stage D); clone evidence from Stage A |
| C1 in-place engine change | The rehearsal clone pair only (§7.7) | Post-change checks on production |
| C4/C5/C6/C7, M1 | Everything in this section | — |

`shared/scripts/cache_compare.py` (read-only; redis-py; memcached mode is stdlib) — config
in a JSON file with **no credentials** (`password_env`, customer-managed `secrets_file`
chmod 600, or `password_secret_id`). Least-privilege user (tested on Redis 7.2; on
ElastiCache the same access string as an RBAC user): `on ~* &* -@all +@read +info +ping
+select +cluster|slots +cluster|info +client|setname +client|setinfo -keys`.

```bash
python3 shared/scripts/cache_compare.py --config cache-compare.json --report evidence/cache-compare-$(date -u +%Y%m%dT%H%M%SZ).json \
  --dashboard dashboard --record validation --step-id cache-compare --plan-section "Phase 7"
```

| Check | Proves | Does NOT prove |
|---|---|---|
| `key_count` (INFO keyspace per db, summed over every primary; optional tolerance) | Same number of keys per logical db at read time | Same keys or values; `DBSIZE` includes logically-expired-not-yet-reclaimed keys and Serverless `DBSIZE` is not atomic across slots — small live differences are normal; frozen copies should match exactly |
| `sample_match` — deterministic bottom-k sample (seeded keyed hash; same seed ⇒ same keys on both sides and across runs), or `--full-scan`; TYPE, PTTL within `ttl_tolerance_ms`, per-type digest (string bytes; list order; hash/set/zset sorted; stream entries + group names/last-delivered-id); extras sampled from the target; mismatches re-read once | Sampled keys are identical in type, TTL and value | Unsampled keys (state the sample size and seed); stream PEL; module types (`unsupported_type`) and keys over `max_elements` (type/TTL/cardinality only) — both make the result **unknown (null)**, never a pass, and are listed in `uncompared_keys`; stream field order and duplicate field names **are** compared (raw `XRANGE` pairs) |
| `replication_link` (`expect_replication`) | Every target primary that is a replica has `master_link_status:up` | That it is caught up — read `offset_lag_bytes` and `MasterLinkHealthStatus` |
| `headroom` | Target memory headroom per primary above the threshold | Anything on Serverless (`not_applicable`) |
| `alarms` (`alarm_names`) | Named CloudWatch alarms are OK now | Nothing about the past — §2.7 history review |

Plus, mirroring `validation-patterns.md` §2.6: **each application user authenticates
through the intended endpoint with the application's own client library**, TLS on,
cluster mode as configured (a `MOVED`-following test for CME), and its ACL permits the
commands it uses; Lua scripts/functions loaded or `NOSCRIPT` fallback proven; pub/sub
subscribers reconnect; keyspace-notification consumers receive events (node-based).
`INFO memory` totals are informational only — encodings differ across engines/versions.
Never compare `DUMP` payloads across versions (Valkey 9 writes RDB 80). The dashboard gets
`migration_objects.keyspaces` (`dashboard.md`) with a four-state per-db `comparison`
(`match`/`mismatch`/`unknown`/`not_applicable`); only `match` is published as `validated`,
and any `false` blocks the validation gate. Any recorded error (a denied `INFO`, an alarm
lookup failure, an exhausted time budget — checked inside per-key reads and re-reads too)
caps the verdict at null. Logical dbs other than 0 on a cluster-mode side are compared where
the engine allows `SELECT` (Valkey 9 `cluster-databases`); otherwise that db is unknown.

GATE 3 presentation: state the sample size/seed or full-scan scope, the tolerance used
and why, transient vs persistent mismatches, and the limitations list verbatim.

---

## 9. Soak (Phase 7.7)

Which methods soak: those that keep the target **live-current** while production stays on
the source — C3 (until `complete-migration`), C4 RedisShake sync, C7 dual-write — with
daily `cache_compare.py`. **C2 soaks without content checks:** during Stage C the target
cannot be read independently, so its daily record is the operational evidence of §7.2
(state `syncing`, no failure events, `CurrItems`/`BytesUsedForCache` tracking the source,
`ThrottledCmds`, app errors through the endpoint), recorded manually with `sample_match`
`"not_applicable"` and the limitation stated; a daily `cache_compare.py` run there would
compare the source with itself. C1 has no soak (no second live copy). C5 and C6 are offline/static → `method-readiness.md`
static-validation alternative (final frozen copy for C5; C6 has no data obligation — record
it). Risk tiers and the daily UTC cadence with the 36-hour banner are unchanged.

**A green cache day** = all of: `key_count` within the agreed live tolerance;
`sample_match` true (zero persistent mismatches); `replication_link` true where it applies;
`alarms` OK; `headroom` above threshold or `not_applicable`; customer test suite if any;
and **`period_evidence`** reviewed for the whole UTC day — node-based: `MasterLinkHealthStatus`
minimum 1 all day, `Evictions` 0 (unless expected), `EngineCPUUtilization`,
`DatabaseMemoryUsagePercentage`, `CurrConnections` steady; Serverless: no migration
failure events, `ThrottledCmds` 0, `CurrItems`/`BytesUsedForCache` tracking the source;
RedisShake: no errors/restarts in its log (a restart = full resync = RED). `cache_compare.py
--record soak` writes the day with `period_evidence: null` — it never self-greens.

**Phase 7.7 entry choice** (`engagement-safety.md` §Phase 7.7 entry): the soak Lambda and
`soak_check.py` are SQL-only. For caches offer **D — daily `cache_compare.py --record
soak` runs** (who runs it, from which host, at what UTC time — or an installed cron on the
migration host verified by its first run) plus CloudWatch alarms → SNS. Never call it
automatic unless that schedule exists.

---

## 10. Client discovery and repoint (Phase 7.5)

Start from #9, then verify and extend (same completion criterion as
`cutover-procedures.md` §client discovery):

1. **SG trace** of the source's 6379/11211 ingress — same as SQL Step 1.
2. **`CLIENT LIST`** on **every node, primaries and replicas** (reads may target
   replicas): group by IP of `addr`, plus `name`, `lib-name`/`lib-ver` (populated only by
   clients that send `CLIENT SETINFO`, Redis 7.2+/Valkey), `user`, `db`, `cmd`, `flags`
   (`S` = a replica — a downstream consumer, `P`/`sub`/`ssub` = pub/sub subscriber, `x` =
   in `MULTI`), `age`/`idle`. Exclude this skill's own connections (`name=cache_compare`).
   **Repeat it over a full business cycle** (e.g. every 5 minutes for 24 h, including batch
   windows) — cron jobs, Lambdas and short-lived workers are invisible to one snapshot; use
   `total_connections_received` deltas to see churn.
3. **NAT/LB pitfalls:** SNAT (Kubernetes nodes, NAT gateways, proxies such as twemproxy,
   Envoy, HAProxy) collapses many clients to one IP — inventory behind the proxy; Sentinel
   clients learn the master from Sentinel (repoint = change the client's mode, not a
   hostname); hardcoded node IPs in cluster seed lists. Host-side `ss -tn state established
   '( sport = :6379 )'` on an EC2 source and VPC Flow Logs fill gaps. Memcached: `stats
   conns` where supported, else `ss`/Flow Logs.
4. **Downstream consumers:** replicas of the source, keyspace-notification listeners,
   RedisShake/other sync jobs, RDB-reading backup scripts — each gets its own row and plan.
5. **Repoint spec per client** (staged, never activated before Phase 8): new endpoint
   (DNS, never IPs — hard constraint 7), endpoint type, port, TLS flag (`rediss://` /
   `ssl=true`, CA trust), auth (AUTH token or RBAC username/password from Secrets Manager),
   cluster mode on/off, read-from-replica choice, pool/timeout settings.

| Target | Endpoint to use |
|---|---|
| Node-based CMD | **Primary endpoint** for writes (`master.<name>.<id>.<region>.cache.amazonaws.com` when TLS); **reader endpoint** for reads — DNS round-robin over replicas, "not a load balancer" |
| Node-based CME | **Configuration endpoint** + cluster-aware client (`ConfigurationEndPoint.Address` exists only for CME) |
| Serverless Valkey/Redis | One endpoint: 6379 read/write, 6380 `READONLY` (eventually consistent reads) |
| Memcached node-based | Configuration endpoint (`.cfg.`) with the ElastiCache Cluster Client (Auto Discovery: Java, PHP, .NET), or node endpoints; a CNAME must keep `.cfg.` |
| Memcached Serverless | One endpoint, 11211, TLS, no `.cfg` |

---

## 11. Cutover and rollback (Phase 8)

**Freeze for caches** (A4 / customer): stop or pause writers (app maintenance flag), or
`CLIENT PAUSE <ms> WRITE` on the source (Redis 6.2+; a production-source fence — scoped in
A4); prove writers = 0 (`instantaneous_ops_per_sec` of write commands via commandstats
deltas, `CLIENT LIST` cmd). Serverless online migration does its own `CLIENT PAUSE` at
cutover. Then per method: C3 `complete-migration` → repoint → TLS/RBAC transition C3-T1…T4
with per-user proof (§7.1 step 7); C2 Stage B (proxy repoint) earlier, Stage D
`complete-migration` → deferred validation (§7.2); C1 the engine change itself (§7.7 step 3);
C4 drain then stop RedisShake; C5 already frozen; C6 none; C7 switch reads. Repoint → bidirectional
verification (app health **and** target `CLIENT LIST`/`CurrConnections` shows every
inventoried client; source connections drained).

**Rollback — ElastiCache cannot replicate out** (`replicaof`, `sync`, `psync`, `migrate`,
`bgsave`, `config` are restricted; non-ElastiCache replicas unsupported). So
`cutover-procedures.md` §When Reverse Replication is NOT Possible always applies:

| Moment | Rollback | RPO |
|---|---|---|
| Before C3/C2 `complete-migration` | C3: clients never moved — do nothing. C2: `stop-migration`, repoint clients from the migration endpoint back to the source | 0 |
| After cutover, **dual-write (C7) still on** | Switch reads back | 0 if failed secondary writes were reconciled |
| After cutover, no dual-write | Repoint back to the source and **accept writes made on ElastiCache since cutover as lost** — explicit RPO acknowledgment in the plan/A4 | All writes since cutover |
| After cutover, reverse logical copy | RedisShake `scan_reader` from ElastiCache → source (scan needs no `PSYNC`) — **UNVERIFIED** against ElastiCache; only if rehearsed | Not point-in-time |
| After cutover, snapshot export | Export snapshot to S3 and load into the self-managed source **only if the RDB version is loadable** (never from Valkey 9) | Since the snapshot |

**Pure caches (C6/M1) roll back cheaply — but not blindly:** the old source still holds
values from before cutover, and writes since went to ElastiCache, so a repointed client can
read **stale** entries until their TTL expires. Before the rollback repoint, either flush
the source (`FLUSHALL` — destructive, its own explicit authorization) or accept
stale-until-TTL in the RPO statement. The same applies when a dual-write pair diverged.

Abort criteria to watch (T+15m/T+1h/T+24h): error rate, `CacheHitRate` drop, backing-store
load, `Evictions`, `EngineCPUUtilization`, `ThrottledCmds` (Serverless), connection counts.

### 11.4 Post-migration (Phase 9)

Compare against the Phase 2 baseline (ops/s, hit ratio, p95 latency from the app,
`LATENCY`/`SLOWLOG` where available); right-size nodes / Serverless minimums; restore pool
settings; enable automatic snapshots (`SnapshotRetentionLimit`; default 0 = disabled on
node-based); reconcile CDK drift (TLS/RBAC changes made in §7.1 step 7); remove migration
users on the source and the S3 seed objects; decommission the source only after the
rollback window, with constraint 8's confirmation.

---

## 12. Memcached specifics

- **There is no data migration**: node-based Memcached has no backup/restore/export; the
  cache is re-warmed (lazy loading, write-through, or a dual-write period — AWS caching
  strategies). The real risk is the cold-cache load on the backing store; plan its
  ceiling and a warm-up ramp, cut over in a low-traffic window.
- Target: node-based (multi-AZ node placement, Auto Discovery) or Serverless (TLS required,
  text protocol only, `GET/GETS` limited to 16 KB of keys, delayed `flush_all` rejected,
  only `stats`/`stats reset`). Node-based TLS (1.6.12+) is **creation-time only**. Version
  upgrades of node-based Memcached are destructive (cold cache).
- `max_item_size` default 1,048,576 B (modifiable, restart); compare with `stats slabs`.
- Validation: `cache_compare.py` with `"engine": "memcached"` — a **stats comparison, not a
  data comparison** (`data_match: "not_applicable"`); optional warmth checks
  (`memcached_min_items_ratio`, `memcached_max_hit_ratio_drop` with `memcached_min_gets`).
- Soak: none (no live copy); the static alternative records the warm-up observation.
  Rollback: repoint back (stale-entry caveat of §11).

---

## 13. Redis OSS → Valkey: licensing and compatibility

- Valkey (BSD-3-Clause) forked from Redis OSS 7.2.4; `INFO` reports `redis_version:7.2.4`
  plus `valkey_version`. "All features available with Redis OSS 7.2 are available in
  Valkey 7.2 and above." Redis OSS 2.x–7.2.x → Valkey 7.2+ is supported; CE 7.4+ → "n/a".
- Redis licensing: 7.2.x and earlier BSD-3; Redis 7.4–7.8 RSALv2/SSPLv1; Redis 8 adds
  AGPLv3 as an option. A CE 7.4+/8 source is therefore not "Redis OSS" — name it correctly
  in the plan, and route per V1.
- Client compatibility: Redis clients work with Valkey; clients that branch on the
  server version string are what `extended-redis-compatibility` (Valkey 8) addresses.
  Verify with the app's real client in Phase 7.

---

## 14. Korean context (brief)

- Korean clouds' **managed** Redis/Memcached products (e.g. on NHN Cloud or Naver Cloud
  Platform) normally give no OS access and no `PSYNC`/`REPLICAOF` to a foreign replica —
  treat them like matrix row 12a/12b's managed-DB case: C4 RedisShake **`scan_reader`**, or
  the provider's backup **export** to an RDB file (if it offers one) for C5 — check the
  provider's engine version first (V1). Exact provider capabilities are provider-specific:
  confirm with the customer/provider, record as assumptions until evidenced.
- A self-managed Redis VM on those clouds behaves like on-prem (C3 does not apply — not
  EC2; use C4/C5).
- Regulatory (`regulatory-compliance.md`): in-transit TLS and access control for
  personal data in caches (session tokens, profiles) under PIPA/ISMS-P; data residency →
  `ap-northeast-2`; network separation → private endpoints only (no Serverless public
  endpoint). Serverless online migration is available in all commercial Regions.

---

## 15. Cost and IAM pointers

- GATE 2 cost lines: target nodes or Serverless (storage GB-hours + ECPUs + minimums),
  replicas/Multi-AZ, snapshots, the migration host (C4), S3 seed storage (C5), Serverless
  online-migration nodes (free 180 days per account, then per node-hour), source
  double-running through the rollback window, Redis OSS Extended Support if staying on
  4/5/6. Valkey pricing differences: §6.
- IAM actions and the S3/KMS grants: `preflight-iam-cost.md` §2 (ElastiCache row);
  alarms and metric names/dimensions: `preflight-iam-cost.md` §4 and `cdk-stacks.md`
  §ElastiCache.

---

## Sources

Facts brief: `research/facts-elasticache-mysql-pg.md` Part A (2026-10-28), whose URLs include:
[engine versions](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/engine-versions.html) ·
[node-based online migration](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/OnlineMigration.html) ·
[Serverless online migration](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/migration-to-serverless.html) ·
[its source authentication](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/migration-to-serverless-auth.html) ·
[seeding from S3](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/backups-seeding-redis.html) ·
[Redis OSS → Valkey in place](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/VersionManagement.HowTo.html) ·
[supported/restricted commands](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/SupportedCommands.html) ·
[Serverless configuration](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/RedisConfiguration.html) ·
[parameter groups](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/ParameterGroups.Engine.html) ·
[in-transit encryption](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/in-transit-encryption.html) ·
[RBAC](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/Clusters.RBAC.html) ·
[endpoints](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/Endpoints.html) ·
[metrics](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/CacheMetrics.Redis.html) ·
[data tiering](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/data-tiering.html) ·
[CFN ReplicationGroup](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-elasticache-replicationgroup.html) ·
[Valkey migration](https://valkey.io/topics/migration/) ·
[Redis licenses](https://redis.io/legal/licenses/) ·
[RedisShake](https://github.com/tair-opensource/RedisShake).
Added in this file: [Bloom filters (Valkey 8.1+)](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/BloomFilters.html) ·
[JSON data type](https://docs.aws.amazon.com/AmazonElastiCache/latest/red-ug/json-document-overview.html) ·
live read-only checks 2026-10-28: `describe-cache-engine-versions` (families),
`describe-cache-parameters default.valkey8` (`maxmemory-policy volatile-lru`,
`notify-keyspace-events` empty, `databases 16` requires-reboot; no `rdb-version-check` in
`valkey8`/`valkey9`); Docker: Valkey 8.1.11 replicated from Redis 7.2.17 via `REPLICAOF`;
`REDISCLI_AUTH` honored by redis-cli 7.2 and valkey-cli 8.1 with `--user`.
