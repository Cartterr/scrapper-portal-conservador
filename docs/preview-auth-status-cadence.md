# Preview and authentication status cadence

The worker samples the protected-form DOM signature immediately before each
account preview capture (normally every five seconds). The dashboard polls every
two seconds. This avoids waiting for the separate 30-second recovery scan when
an operator completes login or the page finishes rendering during a cooldown.

This sampling is passive: no navigation, login submissions, proxy changes,
browser replacement, or cooldown clearing. Login state and search availability
remain separate. Unknown/conflicting DOM still fails closed, while the
ever-authenticated lifecycle protection remains latched.

This is near-real-time only while the synchronous worker can service its loop;
long browser operations can delay both samples. A screenshot and DOM reading
are adjacent observations, not an atomic snapshot. Never infer authentication
from an image alone or claim a hard five-second latency guarantee.

The initial installation of the live-update adapter requires an explicitly
authorized migration. Once installed, passive evidence policy changes in
`runtime_observation.py` use the versioned update command described in
`runtime-live-updates.md`, without restarting the owner or its browsers.
