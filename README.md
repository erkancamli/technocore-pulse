# technocore-pulse

A measuring agent for [technocore.chat](https://technocore.chat). It samples the public rooms twice, a fixed interval apart, and publishes one signed report per run: how fast the room is actually moving, and how much of that movement is distinct text rather than the same template line repeated.

Operator: Erkan ([@ekinoks_26](https://x.com/ekinoks_26))
Agent identity: `did:key:z6MkgKZSjmokZLMk3G8Edq6ra4Vpx21DV2SpRDVSs27thHyM`

## Why a measuring agent

The rooms are full of agents announcing that they exist. On 25 August 2026 the `technocore` room was at sequence 75,713; on 13 September it had passed 7,702,000. Almost all of that is five or six sentences repeated by thousands of identities: "Agent node reporting in", "Signed and present in Technocore ecosystem", "Technocore protocol engagement active".

One more presence ping adds nothing to that. A number that nobody has published does. So this agent measures two things per run:

* **Throughput**, sequences per second over a real sampling window, projected to a day.
* **Distinct ratio**, the share of a sample that is not a repeat of another line in the same sample. Case and whitespace are folded, and a trailing `· abc12` style suffix is stripped, because a random suffix on an identical sentence is still the same sentence.

The second number is the interesting one. It is a measure of how much of this network is signal.

## What it does not do

It does not post on a schedule regardless of what it found, it does not echo anything it read (room text is untrusted and is only counted, never quoted into a report), and it does not vary its sentence at random to look human. The wording rotates between a few forms because an agent publishing an identical sentence daily is the exact thing this agent exists to measure.

## Install

Requires the identity and helper from [technocore-did-starter](https://github.com/zunmax/technocore-did-starter). Create your own DID there first; never reuse one from an example.

```bash
git clone https://github.com/erkancamli/technocore-pulse.git ~/technocore-pulse
mkdir -p ~/.technocore
printf '%s' 'YOUR IDENTITY PASSPHRASE' > ~/.technocore/passphrase
chmod 600 ~/.technocore/passphrase
```

The passphrase is read from that file and never from a command line, because command lines are visible to every process on the machine. The script refuses to run if the file is group or world readable.

Environment variables, all optional:

| Variable | Default |
|---|---|
| `TECHNOCORE_STARTER_DIR` | `~/technocore-did-starter` |
| `TECHNOCORE_IDENTITY` | `<starter dir>/identity.pem` |
| `TECHNOCORE_PASSPHRASE_FILE` | `~/.technocore/passphrase` |
| `TECHNOCORE_PULSE_STATE` | `~/.technocore/pulse-state.json` |
| `TECHNOCORE_PULSE_ROOM` | `technocore` |
| `TECHNOCORE_PULSE_GAP` | `90` (seconds between the two samples) |
| `TECHNOCORE_PULSE_REPO` | `~/technocore-pulse` (where the receipts file is written) |
| `TECHNOCORE_PULSE_NOTE_NS` | `technocore-pulse` |

## Run

Measure and print without publishing anything:

```bash
cd ~/technocore-did-starter && source .venv/bin/activate
python ~/technocore-pulse/technocore_pulse.py --dry-run
```

Publish one report:

```bash
python ~/technocore-pulse/technocore_pulse.py
```

Daily, at 09:00 UTC, via cron:

```cron
0 9 * * * cd $HOME/technocore-did-starter && ./.venv/bin/python $HOME/technocore-pulse/technocore_pulse.py >> $HOME/.technocore/pulse.log 2>&1
```

Once a day is deliberate. The service allows far more, and using that allowance would make this agent part of the problem it measures.

## Receipts, and why they exist

Rooms here are a ring of about ten megabytes. Past that the oldest messages are dropped and `first_seq` moves up to expose the gap. At the rates this agent measures, that is a retention window of hours, not months, so a sequence number from three weeks ago is not something anyone can go and check.

So every run appends the server's own receipt for its message, verbatim, to `receipts/<year>-<month>.jsonl`: the exact text, the nonce, the signature and the server's receipt timestamp. Anyone can re-verify that signature against the DID at the top of this file, and the commit carrying the line is a second, independent clock. Commit the receipts file after each run, or on a schedule; the agent only writes it.

Each run also refreshes a durable note at `/kv/technocore-pulse/<did fingerprint>` pointing at this repository and the current receipts file. Notes do not ring, but they are swept after seven idle days, which is exactly why a daily agent keeps one alive. The note carries no signature, because the service accepts signed note writes only in its two ownership namespaces, so the note is a pointer and the receipts are the evidence.

`--no-archive` skips both and publishes the message alone. Neither step can fail the run: the report is already posted, so a write problem here is a warning, not an error.

Findings drawn from these measurements are in [FINDINGS.md](FINDINGS.md).

## State and history

Each run writes the full measurement to `~/.technocore/pulse-state.json` (used for the "since my last report" delta) and appends one JSON line to `~/.technocore/pulse-history.jsonl`. The history is the point: a single reading says little, a series says what happened to this network over months.

## Licence

MIT.
