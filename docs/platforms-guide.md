# Testing a Vapi, Retell, ElevenLabs or LiveKit agent

Vocto can call an agent that runs on a hosted voice platform directly, with your API key: no phone
number, no phone bill, no change to the agent. The simulated caller really speaks and listens, and
after every call Vocto reads the platform's own record of it. So the checks and AI judges see the
tools the agent called on the platform, with their arguments and answers, as if Vocto had watched
them itself.

| Platform | How Vocto calls it | What Vocto reads after the call |
|---|---|---|
| Vapi | A Vapi call over its WebSocket transport (raw 16 kHz audio both ways) | `GET /call/{id}`: tool calls and results, why it ended, cost, Vapi's latency, its summary |
| Retell | A Retell web call, the way Retell's own web SDK joins it | `GET /v2/get-call/{id}`: tool calls and results, why it ended, cost, Retell's latency |
| ElevenLabs | The ElevenLabs Agents WebSocket | `GET /v1/convai/conversations/{id}`: tool calls and results, why it ended, credits used |
| LiveKit | A new room on your LiveKit server, with your agent sent into it | LiveKit keeps no record: the agent reports its own tool calls (below) |

## What you need

| Platform | API key | Agent |
|---|---|---|
| Vapi | Dashboard › Organization › API keys: the **private** key | Assistants › your assistant › its ID |
| Retell | Dashboard › Settings › API keys | Agents › your agent › Agent ID |
| ElevenLabs | Developers › API keys (optional for a public agent, but needed to read the call's record) | Agents › your agent › Agent ID |
| LiveKit | Cloud › Settings › API keys: the key **and** the secret (or your own server's) | The `agent_name` your agent registers with, and the server address (`wss://…livekit.cloud`) |

## Set it up (in the dashboard)

1. **Agents › New agent › Voice › Hosted platform**, pick the platform and paste the agent's ID
   (or, for an existing agent, **Connect › How Vocto reaches the agent › Hosted platform**).
2. In **Connect**, an **admin** pastes the API key (LiveKit: the key and the secret) and presses
   **Save key**, then **Save**. The key goes into Settings › Secrets on this computer; the agent's
   file only names it, as `${VAPI_API_KEY_MY_AGENT}`. Nobody can read it back: the page shows only
   whether it is saved.
3. Press **Check … key and agent**. It makes no call: it checks the key and that the agent exists.
4. Press **Make a test call**. A short real call (billed by the platform): Vocto listens for the
   greeting, hangs up, reads the platform's record and shows what it heard, the call id and its cost.
5. Run the **Functional** tests as for any agent.

**Values for the agent's prompt.** Under Connect you can set `{{variables}}` the platform fills into
the agent's prompt (Vapi `variableValues`, Retell dynamic variables, ElevenLabs dynamic variables,
LiveKit room metadata). A value starting with `$` takes the test caller's fact of that name. With
**The agent is told it when the call starts** on, Vocto also sends `vocto_test_id`, so your tools
and database can be linked to the test call.

## How the platform's tool calls get into the call

After the call, each tool call in the platform's record becomes a tool call in the call's evidence,
marked **Reported by Vapi** (or Retell...) in the call's timeline, placed where it happened:

1. at the moment the platform announced it during the call, when it did (Vapi `tool-calls`,
   ElevenLabs `agent_tool_response`);
2. otherwise at its time in the platform's record, moved onto Vocto's clock by lining up the
   agent's greeting in the record with the greeting in Vocto's recording;
3. otherwise just before the call ended.

A tool call is never counted twice: one Vocto answered itself (an ElevenLabs **client tool**, which
goes through Vocto's tool gateway, so failure drills work on it) or one the tool gateway already saw
is skipped. A transfer the platform records (Vapi `assistant-forwarded-call`, Retell
`call_transfer`, a `transferCall` / `transfer_to_number` tool) counts as a hand-off to a person.

The platform's cost goes into the run's costs: per call when the call has a bill in the run's
currency; and when you set no prices, the run total is the platform's bills (only when every call
has one, so the total is never too low). ElevenLabs reports credits; they are shown on the call, and
counted as money only when ElevenLabs also gives the call's cost in dollars.

If the record can't be read (wrong key, the platform is slow), the call still runs and is judged on
what Vocto heard; the call page says the record is missing, and tool checks that need it fail rather
than pass.

## LiveKit: the agent reports its tool calls

LiveKit has no record of an agent's tool calls. Add these lines to your agent, so each tool call is
sent to the room as a data message on the topic `vocto.tool` (only participants in the test room,
that is Vocto, receive it):

```python
import json
from livekit.agents import function_tool, get_job_context

async def report_tool(name: str, args: dict, result, ok: bool = True) -> None:
    room = get_job_context().room
    await room.local_participant.publish_data(
        json.dumps({"name": name, "args": args, "result": result, "ok": ok}), topic="vocto.tool")

@function_tool
async def book_appointment(context, date: str, time: str):
    result = await my_backend.book(date, time)
    await report_tool("book_appointment", {"date": date, "time": time}, result)
    return result
```

Or route the agent's tools through Vocto's tool gateway (Tools & data), as for any agent.

## Security

- **Keys never reach the browser, the logs or the project.** Keys are typed once by an admin, kept
  in `.env` on the Vocto computer (owner-only), and the agent's file holds only `${NAME}`. A key
  typed into the file itself is refused. Every message from a platform connector has the key, the
  signed URL and the room token removed before it is shown or saved.
- **A key only goes to its platform.** The API hosts are fixed in the code (api.vapi.ai,
  api.retellai.com, api.elevenlabs.io); an agent's file can't change them. Only the environment of
  the machine running Vocto can (`VOCTO_VAPI_API_BASE`, `VOCTO_RETELL_API_BASE`,
  `VOCTO_ELEVENLABS_API_BASE`, `VOCTO_RETELL_ROOM_URL`), for a proxy or for tests. Every address
  still goes through the SSRF guard (`netguard.py`), and a redirect to another host drops the key.
- **Admin only where it matters.** Saving a key, changing which secret a connection uses, or moving
  the address a secret is sent to (LiveKit's server, an agent URL with an auth header, a tool's
  upstream with secret headers, the REST backend or log server with secret headers) needs an admin.
  Changing the agent's ID on Vapi, Retell or ElevenLabs is an editor's change, since the key can
  only go to that platform anyway.

## What to test live (with real accounts)

The connectors are tested against faithful fakes of each platform's API; these need one real run:

1. **Vapi**: Check key and agent, Make a test call (the call shows in Vapi's dashboard with the
   cost), then a Functional run. In the call's timeline, the tool cards say "Reported by Vapi".
2. **Retell**: the same. The web call joins Retell's LiveKit room; if your Retell workspace uses a
   different room address, set `VOCTO_RETELL_ROOM_URL` on the Vocto computer.
3. **ElevenLabs**: the same, with an agent that has one client tool (answered by Vocto) and one
   server tool (read from the record).
4. **LiveKit**: with your agent running (`python agent.py dev`), Make a test call, then a run with
   the `vocto.tool` lines added.
