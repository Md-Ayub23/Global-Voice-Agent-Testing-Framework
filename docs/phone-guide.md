# Testing a real phone number

Vocto can ring the agent's real phone number, the same number your callers dial. The simulated caller
talks to the agent over a real phone call, and every check, judge and report works as it does for any
voice test. This tests the whole phone line: the carrier, the phone provider, the agent's speech
services and the agent itself.

Vocto places the calls through **your own Twilio or Plivo account**: whichever your team already uses.

## What you need

### With Twilio

| What | Where to get it |
|---|---|
| A Twilio account that is **upgraded** (not a trial) | twilio.com › Console › Upgrade. A trial account plays "You have a trial account…" and waits for a key press at the start of every call, so the test can't start. |
| A Twilio phone number | Console › Phone Numbers › Manage › Buy a number. Test calls come from this number. A verified caller ID also works. |
| The Account SID and Auth token | Console › Account info. |
| The agent's phone number | From the agent's team, in international format: `+` then the country code, e.g. `+14155550123`, `+919876543210`. |

### With Plivo

| What | Where to get it |
|---|---|
| A Plivo account with credit | plivo.com › Console. A free trial account can only call the numbers you verified in it (Plivo's sandbox numbers), so add credit before testing a client's number. |
| A Plivo phone number | Console › Phone Numbers › Buy Numbers. Test calls come from this number. |
| The Auth ID and Auth token | Console › Overview (Account). |
| The agent's phone number | As above, in international format. |

## Set it up (in the dashboard)

1. **Settings › Secrets**: add the two values. Twilio: `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN`.
   Plivo: `PLIVO_AUTH_ID` and `PLIVO_AUTH_TOKEN`. The profile only holds the names, such as
   `${TWILIO_AUTH_TOKEN}`, never the values.
2. **Agents › New agent › Voice › Phone number** (or, for an existing agent, **Set up › Connection › Phone number**).
3. Pick the **Phone provider** (Twilio or Plivo). Fill in the agent's phone number and **Your Twilio number**
   (or **Your Plivo number**). Leave the rest as it is.
4. Press **Check Twilio account** (or **Check Plivo account**). It rings no one. It checks the keys, shows the
   account (a Twilio trial account is flagged, because it can't be used), and checks that your number
   belongs to the account.
5. Press **Make a test call**. It really rings the agent, listens up to 12 seconds for its greeting,
   hangs up, and shows how long the agent took to speak and what the call cost.
6. Run tests as usual from **Test modes**, or from a terminal:
   `uv run python -m vatf run projects/<agent> --profile profile.yaml`

## How a call works

1. Vocto asks the provider to ring the agent's number from your number.
2. When the agent answers, the provider sends the call's audio to Vocto's phone line over a secure
   WebSocket, and plays back what Vocto's caller says (8 kHz phone audio, both ways). With Plivo, the
   provider first asks Vocto's phone line for the call's instructions, and tells it how a call that
   never connected ended (busy, no answer).
3. The provider needs a public `https://` address for Vocto's phone line. Vocto opens one by itself for
   each run: a free Cloudflare quick tunnel (`https://<random words>.trycloudflare.com`). No account
   or sign-up is needed. The `cloudflared` program is downloaded once into `models/tools/`.
4. When the caller is done, Vocto hangs up. It then asks the provider for the call's length and price,
   which appear on the call's page (**Real phone call** under the recording) and in `events.jsonl`.
   Plivo writes its call record a few seconds after the call, so Vocto waits for it briefly.

If your team already has its own public address (its own domain, a reverse proxy or ngrok), put it in
**Public address for Vocto's phone line**. It must start with `https://` and forward to port **8766**
on the computer running Vocto (change the port with the `line_port` option).

## Good to know

- **Every test call is a real, billed call**: Twilio or Plivo charges your account per minute, and the agent's own
  provider may charge too. A run of 10 scenarios × 3 repeats is 30 calls.
- **Response times include the phone network**, as your callers hear them. Expect a few hundred
  milliseconds more than on a direct WebSocket test, and set the limits in **Pass rules** to what your
  callers should get.
- **The agent's tool calls aren't visible over the phone.** Vocto can't sit between the agent and its
  APIs on a real line. Use **Tools & data › Database** or **Logs** so the checks see what the agent
  really did, not only what it said.
- **Calls go out about one a second**, a safe pace for both providers' default limits. A load test with
  many calls at once starts them one a second; ask your provider to raise the limit for bigger load tests.
- **Errors are in plain words**: "the number was busy", "no one answered", "Twilio refused the account
  SID or auth token", "Plivo refused the Auth ID or Auth token", "TWILIO_AUTH_TOKEN has no value yet". A call that never connects is Vocto's or the
  network's problem, never counted against the agent.

## In the profile

```yaml
connection:
  type: phone_call
  target: "+14155550123"                # the agent's number
  options:
    provider: twilio
    account_sid: ${TWILIO_ACCOUNT_SID}
    auth_token: ${TWILIO_AUTH_TOKEN}
    from_number: "+14155550199"         # your Twilio number
    ring_timeout_s: 30                  # optional
    public_url: ""                      # optional: your own https:// address (port line_port, default 8766)
```

With Plivo, only these change:

```yaml
    provider: plivo
    account_sid: ${PLIVO_AUTH_ID}         # the Auth ID
    auth_token: ${PLIVO_AUTH_TOKEN}
    from_number: "+14155550199"         # your Plivo number
```
