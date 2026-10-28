# Eval Scenario — On-prem Memcached 1.6 → ElastiCache for Memcached

## Simulated user prompt

> "We have two Memcached 1.6 servers on-prem in front of our MySQL product catalog (the
> catalog is also moving to Aurora next quarter). PHP apps use the memcached extension
> with a static server list. Items are up to ~2 MB. We have a VPN to ap-northeast-2. Please
> move the cache to ElastiCache — we'd like TLS."

## Expected behavior checklist

**Process**
- [ ] Same activation / mode question / Phase 0 preflight; recognizes a cache source at #1,
      loads `elasticache-migration.md` and generates `discovery-questions.md` from the cache
      template `discovery-questions-cache.md` in its
      Memcached form (drops Redis-only sub-questions, keeps #9 access checks and #16)
- [ ] Notes the catalog DB's own migration is a separate engagement (two client repoints if
      sequenced badly) and asks which goes first

**Assessment (read-only)**
- [ ] `stats`, `stats settings`, `stats slabs`/`stats items`, `stats conns` where supported;
      flags items > 1 MiB against `max_item_size` (default 1,048,576 B) and binary/UDP/SASL use
- [ ] Measures hit ratio and request rate to estimate the cold-cache load on MySQL

**Method**
- [ ] Selects **M1 provision → warm → repoint** and says plainly there is **no data
      migration path** for Memcached (no backup/export on node-based); does not invent one
- [ ] Explains the real risk = backing-store load during warm-up; proposes a warm-up/ramp
      (lazy loading, write-through or a dual-write period) and an abort ceiling
- [ ] Target choice explained: node-based with TLS **at creation only** (1.6.12+) and a
      custom `memcached1.6` parameter group raising `max_item_size`, vs Serverless (TLS
      mandatory, text protocol only, `GET/GETS` key-size limits) — client TLS support verified
- [ ] Rollback: repoint back to on-prem, with the **stale-entry caveat** (flush — its own
      authorization — or accept stale-until-TTL); RPO stated

**Provisioning / validation**
- [ ] CDK `CfnCacheCluster` from §cache-stack.ts (cross-AZ, TLS, parameter group,
      `CacheClusterId` alarms for CPU/Evictions); configuration endpoint (`.cfg`) vs node
      endpoints explained for the PHP client (Auto Discovery needs the ElastiCache Cluster Client)
- [ ] `cache_compare.py` in memcached mode presented as a **stats comparison, not a data
      comparison** (`data_match: "not_applicable"`), with optional warmth thresholds
- [ ] No soak (no live copy); static alternative recorded; rehearsal includes a measured
      warm-up and an exercised rollback repoint

**Cutover (Mode 2)**
- [ ] Hands over the per-client config change (server list → endpoint, TLS on) and stops

## Anti-patterns (fail the eval if seen)
- Claiming Memcached data can be copied/replicated to ElastiCache
- Claiming TLS can be enabled on an existing node-based Memcached cluster later
- Cutting over at peak without a backing-store load estimate
- Reporting a "data validated" result from memcached stats
