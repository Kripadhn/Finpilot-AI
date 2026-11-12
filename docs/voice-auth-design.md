# Voice authentication design note

> Maximum 1 page. No code.

## Voice enrolment at eKYC

During digital onboarding, after document and face checks succeed, the customer records 3–5
short utterances: one fixed passphrase and several prompted random phrases (so both text-dependent
and text-independent verification are possible later). The audio is quality-gated (SNR, duration,
clipping), checked for liveness at enrolment itself, and converted into a **voiceprint
embedding** by a speaker-encoder model. Only the embedding and quality metadata are stored —
encrypted, in the customer's KYC record, never the raw audio beyond the regulatory retention
window. Enrolment is bound to the verified eKYC identity and recorded as an auditable consent
event (biometric data requires explicit consent under UAE PDPL/GDPR-class rules).

## Verification of a live voice against the enrolled voiceprint

When the customer calls or opens the voice channel, they are asked to speak a **random prompted
phrase** (never the same twice). The platform runs two checks in parallel: the spoken words must
match the prompted phrase (ASR match — defeats replay of old recordings), and the speaker
embedding of the live audio is compared to the enrolled voiceprint producing a similarity score.
Scores are calibrated per channel (telephony vs app microphone) and combined with context
signals — device binding, SIM/caller-ID consistency, geolocation plausibility — into a single
**confidence score** with two thresholds: accept, and grey-zone.

## Liveness and anti-spoofing (replay and cloned voice)

- **Replay**: random prompted phrases plus acoustic replay detection (loudspeaker artefacts,
  channel/pop characteristics); reject if the utterance matches a previously seen recording
  fingerprint.
- **Synthetic/cloned voice**: a dedicated deepfake/spoof-detection model (trained on TTS and
  voice-conversion artefacts) scores every sample; continuous liveness runs during the whole
  session, not only at login, so a mid-call swap to a clone is caught.
- Model scores are monitored for drift and recalibrated; spoof attempts raise fraud alerts and
  are rate-limited per account and per originating number/device.

## Step-up authentication when confidence is low

Grey-zone score, spoof suspicion, high-risk intent (even read-only but sensitive), or a new
device triggers **step-up**: a second factor through a separately enrolled channel — app push
confirmation with device biometrics, or OTP to the registered number — optionally plus a
knowledge check handled by a human agent for the highest risk. Repeated failure locks the voice
channel and routes to manual identification. Step-up outcomes feed back into the risk engine.

## From verified identity to session token

On accept, the authentication service issues a short-lived, scoped **session token** (opaque
reference token): bound to `customer_id`, channel, and device fingerprint; scopes limited to
read (`accounts:read`, `transactions:read`); TTL of minutes with idle expiry; revocable
centrally. Downstream systems — the agent and MCP server in this repository — never see voice
data or identity claims; they only hold the token and must **introspect** it
(`/sessions/introspect`) on every use, receiving `active`, `customer_id`, `expires_at` and
`scopes`. Step-up upgrades the same session with additional scopes rather than issuing broader
tokens by default; logout and fraud signals revoke the token immediately.
