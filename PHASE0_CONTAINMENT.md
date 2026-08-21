# Phase 0 containment status

Classification: **UNVERIFIED_PRODUCTION**  
Promotion eligible: **NO**

Gemini credentials are sent in the `x-goog-api-key` header rather than the URL.
A centralized logging filter redacts sensitive query parameters, authorization
values, Discord webhooks, key-like values, known configured secrets, and raw
exception output. The dashboard rejects non-loopback binds, and state-changing
HTTP methods require `Authorization: Bearer $CTW_DASHBOARD_AUTH_TOKEN`.

This source change does not prove historical containment. Before Phase 0 closes,
an operator must scan the current tree, complete Git history, CI artifacts,
release artifacts, and retained logs, then rotate every credential that may
have appeared. Record only the rotation owner, time, and verification result in
the fleet ledger—never the credential value.
