<!-- grillspec:begin -->
## grillspec

This repository uses grillspec phases (grill → spec → plan → execute); run `grillspec status` (the agent gets the exact command for this machine at session start) to see the current one.
<!-- grillspec:end -->

<!-- grillspec:memory:begin -->
## Project memory (grillspec)
- [constraint] YouTube quota is per Google project: all built-in sign-in users share 10,000 units/day (reset midnight PT); count units/day before adding YouTube polling (youtube-quota-budget)
- [gotcha] Twitch never echoes your own PRIVMSG; the sent message's real id arrives in USERSTATE's id tag (client-nonce isn't echoed back) (audit-2026-11-08)
- [gotcha] Twitch IRC checks the token only at connect: an expired token keeps a live connection, then gets "Login authentication failed" on the next reconnect (twitch-signin-resilience)
- [constraint] Twitch public-client refresh tokens are single-use: never cancel a refresh mid-flight; every token save must check the store is unchanged (twitch-signin-resilience)
<!-- grillspec:memory:end -->
