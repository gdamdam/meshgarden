# Games and daily draws

Details for the menus, the two DM games (`BOOK`, `WORDLE`) and the two daily draws (`ICHING`,
`HAIKU`). The full command list is in the [README](../README.md).

Puzzles and the haiku follow the local calendar day, so every player sees the same one. `ICHING` is
per sender per day.

## Menus

`MENU` (or `?`) is a two-level menu. Each page is one DM of at most 180 bytes. The top page lists
`SIGN`, `GUESTBOOK`, `UNSIGN` and `INFO`, and points to `MENU STATS`, `MENU GAMES` and `MENU DAILY`.
Any other argument (`MENU xyz`, `MENU 2`) returns the top page. Its last line shows your daily quota
(see [security-and-privacy.md](security-and-privacy.md#quotas)).

> 🌱 MeshGarden menu
> 📝 SIGN <msg>
> 📖 GUESTBOOK
> 🚫 UNSIGN
> 📊 MENU STATS
> 🎲 MENU GAMES
> 🍃 MENU DAILY
> ℹ️ INFO · ⚠️ experimental · quota 1/60 today

A node that has unlocked the LAB tier also sees `🧪 MENU LAB · experimental` before the `INFO` line
(see the Lab section of the [README](../README.md)). Locked nodes never see it.

| Page | Also | Contents |
| ---- | ---- | -------- |
| `MENU STATS` | `? STATS` | `STATS`, `GARDEN`, `WEEK`, `REPEATER`, `ME`, `CHECK`, `PATH`, `SEEN <node>` |
| `MENU GAMES` | `? GAMES` | `BOOK <title>`, `WORDLE`, `CLUE`, `GIVEUP`, `SCORE` |
| `MENU DAILY` | `? DAILY` | `ICHING`, `HAIKU` |

## Daily draws

### ICHING

One hexagram per sender per local day: same visitor, same day, same draw. The optional
`[service] fortune_salt` mixes into the derivation.

> ☯ Hexagram 1
> ䷀ Qián · The Creative
> great strength rising; act with clear purpose
> 🌱 same for you all day

### HAIKU

One random garden or mesh haiku. With `[service] haiku_place = "Potrero"`:

> Packets drift like leaves
> over Potrero at dusk
> signal finds its way 🌱

### Feeds for a display

| Endpoint | Serves |
| -------- | ------ |
| `/api/iching.json` | The observatory's own hexagram. |
| `/api/haiku.json` | Today's haiku. |
| `/api/daily.json` | The draw chosen by `[service] daily_display` (`"haiku"` is the only value today), with a `kind` field. |

## BOOK

Today's emoji book-trivia puzzle, from a fixed table of 27 books (4-6 emoji each). `BOOK <title>` is
a forgiving guess: punctuation, diacritics and a leading article are ignored. The example puzzle
below is illustrative.

> **You:** `BOOK`
> **MeshGarden:** 📚 Guess: 🐋⚓️😤🦵🌊🌊
> → reply BOOK &lt;title&gt;
> 🌱 new puzzle tomorrow
>
> **You:** `BOOK Moby Dick`
> **MeshGarden:** 📚 ✅ Moby-Dick
> Herman Melville
> 🎉 you're #1 to solve it today 🌱

- **Wrong guess:** `📚 ❌ Not it` / `Hint: <hint>` / `→ BOOK <title> to try again 🌱`.
- **Already solved today:** a fixed "already solved" reply, with no new tier and nothing added to
  `book_stats`. A solve counts once per day.
- **Privacy:** the guess text is never stored or logged. Only the fixed reply and a bounded
  correct/wrong outcome exist (see [security-and-privacy.md](security-and-privacy.md)).

A correct guess gets one of three replies, chosen from bounded all-time and daily aggregates
(`book_stats`, with no node ids or guess text):

| Tier | When | Reply |
| ---- | ---- | ----- |
| First | First solve of that book, ever | `📚 🎉🥇 First to crack it!` / `<Title> · <Author>` / `🌱 new puzzle tomorrow` |
| Rare | All-time solve rate under 20%, with at least 5 guesses | `📚 🏆 Rare catch!` / `<Title> · <Author>` / `🌱 few ever get this one` |
| Normal | Otherwise | `📚 ✅ <Title>` / `<Author>` / `🎉 you're #<N> to solve it today 🌱` (N is today's solves of that book) |

`BOOK SCORE` (or `SCORES`) returns mesh-wide totals only, never node ids or per-player data: today
and the rolling 7 days (today plus the 6 previous local days). It is never counted as a guess.

> 📚 Book scores
> Today: 3 solved · 9 guesses
> 7 days: 14 solved · 52 guesses
> 🌱 BOOK to play

Operators: `mgarden books` prints the all-time per-book shown, guesses and solves.

## WORDLE

One daily five-letter word with up to 6 guesses.

> **You:** `WORDLE`
> **MeshGarden:** 🟩 Wordle 2026-09-13
> 5 letters · 6 tries
> → reply WORDLE &lt;guess&gt; 🌱
>
> **You:** `WORDLE crane`
> **MeshGarden:** 🟩 Wordle · try 1/6
> C R A N E
> ⬜ 🟨 ⬜ N ⬜
> → WORDLE &lt;guess&gt; 🌱

In the feedback row, a green square is shown as its own letter (here `N`), and yellow and white stay
as squares, so the row lines up under the guess.

**Rules**

- The word is the same for every player on a given local day.
- You get 6 guesses per day, with 15 to 30 seconds between them, depending on
  `[service] per_node_cooldown_seconds` (the cooldown every command shares).
- No guessed word is ever stored or logged. Only a per-player guess count and solved flag, keyed
  under a one-way hash, and a daily aggregate with no node ids or words.
- Operators: `mgarden wordle` prints daily aggregates (players, solves, average guesses), newest
  first.

**Endings**

| Outcome | Reply |
| ------- | ----- |
| Solved | `🎉 Solved in <n>/6 · <WORD>` / `<N> solved today · avg <G>` / `🌱 new word tomorrow` |
| 6th wrong guess | `❌ Out of guesses` / `It was <WORD>` / `🌱 new word tomorrow` |
| Any further try that day | `🟩 Wordle <day>` / `You're done for today` / `🌱 new word tomorrow` |

`N` and `G` are today's solve count and average guesses-to-solve (from `wordle_daily`).

### CLUE

`WORDLE CLUE` reveals the answer letter at the position equal to the guesses you have used (none
used shows letter 1, two used shows letter 3). It **costs a try**, like a wrong guess.

> **You:** `WORDLE CLUE`
> **MeshGarden:** 🟩 Wordle · clue used try 1/6
> C _ _ _ _
> → WORDLE &lt;guess&gt; 🌱

### GIVEUP

`WORDLE GIVEUP` (also `GIVE UP` or `I GIVE UP`, any case) reveals the answer and ends your day. It
**never counts as a solve**.

> **You:** `WORDLE GIVEUP`
> **MeshGarden:** 🏳️ Gave up · it was CRANE
> CLUE costs a try, GIVEUP ends the day
> 🌱 new word tomorrow

### SCORE

`WORDLE SCORE` (or `SCORES`) returns mesh-wide totals only, for today and the rolling 7 days. It
never touches player state and never counts as a guess.

> 🟩 Wordle scores
> Today: 4 solved of 7 players · avg 3.5
> 7 days: 21 solved of 38 · avg 3.8
> 🌱 WORDLE to play
