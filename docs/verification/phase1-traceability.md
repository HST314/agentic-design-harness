# Phase 1 acceptance traceability

This matrix maps RFC v0.2 section 19 to the work package and executable evidence
that must close it. `planned` rows belong to later P1 packages and are not
claimed by P1-00 through P1-03.

| RFC 19 | Work package | Evidence | P1-03 status |
| --- | --- | --- | --- |
| 1 | P1-03/P1-04 | task/input command tests; asset import tests | foundation |
| 2 | P1-03/P1-07/P1-08 | manual/auto plan tests; three-process E2E | foundation |
| 3 | P1-07/P1-14 | supervisor isolation E2E | planned |
| 4 | P1-07/P1-12 | instance API and browser E2E | planned |
| 5 | P1-05/P1-13 | allocation, crash and sticky-pair tests | planned |
| 6 | P1-06 | global/instance revision integration tests | planned |
| 7 | P1-02/P1-04 | layout and path-boundary tests | foundation |
| 8 | P1-04 | adversarial publication tests | planned |
| 9 | P1-11/P1-12 | resource browser E2E | planned |
| 10 | P1-09 | owner freeze and FIFO tests | planned |
| 11 | P1-08/P1-10/P1-12 | usage completeness and UI tests | planned |
| 12 | P1-09 | notification/deep-link tests | planned |
| 13 | P1-02/P1-07/P1-13 | crash recovery and no-replay E2E | foundation |
| 14 | P1-03/P1-08 | topology and unavailable adapter tests | foundation |
| 15 | P1-03 | delayed required-PPT blocking tests | complete |
| 16 | P1-03/P1-09 | aggregation priority tests | complete |
| 17 | P1-07 | process cancellation retention test | planned |
| 18 | P1-10/P1-13 | concurrent retry-budget adversarial tests | planned |

## P1-00 fixtures

- Test port range: `18100-18199`; allocation tests bind before claiming a port.
- Temporary root: test-framework-owned directories only; no repository runtime
  state is reused between tests.
- Credential pairs: `tests/fixtures/p1/credential-pairs.json`. Values are
  non-routable, test-only markers and are never accepted by a real smoke.
- Fake Image mode: no network, deterministic jobs, explicit token usage and
  deterministic candidate files.
- Real Provider smoke: disabled by default and CI; requires
  `HARNESS_REAL_PROVIDER_SMOKE=1`, an allow-listed Provider and environment-only
  credentials. It may create one bounded request and must redact all output.
