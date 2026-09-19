---
name: phone-agent
description: Place and manage user-approved AI voice calls to Japanese phone numbers.
---

# AI Phone Agent JP

Use `make_phone_call` only when the user has explicitly asked to place that specific call now.

Before calling, make sure the destination and the goal are clear. Keep the goal short and operational. Never invent a phone number. Set `confirmed=true` only when the user has clearly authorized the call.

The service is Japan-first and accepts only `+81` E.164 destinations. Do not use it for emergency services, premium-rate destinations, bulk calling, harassment, impersonation, or deceptive calls.

The callee is told that the call uses an AI voice. The agent should also identify itself as an AI assistant when starting the conversation.

Use `get_phone_call_status` when the user asks whether a call connected, completed, failed, or is still active.

Use `hangup_phone_call` when the user explicitly asks to stop an active call.
