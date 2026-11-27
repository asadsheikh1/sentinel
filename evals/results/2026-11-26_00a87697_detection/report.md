# Detection evaluation

- **Git SHA:** `00a8769722695401a1c4b4271e2343088930154b` · **AVIRO:** `2fd29930207202e85f4dfb72df1cbd6aefcd43fa`
- **When:** 2026-11-26T21:46:13+00:00 → 2026-11-27T08:12:02+00:00
- **Machine:** Apple M4, 24 GB, Darwin 25.6.0
- **Scenarios:** S01, S02, S03, S04, S05, S06, S07, S08, S09, S10, S11, S12, S13, S14, S15, S16 · **repeats:** 3
- **Detection deadline:** per scenario (default 300 s); runs are watched for `observe_s`.

## Summary

| Metric | Value |
|---|---|
| Runs (invalid: harness error) | 49 (0) |
| Detection precision | 46% (33 true, 38 false) |
| Detection recall (within deadline) | 55% · per repeat: mean 55%, min 43%, max 64% |
| Detected, but after the deadline | 4 runs |
| Time to detect (median / p90 / max) | 140.1 s / 275.1 s / 298.2 s |
| False positives per hour (fault-free: 2.5 h) | 1.2 |
| Grouping accuracy (exactly one incident) | 19% |
| Origin accuracy (host scenarios) | 0% |
| Gap classification accuracy | 83% |
| Agent gap without a service incident (S15) | 100% |
| Incidents about other things on this SENTINEL (excluded) | 0 |

Integration time to first data (every required checklist item ticked): app 111.5 s, host 136.4 s, check 61.0 s

## Per scenario

| Scenario | Detected (deadline) | TTD, s | Incidents (true / false) | One incident | Origin | Gap |
|---|---|---|---|---|---|---|
| S00 | 0/1 | - | [0] / [2] | 0/1 | - | - |
| S01 | 2/3 | 80.8, 218.8, 352.9 | [1, 1, 1] / [1, 5, 0] | 1/3 | - | - |
| S02 | 0/3 | - | [0, 0, 0] / [0, 1, 0] | 0/3 | - | - |
| S03 | 0/3 | - | [0, 0, 0] / [0, 1, 1] | 0/3 | - | - |
| S04 | 0/3 | 373.7 | [0, 1, 0] / [0, 1, 1] | 0/3 | - | - |
| S05 | 2/3 | 57.3, 133.3, 344.6 | [1, 1, 1] / [0, 1, 0] | 2/3 | 0 | - |
| S06 | 3/3 | 57.1, 91.4, 186.3 | [1, 1, 1] / [1, 1, 2] | 0/3 | - | - |
| S07 | 2/3 | 183.9, 257.7 | [0, 1, 1] / [1, 1, 0] | 1/3 | - | - |
| S08 | 0/3 | - | [0, 0, 0] / [1, 0, 0] | 0/3 | - | - |
| S09 | 2/3 | 171.1, 276.1 | [0, 1, 1] / [2, 2, 0] | 1/3 | - | - |
| S10 | 3/3 | 53.3, 62.0, 62.7 | [1, 1, 1] / [1, 1, 0] | 1/3 | - | - |
| S11 | 2/3 | 161.0, 275.1, 399.4 | [1, 1, 1] / [0, 1, 1] | 1/3 | - | - |
| S12 | 0/3 | - | [0, 0, 0] / [1, 1, 1] | 0/3 | 0 | - |
| S13 | 3/3 | 97.5, 140.1, 154.3 | [2, 2, 2] / [0, 0, 0] | 0/3 | 0 | - |
| S14 | 1/3 | 298.2 | [1, 0, 0] / [0, 1, 1] | 1/3 | 0 | - |
| S15 | 0/3 | - | [0, 0, 0] / [1, 1, 2] | 0/3 | - | 2 |
| S16 | 3/3 | 127.5, 139.8, 158.7 | [2, 2, 2] / [0, 0, 0] | 0/3 | - | 3 |

## Runs

| Scenario | Repeat | Fault start | Incidents | Signals seen | Notes |
|---|---|---|---|---|---|
| S00 | 1 | 08:26:04 | INC-130 SEV3 + (aviro_postgres_1); INC-129 SEV3 + (aviro_postgres_1) | error_group_new |  |
| S01 | 1 | 21:50:17 | INC-29 SEV3 + (aviro_postgres_1); INC-28 SEV1 + (aviro_orders_1, orders, gateway) | ServiceHighErrorRatio, error_group_new, error_group_spike | closed leftover INC-23; closed leftover INC-22 |
| S01 | 2 | 01:03:05 | INC-63 SEV3 + (orders); INC-62 SEV3 + (aviro_gateway_1); INC-61 SEV3 + (aviro_orders_1); INC-60 SEV3 + (aviro_payments_1); INC-59 SEV3 + (aviro_inventory_1); INC-58 SEV3 + (aviro_postgres_1) | error_group_new, error_group_spike |  |
| S01 | 3 | 04:55:40 | INC-95 SEV2 + (aviro_orders_1, orders) | error_group_new, error_group_spike |  |
| S02 | 1 | 22:00:51 | none | error_group_new |  |
| S02 | 2 | 01:13:47 | INC-64 SEV3 + (aviro-host) | HostFilesystemFillingUp |  |
| S02 | 3 | 05:05:22 | none | error_group_new | closed leftover INC-97; closed leftover INC-96 |
| S03 | 1 | 22:09:51 | none | error_group_new |  |
| S03 | 2 | 01:25:15 | INC-66 SEV3 + (aviro-host) | HostFilesystemFillingUp | closed leftover INC-65 |
| S03 | 3 | 05:15:55 | INC-99 SEV3 + (aviro_worker_1) | ContainerRestarted |  |
| S04 | 1 | 22:18:44 | none | error_group_new |  |
| S04 | 2 | 01:35:45 | INC-68 SEV3 + (worker); INC-67 SEV3 + () | error_group_spike |  |
| S04 | 3 | 05:27:29 | INC-101 SEV3 + (aviro_worker_1) | error_group_spike |  |
| S05 | 1 | 22:27:42 | INC-37 SEV1 + (orders, gateway) | ServiceHighErrorRatio, error_group_new, error_group_spike | closed leftover INC-35 |
| S05 | 2 | 01:46:14 | INC-70 SEV3 + (orders); INC-69 SEV3 + (aviro-host) | HostFilesystemFillingUp, error_group_spike |  |
| S05 | 3 | 05:38:06 | INC-104 SEV2 + (orders, payments) | ServiceLatencyHigh, error_group_spike |  |
| S06 | 1 | 22:38:12 | INC-40 SEV3 + (aviro_inventory_1); INC-39 SEV3 + (gateway) | ContainerRestarted, error_group_new |  |
| S06 | 2 | 02:06:53 | INC-72 SEV3 + (aviro_postgres_1); INC-71 SEV3 + (aviro_inventory_1) | ContainerRestarted, error_group_new |  |
| S06 | 3 | 05:46:33 | INC-107 SEV3 + (aviro_postgres_1); INC-106 SEV3 + (aviro_inventory_1); INC-105 SEV3 + (gateway) | ContainerRestarted, ServiceLatencyHigh, error_group_new |  |
| S07 | 1 | 22:50:14 | INC-43 SEV3 + (aviro_postgres_1) | ServiceHighErrorRatio, error_group_new, error_group_spike |  |
| S07 | 2 | 02:19:12 | INC-74 SEV3 + (aviro_postgres_1); INC-73 SEV3 + (orders) | error_group_new, error_group_spike |  |
| S07 | 3 | 05:58:16 | INC-108 SEV3 + (orders) | error_group_spike |  |
| S08 | 1 | 23:00:40 | INC-44 SEV3 + (aviro_postgres_1) | error_group_new |  |
| S08 | 2 | 02:27:47 | none | - |  |
| S08 | 3 | 06:06:42 | none | - |  |
| S09 | 1 | 23:12:09 | INC-46 SEV3 + (aviro_orders_1); INC-45 SEV3 + () | error_group_spike |  |
| S09 | 2 | 02:39:09 | INC-78 SEV3 + (aviro_postgres_1); INC-77 SEV3 + (orders); INC-76 SEV3 + (aviro_orders_1) | error_group_new, error_group_spike |  |
| S09 | 3 | 06:18:14 | INC-109 SEV3 + (orders) | error_group_spike |  |
| S10 | 1 | 23:22:23 | INC-48 SEV3 + (inventory); INC-47 SEV3 + (aviro_inventory_1) | error_group_new |  |
| S10 | 2 | 02:49:07 | INC-81 SEV2 + (aviro_inventory_1, aviro-host, aviro_postgres_1); INC-80 SEV1 + (inventory, gateway) | HostFilesystemFillingUp, ServiceHighErrorRatio, error_group_new, error_group_spike | closed leftover INC-79 |
| S10 | 3 | 06:29:49 | INC-111 SEV3 + (inventory) | error_group_spike | closed leftover INC-110 |
| S11 | 1 | 23:30:45 | INC-49 SEV2 + (aviro_orders_1, orders) | error_group_new |  |
| S11 | 2 | 03:11:07 | INC-83 SEV3 + (orders); INC-82 SEV3 + (aviro_orders_1) | error_group_spike |  |
| S11 | 3 | 06:38:16 | INC-113 SEV3 + (orders); INC-112 SEV3 + (aviro_postgres_1) | error_group_new, error_group_spike |  |
| S12 | 1 | 23:42:24 | INC-51 SEV3 + (aviro-host) | HostFilesystemFillingUp | closed leftover INC-50 |
| S12 | 2 | 03:21:28 | INC-85 SEV3 + (aviro-host) | HostFilesystemFillingUp |  |
| S12 | 3 | 06:49:44 | INC-117 SEV3 + (inventory) | ServiceLatencyHigh | closed leftover INC-116; closed leftover INC-115 |
| S13 | 1 | 00:03:06 | INC-53 SEV2 + (aviro_postgres_1, aviro-host); INC-52 SEV2 + (payments, orders) | HostFilesystemFull, error_group_new, error_group_spike |  |
| S13 | 2 | 03:49:39 | INC-88 SEV2 + (aviro_postgres_1, aviro-host); INC-87 SEV1 + (orders, aviro_orders_1, inventory, worker) | HostFilesystemFull, error_group_new, error_group_spike | closed leftover INC-86 |
| S13 | 3 | 06:59:17 | INC-120 SEV2 + (orders, payments); INC-119 SEV2 + (aviro_postgres_1, aviro-host) | HostFilesystemFillingUp, HostFilesystemFull, error_group_spike |  |
| S14 | 1 | 00:16:29 | INC-54 SEV3 + (aviro_postgres_1, aviro-host) | HostHighCpu, error_group_new |  |
| S14 | 2 | 04:08:54 | INC-89 SEV3 + () | telemetry_gap |  |
| S14 | 3 | 07:19:31 | INC-121 SEV3 + (aviro_postgres_1) | error_group_new |  |
| S15 | 1 | 00:37:45 | INC-55 SEV3 + () | telemetry_gap |  |
| S15 | 2 | 04:30:24 | INC-90 SEV3 + () | - |  |
| S15 | 3 | 07:41:05 | INC-124 SEV3 + (); INC-123 SEV3 + (aviro-host) | HostHighCpu, telemetry_gap |  |
| S16 | 1 | 00:48:53 | INC-57 SEV3 + (); INC-56 SEV2 + (eval-aviro-health-bf944ec5) | EndpointDown, EndpointSlow, telemetry_gap |  |
| S16 | 2 | 04:41:09 | INC-92 SEV3 + (); INC-91 SEV2 + (eval-aviro-health-bf944ec5) | EndpointDown, EndpointSlow, telemetry_gap |  |
| S16 | 3 | 07:51:45 | INC-126 SEV3 + (); INC-125 SEV2 + (eval-aviro-health-bf944ec5) | EndpointDown, EndpointSlow, telemetry_gap |  |
