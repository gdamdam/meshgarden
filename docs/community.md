# Community commands

The visitor-facing community features: NEW, NEWS, the Bay Area guide, mail, aliases, radio checks
and usage measurement. All replies use the existing DM-only send boundary, quotas, cooldowns and
byte budget. There are no push notifications and no channel replies.

## Commands

| Command | Purpose |
|---|---|
| `NEW` | New visible guestbook entries, NEWS notices, unlocked board posts, and waiting mail |
| `NEWS [page]` | One operator notice per page; follow `NEXT NEWS n` |
| `LOCAL` / `GUIDE` | List Bay Area MeshCore reference topics |
| `GUIDE RADIO` | Regional radio settings; `GUIDE SOURCE` gives the check date and site |
| `CHECK` | Current received SNR, 24-hour median, best and sample count, and last direct reception when space allows |
| `INBOX` | Alias of bare `MAIL`; collect pending notes |
| `REPLY #id <msg>` | Reply to a note you received and acknowledged before its original expiry |
| `MENU SOCIAL` | Guestbook, mail, INFO and (when unlocked) LAB commands |

## NEW delivery

The first `NEW` summarizes the previous 24 hours. Later requests report what was published after
your last **acknowledged NEW**, not after your last `MENU` or read. It is a summary cursor, not a
claim that you read every item.

- **Committed on ACK.** Pending snapshots are stored in SQLite and advance only when the transport
  reports an ACK. A failed, expired or uncertain reply does not advance them.
- **Never regresses.** Acknowledged snapshots do not move backwards, even if ACKs arrive out of
  order. Numeric entry high-water marks keep posts that arrive in the same second.
- **Mail is current.** Waiting mail is always a live count.
- **Privacy.** Locked boards do not appear or advance their cursor. No node identifiers or private
  message text appear in `NEW`. Inactive cursors expire after 90 days.
- **ACK is not readership.** It means the recipient radio received the reply. A lost ACK can cause
  an update to be shown again.

## NEWS and local references

### Operator notices

After starting the updated daemon once to migrate the database:

```sh
sudo mgarden news add 'Saturday antenna meetup, 10am at the park.' --days 3
mgarden news list
sudo mgarden news hide 1
sudo mgarden news delete 1
mgarden usefulness
```

NEWS is empty until you add a notice.

- Notices last seven days by default; `--days` accepts 1-90.
- Text is limited to 84 UTF-8 bytes, leaving room for the identifier and continuation within the
  default MeshCore budget.
- Expired and hidden notices are not served. Hiding keeps the record.
- These CLI actions transmit nothing, and READ commands never change notices.

`hide` is the ordinary way to retire a notice. `delete` removes the row outright and cannot be
undone. Nothing else in the daemon purges the table (expiry only stops a notice being served), so
`delete` is the only way to remove the text from disk. Both exit 3 when no matching notice exists.

### Bay Area guide

The guide is bundled, cached content checked on 2026-09-20. It never fetches websites in response
to radio requests. It explicitly concerns MeshCore, even when this service runs on Meshtastic.
Settings and device recommendations can change; see [the sources and maintenance
notes](bay-area-guide.md). Operators can update the curated `GUIDE`/`GUIDE_SOURCES` data in
`meshgarden/community.py`.

## Mail

`MAIL`, `INBOX` and `REPLY` are public by default, but receiving mail still requires each
recipient to send `MAIL ON`. Setting `[service] mail_public = false` puts these commands behind the
LAB gate again. Boards always stay in LAB. For what mail stores and who can read it, see
[Mailbox](security-and-privacy.md#mailbox-mail).

### Aliases

An alias is a local, case-insensitive address for the authenticated sender.

| Command | Effect |
|---|---|
| `MAIL ALIAS <name>` | Claim an alias |
| `MAIL ALIAS` | Show yours |
| `MAIL ALIAS OFF` | Release it |
| `MAIL @name <msg>` | Send to an alias |

- Names are 3-16 letters, numbers, `_` or `-`, start with a letter or number, and cannot be
  command words.
- You can change or release an alias once per rolling 24 hours.
- A valid but unknown alias gets the same neutral acknowledgement as an opted-out or full mailbox.
  There is no radio-side directory.
- Operators can inspect and repair mappings with `mgarden mail alias list|set|release`.

### Collecting and replying

Numbered notes show their remaining reply window when space permits.

- After an acknowledged collection, the delivered body is deleted. A small routing record keeps
  only the note ID, sender, recipient and original expiry so `REPLY` still works, and only the
  authenticated recipient can use that ID.
- Partial collections keep only the unread text. Repeated ACKs cannot consume a second chunk or
  double-count a collection.
- Reply routes expire at the note's original deadline (by default seven days after sending), not
  seven days after reading.
- `MAIL OFF` stops new notes, but notes already pending can still be collected.
- Well-formed sends keep the same neutral acknowledgement regardless of opt-in or capacity.

## Radio checks, typo hints and measurement

### CHECK

`CHECK` uses unique local RF observations, and relayed SNR describes the final RF link. Unknown
hops stay unknown, and imported contacts do not prove reception. A direct sample requires a
recorded zero-hop reception. Sparse samples are reported as such. A comparison does not establish
end-to-end delivery or coverage.

### Typo hints

A short, single-word command typo one character away (including adjacent transposition) gets one
fixed suggestion, only when the match is unique.

- Arbitrary conversation and hidden or disabled commands stay silent.
- Hints use the same quota and cooldown as other commands, do not echo your input, and never
  appear on channels.
- `HELP`, `SOS` and `EMERGENCY` keep their existing non-emergency response.

### Usefulness report

`mgarden usefulness` is a local aggregate JSON report covering:

- return visits on multiple UTC days within seven days
- command and outcome counts
- MENU-to-useful and NEW-to-read conversion, and board use
- stored and fully collected mail, unknown requests and errors
- a `visitors` block: messages, senders, valid commands and guestbook signs from everyone except
  LAB-unlocked nodes (the operator's own radios), and when the last one arrived

`mgarden usefulness --reset` starts the visitor count again from now. It records the reset time
and deletes nothing; the 90-day history and the rest of the report are unchanged. Each instance
has its own count, so reset a second mesh with its own `--config`.

NEW-to-read counts guestbook, NEWS and board reads. Inbox collection is measured separately, so
`MAIL ON` and mail sends cannot inflate read conversion. Conversions measure accepted command use,
not human satisfaction.

**Privacy.** No raw text or arguments are stored. Events pseudonymize request and user identities
with a random per-database HMAC key and expire after 90 days. Someone holding the whole database
can still test candidate identities, so this is not anonymity. There are no per-node Prometheus
labels and no external analytics.

Privacy self-inspection and deletion commands are on the [TODO list](TODO.md).
