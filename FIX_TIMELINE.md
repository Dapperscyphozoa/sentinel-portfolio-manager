# FIX_TIMELINE — Bug Fixes & Service Events (rolling 72h window)

**Purpose:** When auditing strategy performance, ANY data point
older than the most recent relevant fix is INVALID for judging
current behavior. The council must be told what changed and when.

Generated: 2026-05-22 02:48 UTC

## Events (chronological)

| Timestamp (UTC)         | Type                | Source            | Detail |
|-------------------------|---------------------|-------------------|--------|
| 2026-05-20 02:01:49 | deploy_ended        | sentinel-trader   | {'deployId': 'dep-d86hb8egvqtc73dn1cag', 'deployStatus': 'su |
| 2026-05-20 02:03:00 | deploy_ended        | sentinel-trader   | {'deployId': 'dep-d86hbcn3jp8c73aj969g', 'deployStatus': 'su |
| 2026-05-20 02:27:29 | deploy_ended        | sentinel-trader   | {'deployId': 'dep-d86hnhjtqb8s73fiob40', 'deployStatus': 'su |
| 2026-05-20 02:47:32 | deploy_ended        | sentinel-trader   | {'deployId': 'dep-d86i0o6q1p3s73f04ah0', 'deployStatus': 'su |
| 2026-05-21 04:01:54 | deploy_ended        | spm-strategy-runner | {'deployId': 'dep-d8786fkm0tmc73ci9u6g', 'deployStatus': 'su |
| 2026-05-21 06:08:30 | deploy_ended        | spm-strategy-runner | {'deployId': 'dep-d87a1ugjs32c7393iba0', 'deployStatus': 'su |
| 2026-05-21 06:29:29 | service_suspended   | spm-signal-bus    | {} |
| 2026-05-21 06:29:29 | suspender_added     | spm-signal-bus    | {'actor': 'User', 'suspendedByUser': {'email': 'calebmatene@ |
| 2026-05-21 06:29:31 | service_suspended   | spm-strategy-runner | {} |
| 2026-05-21 06:29:31 | suspender_added     | spm-strategy-runner | {'actor': 'User', 'suspendedByUser': {'email': 'calebmatene@ |
| 2026-05-21 06:29:33 | service_suspended   | sentinel-trader   | {} |
| 2026-05-21 06:29:33 | suspender_added     | sentinel-trader   | {'actor': 'User', 'suspendedByUser': {'email': 'calebmatene@ |
| 2026-05-21 21:50:54 | deploy_ended        | core              | {'deployId': 'dep-d87nrrps16ns73f0unu0', 'deployStatus': 'su |
| 2026-05-21 22:12:20 | deploy_ended        | core              | {'deployId': 'dep-d87o5kr7uimc73anobqg', 'deployStatus': 'su |
| 2026-05-21 23:17:39 | commit              | c527fdf           | fix(landing): attribution panel reads live registry, exposes |
| 2026-05-21 23:19:21 | deploy_ended        | core              | {'deployId': 'dep-d87p55btqb8s73e7leb0', 'deployStatus': 'su |
| 2026-05-21 23:20:37 | deploy_ended        | core              | {'deployId': 'dep-d87p570bho7s73ftblfg', 'deployStatus': 'su |
| 2026-05-21 23:29:25 | commit              | 329f390           | feat(backtest): honest BT on 13 production engines + decisio |
| 2026-05-22 00:03:54 | service_resumed     | spm-signal-bus    | {} |
| 2026-05-22 00:06:04 | deploy_ended        | spm-signal-bus    | {'deployId': 'dep-d87pqr77f7vs73dn7sq0', 'deployStatus': 'su |
| 2026-05-22 00:13:36 | commit              | a9fd424           | fix(signal-bus): /health lock-free, move heavy snapshot to / |
| 2026-05-22 00:15:26 | deploy_ended        | spm-signal-bus    | {'deployId': 'dep-d87pvck2m8qs73b5mr00', 'deployStatus': 'su |
| 2026-05-22 00:25:27 | commit              | 4750268           | remove(funding_triangulation): structurally unfixable, archi |
| 2026-05-22 00:28:33 | commit              | 3742401           | archive: closures + decision log for bleeder triage 2026-05- |
| 2026-05-22 01:31:45 | service_resumed     | sentinel-trader   | {} |
| 2026-05-22 01:31:58 | service_resumed     | spm-strategy-runner | {} |
| 2026-05-22 01:33:10 | deploy_ended        | spm-strategy-runner | {'deployId': 'dep-d87r43sm0tmc7383sid0', 'deployStatus': 'su |
| 2026-05-22 01:33:12 | deploy_ended        | sentinel-trader   | {'deployId': 'dep-d87r40km0tmc7383sfr0', 'deployStatus': 'su |
| 2026-05-22 01:38:59 | deploy_ended        | sentinel-trader   | {'deployId': 'dep-d87r6q67r5hc73f0guhg', 'deployStatus': 'su |
| 2026-05-22 01:43:02 | deploy_ended        | spm-strategy-runner | {'deployId': 'dep-d87r8krbc2fs73e9q170', 'deployStatus': 'su |
| 2026-05-22 02:09:46 | commit              | b2f93c1           | remove(hl_depth_shock): operator rejected observe-mode; engi |
| 2026-05-22 02:10:37 | commit              | eaea746           | remove(hl_depth_shock): final docstring cleanup |
| 2026-05-22 02:11:06 | deploy_ended        | spm-strategy-runner | {'deployId': 'dep-d87rlr42m8qs73b6hgag', 'deployStatus': 'su |
| 2026-05-22 02:12:11 | deploy_ended        | spm-signal-bus    | {'deployId': 'dep-d87rm7u47okc7391e5fg', 'deployStatus': 'su |
| 2026-05-22 02:16:41 | deploy_ended        | core              | {'deployId': 'dep-d87rocn7f7vs73dp61r0', 'deployStatus': 'su |
| 2026-05-22 02:18:03 | commit              | 6a1b15a           | feat(dashboard): merge sentinel-trader as 3rd attribution so |
| 2026-05-22 02:19:31 | deploy_ended        | core              | {'deployId': 'dep-d87rpn8js32c73d1r9l0', 'deployStatus': 'su |
| 2026-05-22 02:20:57 | commit              | 42da854           | fix(dashboard): attribute sentinel-trader during fill-confir |
| 2026-05-22 02:22:23 | deploy_ended        | core              | {'deployId': 'dep-d87rr2sm0tmc738mdk4g', 'deployStatus': 'su |
| 2026-05-22 02:26:44 | commit              | 9e2a3a3           | fix(dashboard): show real engine names not executor name |
| 2026-05-22 02:27:59 | deploy_ended        | core              | {'deployId': 'dep-d87rtpkvikkc7395l5ug', 'deployStatus': 'su |

## Critical Fix → Affected Engines mapping

When auditing an engine's performance, check this table. If the
engine appears below, exclude all closures BEFORE the fix timestamp
from the performance evaluation.

| Fix commit | Engine(s) affected | Type | Pre-fix data still valid? |
|------------|--------------------|------|---------------------------|
| a9fd424 (2026-05-22) | ALL engines reading signal-bus | /health lock-free; bus was crash-looping | NO — bus was OOMing/slow before this |
| 4750268 (2026-05-21) | funding_triangulation | engine REMOVED (structural 8h lag) | N/A engine gone |
| b2f93c1 (2026-05-22) | hl_depth_shock | engine REMOVED (operator decision) | N/A engine gone |
| (prior session) | hl_settle_5m | EV-flipping trail-stop disabled then re-enabled today | Pre-trail-fix WR/PF data INVALID |
| (prior session) | hl_depth_shock | denylist INJ/TIA/WIF, DS_WINDOW_S 5→30, DS_SHOCK_PCT_MIN 40→60 | Pre-denylist n=9 was the bleed-source |

## Service availability (uptime gaps)

Engines could not trade during these windows even if 'enabled':

- **sentinel-trader**: suspended 05-21 06:29 UTC → resumed (still suspended)
- **spm-signal-bus**: suspended 05-21 06:29 UTC → resumed (still suspended)
- **spm-strategy-runner**: suspended 05-21 06:29 UTC → resumed (still suspended)

## How to use this file in audits

ALWAYS include this file's contents when invoking sentinel.

Audit template:
```
## Recent fixes — IGNORE DATA OLDER THAN THESE COMMITS
<paste relevant section of FIX_TIMELINE.md>

## Current question
<your audit target>
```

If you forget this and feed pre-fix data to the council, they will
audit the BUG not the current engine. The verdict will be wrong.