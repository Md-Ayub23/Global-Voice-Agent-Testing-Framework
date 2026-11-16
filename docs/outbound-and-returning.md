# Outbound calls, voicemail and returning callers

Most agents answer calls. Some make them (reminders, sales, follow-ups), and every agent gets callers
who ring back. A test case can do all three: in the dashboard, open a test case and use **Who makes
the call** and **Calls back later**, or write it in YAML.

## The agent rings the customer

```yaml
direction: outbound
outbound:                      # told to the agent with the call (call metadata "outbound")
  reason: appointment reminder
  patient: $full_name          # $name = a caller fact
answered_by: person            # or voicemail
pickup: "Hello, Maria speaking."
```

Vocto tells the agent the call is outbound (`direction` and `outbound` in the call's metadata: the
`session_meta` of a Python agent, `meta` of an HTTP or websocket agent, custom parameters on a Twilio
stream). Then whoever picks up talks: a person says the pickup line and carries on like any test
caller. Words the agent says before anyone answered are kept and marked "said before anyone picked up".

The AI judges add "Opened its call properly" (who is calling, why, checked it reached the right person
before sharing anything private).

## Voicemail

```yaml
direction: outbound
answered_by: voicemail
voicemail:
  greeting: "Hi, you've reached Maria. Leave a message after the tone."
  beep: true
  max_message_s: 60
checks:
  - id: good-voicemail
    type: voicemail
    must_say: ["(?i)riverside clinic", "555[ -]?0100"]     # who is calling, how to call back
    never_say: ["\\b\\d{6}\\b"]                            # e.g. a booking reference
    max_seconds: 30
    severity: blocker
```

Voice runs play the greeting, a beep, then silence while the voicemail records. The `voicemail` check
fails when the agent left no message (or left one when `message: false`), started talking before the
beep, ran too long, missed a `must_say`, or said a `never_say`. The judges add "Left a clear, safe
voicemail".

## Returning callers

```yaml
remember:
  booking_ref: "reference is (\\d{6})"     # found in what the agent said or its tools answered
returning:
  - gap_s: 2
    turns:
      - caller: "Hi, I'm calling about booking {booking_ref}."
        expect:
          - {type: said, pattern: "(?i)tuesday"}
  - goal: "Cancel booking $booking_ref."
checks:
  - {id: booked-once, type: tool_called, tool: book_appointment, times: 1}
  - {id: no-new-booking-on-call-2, type: tool_not_called, tool: book_appointment, call: 2}
```

After the first call the same caller rings again, once per `returning` entry, with nothing reset in
the agent's systems. Each entry is scripted lines or a goal for the AI caller. `remember` carries
details from the earlier calls into the later ones (`{name}` in lines, `$name` in goals). A check with
`call: 2` looks at that call only; one without `call`, and the outcome, look at the whole chain.
Scripted lines on call 2 are checked as `call2.turn1.1`, and so on. Set `caller_id` (a number or
`$phone`) to send the agent the caller's number as the call's "from", so it can recognise them.

The judges add "Picked up from the earlier call".

## On a real phone line

See docs/server.md (Outbound agents on a real phone line) and docs/phone-guide.md.
