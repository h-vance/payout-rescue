# Test fixtures

- `claude_cli_reference.json` is a synthetic CLI response for parser and trace tests. Its usage and cost values are invented.
- `connect_payout_events_for_a_failed_payout.json` contains two recorded sandbox events from 2026-10-11. Resource identifiers have been replaced and request metadata removed. Both events carry a failed payout, including the event named `payout.paid`.
- `real_drafts.json` contains recorded sandbox drafts from 2026-10-11, with resource identifiers replaced. Messages, token counts and costs remain historical observations.

Keep new raw recordings in the ignored `artifacts/` directory and sanitize them before sharing.
