# Private ASI counteroffer notifications

An authorized owner sends `NOTIFY HERE` in their private chat with their personal
agent. This explicitly enrolls that address and current session, replacing any
previous enrollment for that address. Known peer-agent addresses cannot enroll.
Enrollment lasts until the agent restarts. The protocol does not establish that
a conversation is private: the owner must choose a private chat for enrollment.

When a counteroffer requires review, the agent stores the pending offer first.
If enrolled, it sends the private message in the owner's session using
`ctx.send_raw()` with `chat_protocol_spec.digest` and the ChatMessage schema.
This avoids inheriting the peer handler's protocol digest. The original session
is restored in `finally` before sending the coordinator's status reply.
The session override uses the same private SDK attribute as `use_plan_session()`;
the explicit protocol argument is supported by the installed SDK.

Each attempted notification records its message ID, owner, session, transport
status and correlated acknowledgement in process memory. Acknowledgements must
match the owner, current authorization and enrolled session used for that send.
Records remain available after offer cleanup to correlate late acknowledgements.
They currently have no retention limit. Neither transport acceptance nor a chat
acknowledgement proves that ASI displayed the message or that the human saw it.
Private notification diagnostics contain no owner address, body or exception detail.
Existing unrelated chat logging is unchanged.

Without enrollment, or after dispatch failure, the offer remains pending and
`OFFER` retrieves it. `approval_required` continues to mean review is pending,
not that delivery succeeded. No automatic retries or deadline extensions occur.
Approval/rejection, persistence, scoring and TRACE are unchanged. Nothing here
integrates Photon/iMessage.

## Controlled live test (requires operator approval)

1. Resolve overlapping agent processes before launch; never run two copies of
   the same identity. Do not terminate existing agents automatically.
2. Use the disposable demo checkout prepared from `9514ec4`. It does not yet
   contain this change: copy only the reviewed `personal_agent.py` from this
   feature working tree into that disposable checkout. Do not copy `.env`,
   `data/`, persona overrides or secrets. Keep the original workspace data intact.
3. If the disposable checkout has already been used for an approval, use a fresh
   disposable checkout with committed default personas instead. Do not reset real
   preferences. Confirm offline that Jordan alone vetoes the demo candidate for
   availability before launching.
4. Use the existing demo launcher, which reads configuration without copying it,
   removes `DATABASE_URL` and unrelated integration keys from every child, and
   enables `NEGOTIATION_DEMO`. Run its `--check` first. Supply authorized owner
   configuration privately; Jordan needs exactly one owner. Do not print values.
5. After approval to launch, start the four demo agents with that launcher.
   In Jordan's private ASI chat, send `NOTIFY HERE` and verify its confirmation.
6. In Alex's authorized chat, send `PLAN`. Do not send `OFFER`. Verify whether the
   counteroffer appears in the enrolled Jordan chat without a new inbound message.
   Record UI visibility separately from sanitized transport/acknowledgement logs.
7. Within the 180-second review deadline, send `APPROVE <new_plan_id>` to Jordan.
   Confirm Alex automatically rescores and proposes a plan. Send `TRACE` to Alex;
   expect APPROVAL_REQUIRED, APPROVED, RESCORING_STARTED and PLAN_PROPOSED.
8. In a fresh disposable run, test `REJECT <new_plan_id>` and no-enrollment/OFFER
   recovery. Also test an inactive owner chat separately from an open chat.
   Do not invoke BOOK. Approval writes only disposable local preferences when
   using the database-disabled launcher.

If no proactive prompt appears, keep OFFER as recovery and investigate ASI's
session/unsolicited-message policy. Do not report transport success as UI success.
Existing stale round-2 replies and coordinator/owner deadline skew remain risks;
this notification change does not alter those protocols.
