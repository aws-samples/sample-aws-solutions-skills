# Eval Scenario 2 — SQL Server 2019 on EC2 → Aurora PostgreSQL (heterogeneous)

## Simulated user prompt

> "고객사가 EC2의 SQL Server 2019 Standard(약 400 GB)를 라이선스 비용 때문에 Aurora로
> 옮기고 싶어 합니다. 애플리케이션은 .NET이고 T-SQL 저장 프로시저가 200개쯤 있습니다.
> 다운타임은 주말 야간 4시간까지 가능합니다."

## Expected behavior checklist

**Routing & language**
- [ ] Responds in Korean; code/CLI stays English
- [ ] Classifies as **heterogeneous** (engine family changes) — routes schema/code
      conversion via `heterogeneous-migration.md`, NOT the homogeneous matrix
- [ ] Presents the three-way target decision with trade-offs: **Babelfish for Aurora
      PostgreSQL** (minimal .NET/T-SQL change) vs full PostgreSQL conversion (SCT/DMS SC)
      vs RDS for SQL Server (lift-and-shift, keeps license) — user decides at GATE 2
- [ ] Recommends running the SCT / DMS Schema Conversion **assessment report first** to
      quantify auto-convertible % of the 200 procs before promising anything
- [ ] Chains the official `dms-schema-conversion` skill (retrieve_skill) for conversion
      work when MCP is available, instead of duplicating it

**Migration mechanics**
- [ ] Data movement via DMS Full Load (+ CDC if the 4-hour window demands it); notes DMS
      moves data only — converted schema applied before load
- [ ] Verifies SQL Server prerequisites: FULL recovery model, FILESTREAM absence, CLR
      assemblies, server-level objects (logins/Agent jobs) handled separately
- [ ] If RDS SQL Server chosen: native .bak/S3 path with `SQLSERVER_BACKUP_RESTORE`
      option group; collation fixed at creation confirmed against source

**Production discipline (same bar as scenario 1)**
- [ ] migration-plan.md maintained; GATES 1–4 enforced; cost itemized incl. license
      delta (the migration's whole point); client discovery before cutover; rollback
      strategy signed (reverse replication is NOT possible into a different engine —
      must present write-log replay or RPO acknowledgment instead)

## Anti-patterns (fail)
- Claiming DMS/SCT converts everything automatically
- Treating SQL Server → Aurora PostgreSQL as homogeneous
- Promising lossless rollback via reverse replication across engine families

## v2.1 additions (modes/tiers — graded from round 2 onward)
- [ ] Mode question first; tier locked at GATE 1 (heterogeneous ≠ automatic Tier 3 — tier follows business impact)
- [ ] Third-party sweep run on the SQL Server host (backup/monitoring agents are near-universal on Windows DB hosts)
- [ ] authorizations.md maintained; license-decommission decision recorded there
