# Discovery Questions (cache) — {engagement} ({source} → {target})

> Check a lettered option and/or write your own answer on the **Answer:** line under each
> question or labeled sub-question (e.g. 4a/4b/4c, 16a-16e), save, and let me know — or just
> answer any of these here in chat instead, referencing the number/letter so it's
> unambiguous which question you mean — either works, I'll re-read this file either way.
> Short on time? Reply "go with recommendations" and I'll use the called-out default for
> everything below, and flag every one I did that for when we finish up.
>
> Already covered in chat: **#1** (cache engine, exact version, deployment shape, and how
> you already connect to it) and **#2** (target: ElastiCache for Valkey / Redis OSS /
> Memcached, node-based or Serverless, network placement). Mode was chosen even earlier.
> This file picks up from **#3**. The numbers match the database version of this file on
> purpose, so every later reference (#9, #16c, #16d …) means the same thing.
>
> {Agent: this is the cache TEMPLATE. When #1 is Redis OSS, Valkey, Redis CE or Memcached
> (`shared/reference/elasticache-migration.md` §2), instantiate it as the engagement's
> **`discovery-questions.md`** — the canonical filename every resume/backfill step reads —
> instead of the SQL template; never create a second discovery file. Drop
> Redis-only sub-questions (3a, 7, 13) to their Memcached form for a Memcached source; drop
> OS-access framing for a managed provider source. Retain #9's access checks regardless.
> Fill every {bracketed} value with the real, engagement-specific default — never ship a
> literal placeholder to the customer.}

---

### 3. Dataset size

Memory in use (`used_memory`, and its peak) and the number of keys per logical database.
This sizes the target and decides which migration methods fit (Serverless online migration
caps a source node at 32 GB; an RDB file must fit the target node's memory).

- [ ] A. < 1 GB
- [ ] B. 1 – 25 GB
- [ ] C. 25 – 100 GB
- [ ] D. > 100 GB

**Answer:** {A/B/C/D, or exact numbers, or "measure it for me" — I run read-only INFO}

**3a. Largest keys.** Any very large values or collections (multi-MB strings, hashes/lists
with millions of elements, key names longer than 4 KB)? They affect replication, Serverless
limits and validation time. I'll measure with a throttled `--bigkeys` scan regardless.

**Answer:** {known big keys, or "not sure"}

---

### 4. Downtime tolerance, production status, and what the data is

**4a. Downtime tolerance** during the switch.

- [ ] A. Zero — no client errors at all
- [ ] B. Seconds
- [ ] C. Minutes
- [ ] D. Hours (a maintenance window)

**Answer:**

**4b. Production status and live writes** (sets the proposed 1/3/7-day soak tier).

- [ ] A. Production, with active writers
- [ ] B. Production, read-mostly
- [ ] C. Non-production — no live production traffic
- [ ] D. Not sure

**Answer:**

**4c. Is it a cache, or your primary datastore?** If every value can be rebuilt from
another system (a database, an API), the simplest safe migration is often to start the new
cache empty and let it warm up. If some data exists **only** here (sessions you can't
recreate, counters, queues, leaderboards), it has to be copied and protected like database
data.

- [ ] A. Pure cache — everything can be rebuilt (**say which backing system absorbs the
      extra load while it warms**)
- [ ] B. Mixed — some keys/prefixes are the only copy (list them)
- [ ] C. Primary datastore
- [ ] D. Not sure

**Answer:**

---

### 5. What you can lose on rollback

After a switch, writes go to ElastiCache. ElastiCache cannot replicate back to your current
server, so rolling back means pointing applications back and either **losing writes made
since the switch**, or having the application write to both sides for a while. For a pure
cache this usually means "nothing important, but the old cache may serve stale values until
they expire."

**Answer:** {nothing / a stated window / "only what can be rebuilt" / "need to ask the business"}

---

### 6. Usable network bandwidth, source → AWS

The sustained throughput on the path the data travels (needed for an RDB upload or a
replication/RedisShake stream from outside AWS).

**Answer:** {Mbps, or "same VPC", or "not measured — we'll test it"}

---

### 7. Server-side features in use

Some features don't exist, or behave differently, on ElastiCache (especially Serverless).
Tick everything you know is used; I'll also check command statistics read-only.

- [ ] A. Lua scripts (`EVAL`/`EVALSHA`) or Functions (`FCALL`)
- [ ] B. Modules (RedisJSON, RediSearch, RedisBloom, time series, custom)
- [ ] C. Keyspace notifications (subscribers to `__keyspace@…`/`__keyevent@…`)
- [ ] D. Pub/sub, or blocking commands (`BLPOP`, `XREAD BLOCK`)
- [ ] E. Streams with consumer groups
- [ ] F. Multi-key commands or transactions across keys (`MGET`, `MULTI`/`WATCH`)
- [ ] G. None of these / not sure — check for me

**Answer:**

**7a. Eviction and persistence.** What should happen when memory is full (evict, or
return errors)? Is RDB/AOF persistence relied on as a backup or for durability? Note:
ElastiCache's default eviction policy is `volatile-lru`, while self-managed Redis defaults
to `noeviction` — I'll carry your current setting unless you choose otherwise
(Serverless's policy is fixed).

**Answer:** {keep current / change to … / "check for me"}

---

### 8. Application changes allowed

Can application code or client configuration change? Moving to a cluster-mode target
(including any Serverless cache) needs a **cluster-aware client**, and ElastiCache access
control needs **TLS** — both are client-side changes.

**Answer:** {yes / config only / no}

**8a. Client libraries and versions** (e.g. Lettuce 6.x, Jedis 5.x, redis-py 5.x,
ioredis, StackExchange.Redis, go-redis, php-redis, pymemcache, spymemcached). Do they
support TLS, ACL usernames, and cluster mode? I'll also read client names/versions from
`CLIENT LIST` where clients report them.

**Answer:**

---

### 9. How each application finds the cache today

For every application/service that connects: where does it get the address — environment
variable, systemd unit, config file, secret, Sentinel master name, a proxy (twemproxy,
Envoy), or a hardcoded IP/seed-node list? This decides, at cutover, between "update one
secret" and "edit a script on a server with no deployment pipeline." **And how does it read
its credentials** — `EnvironmentFile`, a config file, a secret manager, an environment
variable? That decides how the new credentials reach the host (planned by GATE 2).

**If any part is unknown, confirm access now:** for each known client, does a writable
path exist to change and revert its effective cache-address setting (SSM/SSH or the
deployment pipeline/control plane), and who will operate it? For SSM, confirm the host is
managed, connected, and has the required instance profile/permissions; having no SSH key is
fine only when another working path exists. Also identify where the config is deployed
from. In Mode 2 your team performs/reports host inspection; the agent supplies the checks
and reviews the evidence. Missing access becomes a tracked cutover blocker now, not a
surprise deferred to Phase 7.5. No client setting is changed by this check.

**Answer:** {known clients + address source + writable access/operator; unknown parts and
who will resolve them before Phase 7.5}

---

### 10. Other things reading from this cache

Replicas of this server, keyspace-notification listeners, sync/replication jobs, scripts
that copy `dump.rdb` for backup? They can't simply be repointed and need their own plan.

**Answer:** {list any, or "none that we know of"}

---

### 11. Compliance and security mandates

Encryption in transit, access control (passwords/ACL users), data residency, audit (e.g.
Korean PIPA, ISMS-P, network separation)? These decide TLS, RBAC and region.

**Answer:** {list any, or "none that apply"}

**11a. Current access control.** No password / a single password (`requirepass`) / ACL
users (which ones) / TLS on? (Never send a password — just say which kind.) And **how each
application actually authenticates**: password only (`AUTH <password>`), username + password
(`AUTH <user> <password>`), or nothing — I'll confirm it from the app's real service unit,
connection code and client library during the assessment, because a test client that logs in
differently proves nothing about your app.

**Answer:**

---

### 12. Security products in front of the cache

Any access-control, audit or encryption product (Korean or global) attached to this cache?
Usually none for caches — knowing now avoids a late surprise.

**Answer:** {product(s) + mode, or "none"}

---

### 13. Logical databases and instances

Do applications use database numbers other than 0 (`SELECT 1`…)? Are there several
cache instances on the same host (different ports)? Cluster-mode targets — and every
Serverless cache before Valkey 9 — have **only database 0**.

**Answer:** {db0 only / dbs in use: … / multiple instances: …}

---

### 14. Cross-region or cross-account target

**Answer:** {same region/account / different region / different account / both}

---

### 15. Encryption key

Encrypt at rest with an AWS-managed key or a customer-managed key (CMK)? On node-based
clusters this can't be changed later without rebuilding the cluster. (Serverless is always
encrypted.)

- [ ] A. AWS-managed key — simplest default
- [ ] B. Customer-managed key (CMK)
- [ ] C. Not sure — recommend one

**Answer:**

---

### 16. Engagement parameters

**16a. Rehearsal** on a copy before the real switch.

- [ ] A. One rehearsal (**recommended default**)
- [ ] B. Repeat until timings converge (< 20% delta) — worth it for a zero/seconds budget
- [ ] C. None

**Answer:**

**16b. Validation depth.**

- [ ] A. Key counts per database + sampled key comparison (type, TTL, value)
- [ ] B. All of A plus a full-keyspace comparison where size allows, client login/TLS/
      cluster-mode tests, scripts/pub-sub/notification checks (**recommended default**)
- [ ] C. All of B plus application-level reconciliation you define

**Answer:**

**16c. Parallel-run (soak) length.** A parallel run means: your current cache keeps serving
production exactly as today — nothing is at risk yet. The new cache sits alongside it,
receiving every change in real time, and gets watched for some number of days before the
switch is recommended.

A one-time check can only see what's true right now; daily samples also miss a job at
2 AM or a recovered outage. Acceptance needs retained replication evidence and CloudWatch
alarm/metric history covering each full period, including scheduled-job windows.
Missing evidence blocks acceptance; automation does not prove an incident-free day.

For your case the signal is: **{tier signal}** — so the proposed length is **{N} day(s)**
({tier} risk tier). {If the chosen method keeps no live copy — RDB seed or warm-up — say
instead: "this method has no live copy to watch, so there is no soak; we validate the
copy itself and repeat that check at the switch."} Daily checks run by a named person or a
scheduled job you approve — there is no unattended soak service for caches; you'll get a
dashboard link either way.

- [ ] A. Keep the proposed {N} day(s)
- [ ] B. Shorten it — say how many days (hours require a waiver and manual tracking)
- [ ] C. Skip it entirely — this is a waiver; the risk that moves into the switch window is
      {state plainly, per `engagement-safety.md` §Waiver protocol}
- [ ] D. Not sure — give me one line of advice for a case like mine

**Answer:** {A/B/C/D — if B or C, say why}

**16d. Rollback strategy.** (Reverse replication from ElastiCache is **not available** —
ElastiCache can't replicate out.)

- [ ] A. Point applications back and accept losing writes made since the switch (or, for a
      pure cache, accept stale values until they expire / flush the old cache first)
- [ ] B. Application writes to both caches through the rollback window (lossless, needs an
      app change)
- [ ] C. Abort only before the final switch step (online migration not yet completed) —
      afterwards fall back to A
- [ ] D. Not sure — recommend one for my case

**Answer:** {A/B/C/D, or your own answer}

**Mode 2 only — 16e. Handover depth.**

- [ ] A. Full preparation — copy kept current through the parallel run, switch rehearsed,
      measured timings (**recommended default**)
- [ ] B. Light preparation — target built and loaded/validated once; you run the final
      sync and parallel run; timings estimated, not measured

**Answer:**

---

### 17. Third-party tools on or around the cache

Monitoring agents/exporters, Sentinel, proxies, backup scripts, HA tooling? I run a
detection sweep regardless.

**Answer:** {list any, or "none that we know of"}

---

### 18. Your own test suite

Regression/UAT/load tests that already exercise this cache? They become acceptance gates,
run in *your* CI against the target endpoint (never pasted into chat).

**Answer:** {yes — describe briefly / no}

---

### Ready for Phase 2?

Once #1–18 above (across this file and chat) are answered — or you just say "go with
recommendations" — tell the agent you're ready in chat and it moves straight to Phase 2.
You never edit a file yourself, here or anywhere else in this engagement — just give a
clear go-ahead in conversation, and the agent notes it in `migration-plan.md`'s GATE 1 row
itself (there's no separate `authorizations.md` block for it). From here we'll go with the
choices above — if anything needs to change later, we'll say so plainly rather than quietly
switching.

---

*End of file. Answer as many as you'd like, in any order — reply here in chat when you're
done, or just start talking and I'll re-read this file on my next turn either way.*
