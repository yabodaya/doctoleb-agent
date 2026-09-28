# Slices
Work in order. Set exactly one slice to IN PROGRESS at a time.

| Slice | Name | Status |
|---|---|---|
| VS-001 | Project skeleton | DONE |
| VS-002 | Messaging database | DONE |
| VS-003 | Receive WhatsApp webhook | PARTIAL |
| VS-004 | Worker + send reply | PARTIAL |
| VS-005 | AI replies | IN PROGRESS |
| VS-006 | Agent Core + first tool | TODO |
| VS-007 | Booking tools | TODO |
| VS-008 | Voice notes in | TODO |
| VS-009 | Voice note replies | TODO |
| VS-010 | Human handoff | TODO |
| VS-011 | Connect real Booking Service | BLOCKED |

**PARTIAL** means the code is complete and tested and only the live test against
Meta is outstanding.

- VS-003 is **merged**; its live verification never ran.
- VS-004 is **merged** to `main`; its live test (Task 10 of `docs/plans/VS-004-plan.md`) has not run.

Both are waiting on the same sitting: the first real WhatsApp message proves both,
so they are tested together. See `docs/plans/VS-004-plan.md`, Task 10 — which
leads with the two most likely reasons Meta is not delivering yet.
VS-005's own live test (Task 9 of `docs/plans/VS-005-plan.md`) needs that sitting to have worked first.
