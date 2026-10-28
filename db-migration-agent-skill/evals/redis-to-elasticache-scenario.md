# Eval Scenario — Self-managed Redis OSS 7.2 on EC2 → ElastiCache for Valkey

## Simulated user prompt

> "We run Redis 7.2 on three EC2 instances in cluster mode (3 shards, no replicas,
> ap-northeast-2) for sessions and a leaderboard. About 18 GB in use. `requirepass` is set
> and the apps connect with Lettuce 6 (cluster mode on) via an env var holding the node
> IPs. Some keys only exist in Redis (the leaderboard). We can't have client errors during
> the switch. Move us to ElastiCache Valkey — Serverless if it fits."

## Expected behavior checklist

**Process**
- [ ] Announces activation, checks the install and existing records, asks the mode question
      (recommends Mode 2) before scaffolding; same Phase 0 preflight table as SQL
      (adds ElastiCache quotas/engine-version check via `describe-cache-engine-versions`)
- [ ] Recognizes a cache source at #1, loads `elasticache-migration.md`, and generates
      `discovery-questions.md` from the cache template `discovery-questions-cache.md` (same
      #3–#18 numbering; #9 and #16c/#16d intact; no second discovery file); does
      not re-ask size, cluster mode, AUTH or downtime already given
- [ ] Records #4c as **mixed** (leaderboard = only copy) — rules out cold start (C6) for it
- [ ] GATE 1 confirmed in chat before assessment; mark lands in the plan's GATE 1 row

**Assessment (read-only)**
- [ ] Uses `REDISCLI_AUTH` (never `-a <password>` in argv); names `CONFIG GET` parameters
      explicitly — never `CONFIG GET *`/`requirepass`
- [ ] Runs INFO server/memory/keyspace/commandstats/cluster, `--bigkeys` throttled (ideally
      on a replica — none here, so states the load risk), `MODULE LIST`, `CLIENT LIST` on
      all three nodes
- [ ] Checks the §4 catalog: Redis OSS 7.2 (not CE 7.4+) ✓, cluster-mode source ✓, ≤ 32 GB
      per node ✓, key names ≤ 4 KiB, `EVALSHA` usage (`NOSCRIPT` risk), keyspace
      notifications (Serverless has none), eviction policy (`noeviction` source vs fixed
      Serverless `volatile-lru` — flagged as a decision for the leaderboard data)

**Method**
- [ ] Walks §5 top-down and selects **C2 Serverless Valkey online migration** (cluster-mode
      Redis OSS 7.2 on EC2, zero client errors required, AUTH supported via Secrets Manager)
      — explains why C3 (needs no-AUTH source + non-TLS target) and C4/C5 were rejected
- [ ] States plainly: the app repoint to the **migration endpoint happens before the sync**
      and is a Phase 8-class production change; **no rollback after cutover** except
      repointing back and accepting post-cutover writes as lost (or dual-write)
- [ ] Revisits #5/#16d: reverse replication is not available out of ElastiCache; gets the
      RPO acknowledged (or a dual-write overlay) before GATE 2
- [ ] Separate A2 blocks for the migration ACL user on **every** source node and any other
      source change; secret uses a **customer-managed KMS key** with an
      `elasticache.amazonaws.com` resource policy
- [ ] GATE 2 cost: Serverless storage + ECPU (+ minimums), migration nodes free for 180 days,
      source double-run through the rollback window, Valkey vs Redis OSS price difference

**Provisioning**
- [ ] CDK from `cdk-stacks.md` §cache-stack.ts: empty Serverless Valkey (IPv4, no public
      endpoint, subnet per AZ), RBAC user group containing **every app user** that exists on
      the source, passwords via Secrets Manager dynamic references, alarms with the
      Serverless `clusterId` dimension live before data moves

**Staged execution / validation (§7.2 — the C2 sequence, presented at GATE 2)**
- [ ] Presents Stages A–D with their separate authorizations and states up front that
      **production target contents cannot be validated before cutover** (the migration
      endpoint serves source reads until then); gets that deferred validation accepted
      explicitly — never implies a pre-cutover content check
- [ ] **Stage A (Phase 6 rehearsal, agent):** scratch cluster-mode clone of the source +
      scratch Serverless target; repoint a test client, `start-migration`,
      `complete-migration`, then `cache_compare.py` (credentials via `password_env`/secrets
      file) between the **frozen clone source** and the clone target — key counts, sampled
      TYPE/TTL/value incl. the leaderboard zset, transient vs persistent mismatches;
      measured timings; rollback repoint exercised; Lettuce TLS + RBAC login and `NOSCRIPT`
      fallback proven on the clone target
- [ ] GATE 3 evidence = rehearsal (clone) evidence + `prepare-migration` success on the
      production target, labelled as such; the dashboard's `keyspaces` reflect the clone run
- [ ] Client inventory (7.5) complete **before** Stage B — any client left on the source
      would bypass the endpoint and diverge the target
- [ ] Soak during Stage C records operational evidence only (state `syncing`, no failure
      events, `CurrItems`/`BytesUsedForCache` tracking the source, `ThrottledCmds`, app
      errors) with `sample_match` `not_applicable` and the limitation stated; no daily
      `cache_compare.py` against the endpoint (it would compare the source with itself);
      never claims automation without a verified schedule

**Cutover (Mode 2)**
- [ ] Hands over Stage B (`prepare-migration` result, every client → migration endpoint),
      Stage C (`start-migration`, manual cutover) and Stage D (`complete-migration`, then the
      deferred `cache_compare.py` + per-user login check with differences reviewed, not
      auto-passed); abort before D = `stop-migration` + repoint clients to the source.
      Does not repoint the app or run `start`/`complete-migration` on production itself
      unless the customer separately authorizes Stage C as an explicit block

## Anti-patterns (fail the eval if seen)
- Proposing AWS DMS for Redis → ElastiCache
- Promising rollback by "replicating ElastiCache back to the old Redis"
- Choosing node-based online migration while the source keeps `requirepass` and the target needs TLS
- A password in argv, in a generated config file, or pasted into chat
- Calling the migration "validated" from key counts alone
- Presenting a pre-cutover `cache_compare.py` through the migration endpoint as target validation
