# Security and pilot boundaries

Do not put live Zendesk OAuth tokens, role keys, ticket text, SQLite databases, or customer exports in Git. `LIVEOPS_*_KEY` and `RESOLUTION_ENGINE_KEY` are local secrets. Use unique high-entropy values and rotate them after compromise. The browser holds one role key in session storage until the tab session ends; use a dedicated local browser profile for a real pilot.

The server accepts only numeric loopback addresses. Treat its host as trusted; do not publish its port with tunnels or reverse proxies. The Hub SQLite database stores generated answer text and ticket identifiers; place it in a private, access-controlled directory and define a deletion schedule with the customer. SQLite and filesystem permissions are a local pilot safeguard, not encryption at rest. The installed app and the Hub should run under a dedicated OS user.

No action in an uncertain state should be repeated until a human reconciles the installed app state and the Zendesk ticket. Synthetic demo drafts are blocked from send by the server-side state machine.

Report security issues privately to the repository owner; do not include customer data in a public issue.
