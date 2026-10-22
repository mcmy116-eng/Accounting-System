# Controlling Claude Code from Telegram

This connects a Telegram bot to Claude Code so you can message Claude from your
phone and get answers back in the same chat, while the work happens on your own
machine against your real files.

It uses Anthropic's official `telegram` channel plugin. There is no code to
write — it's a bot token, three commands, and a pairing step.

> **Where this runs.** Claude Code has to be *running on your machine* with the
> channel flag for messages to arrive. Close the session and the bot goes quiet.
> That's the trade-off for it having your actual files. See
> [Keeping it running](#keeping-it-running).

---

## Is this the remote control you're after?

Two different features get described as "controlling Claude Code from my phone",
and they are not the same thing. Pick by what you actually want to do.

| | **Telegram channel** (this doc) | **Remote Control** (Claude app) |
|---|---|---|
| What you see | Claude's replies only, as chat messages | The full conversation, live |
| Which session | The one you launched with `--channels` | Any session, picked from a list |
| Approve a permission prompt remotely | **No** — the session stalls until you're back | **Yes** |
| Send images and files from your phone | Photos in, yes | Yes, both ways |
| Switch between conversations | No | Yes |
| Type from terminal *and* phone at once | No | Yes, they stay in sync |
| Client | Telegram | Claude app or claude.ai/code |
| Cost | Free | Free, all plans |

**If your goal is to genuinely drive your Claude Code conversations from your
phone, Remote Control is the feature built for it.** It needs no bot, no token,
and no plugin:

```sh
claude --remote-control        # or: claude remote-control
```

Then open the session from the Claude app or claude.ai/code. Claude keeps
running on your machine the whole time, so your files and MCP servers stay
exactly where they are.

**The Telegram channel is for something narrower**: firing a question or an
instruction at a session from a chat app you already have open, and getting the
answer in that same chat. It's a message bridge, not a window into the session.

They coexist. Running both means quick asks go through Telegram and anything
needing a real look goes through the app.

---

## Before you start — have these ready

1. **Claude Code**, signed in with your claude.ai account (`claude` then `/login`).
   API-key-only setups can't use channels.
2. **Bun** — the channel server is a Bun script. Check with `bun --version`; if
   that fails, install it:
   ```sh
   curl -fsSL https://bun.sh/install | bash
   ```
3. **A Telegram account** on your phone.
4. *(Team/Enterprise plans only)* An Owner must switch **Channels** on at
   https://claude.ai/admin-settings/claude-code first. On Pro and Max it's
   already available.

Run `scripts/telegram-preflight.sh` at any point to check where you stand.

---

## Step-by-step

### 1. Create the bot

Open [@BotFather](https://t.me/BotFather) in Telegram and send `/newbot`. It asks
for two things:

| It asks for | What to give it |
|---|---|
| **Name** | The display name in chat headers. Anything, spaces fine. e.g. `Prism Claude` |
| **Username** | A unique handle ending in `bot`. e.g. `prism_claude_bot` |

BotFather replies with a token like `123456789:AAHfiqksKZ8...`. Copy the whole
thing including the leading number and colon. **Treat it like a password** — see
[Security](#security).

### 2. Install the plugin

Start Claude Code (`claude`) in any directory and run:

```
/plugin install telegram@claude-plugins-official
```

Choose the **user scope** when it asks, so the plugin works in every project.

If it says the marketplace isn't found, add it first and retry:

```
/plugin marketplace add anthropics/claude-plugins-official
```

If the summary says `Run /reload-plugins to activate.`, run that.

### 3. Give it the token

```
/telegram:configure 123456789:AAHfiqksKZ8...
```

This writes `TELEGRAM_BOT_TOKEN=...` to `~/.claude/channels/telegram/.env`.

### 4. Restart with the channel flag

The server does not connect without this. Exit Claude Code, then:

```sh
claude --channels plugin:telegram@claude-plugins-official
```

The startup screen should mention that messages from
`plugin:telegram@claude-plugins-official` inject into this session. If a warning
appears instead, it names the problem.

> `--channels` doesn't show up in `claude --help`. That's expected — the feature
> is in research preview. The flag still works.

### 5. Pair your account

With that session running, DM your bot on Telegram. It replies with a
six-character code. Back in Claude Code:

```
/telegram:access pair a4f91c
```

Your next message reaches Claude.

### 6. Close the door

Pairing exists to capture your numeric user ID. Once you're through, switch the
policy so strangers get nothing instead of a pairing code:

```
/telegram:access policy allowlist
```

**Do not skip this.** A Telegram bot username is publicly addressable — anyone
who guesses it can DM it. This one command is the difference between a private
bridge and an open one.

---

## Decide this before you walk away

Claude still asks permission before it acts, and the Telegram plugin has no way
to forward those prompts to your phone. So a session left alone will sit waiting
at a prompt you can't see.

Your options, best first:

| Setting | What happens | Verdict |
|---|---|---|
| `--permission-mode acceptEdits` plus allow rules in `.claude/settings.json` | Claude edits files and runs the commands you've allowed; anything else waits | **Use this.** Predictable, and the blast radius is one you chose |
| Default (Manual) | Every tool call waits for you at the keyboard | Fine while you're at your desk, useless from the sofa |
| `--dangerously-skip-permissions` | Anyone on the allowlist can run anything on this machine by typing into a chat | **Don't**, not on a machine holding your real files |

So the command you'll actually use day-to-day:

```sh
claude --channels plugin:telegram@claude-plugins-official --permission-mode acceptEdits
```

---

## Keeping it running

There's no setting that turns channels on permanently — you opt in per session,
by design. Add an alias to your `~/.zshrc` or `~/.bashrc`:

```sh
alias claude-tg='claude --channels plugin:telegram@claude-plugins-official --permission-mode acceptEdits'
```

For a genuinely always-on bridge, run that in a persistent terminal
(`tmux new -s claude`) on a machine that stays awake. If you want it running with
no machine of yours involved at all, that's a different architecture — the cloud
bridge — and it needs a relay service and a Stop hook instead of this plugin.

---

## What Claude can do back

| Tool | What it does |
|---|---|
| `reply` | Sends text to the chat. Auto-splits at Telegram's 4096-character limit. Can attach files up to 50 MB |
| `react` | Adds an emoji reaction. **Only Telegram's fixed whitelist** works (👍 👎 ❤ 🔥 👀 …); anything else is silently ignored |
| `edit_message` | Edits a message it sent earlier — this is how you get a "working…" line that turns into the answer |

Inbound photos are downloaded to `~/.claude/channels/telegram/inbox/` so Claude
can read them. Send as a *file* (long-press → Send as File) if you need the
original rather than Telegram's compressed copy.

**There is no message history.** Telegram's Bot API exposes neither history nor
search, so the bot only sees messages as they arrive. Claude will ask you to
paste earlier context rather than look it up.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Bot never replies to your first DM | Session isn't running with `--channels` | Restart with the flag from step 4 |
| Startup warns the plugin isn't on the approved list | Team/Enterprise policy | An Owner enables Channels in admin settings |
| `unknown option --channels` | Claude Code too old | `claude update` |
| Server exits immediately | Bun missing | `curl -fsSL https://bun.sh/install \| bash` |
| Messages arrive but Claude never acts | Waiting on a permission prompt | Restart with `--permission-mode acceptEdits` |
| A stranger got a pairing code | Policy is still `pairing` | `/telegram:access policy allowlist` |
| Reaction emoji does nothing | Not on Telegram's whitelist | Pick one from the whitelist, e.g. 👀 |

Useful state commands:

```
/telegram:access                      # policy, allowlist, pending pairings
/telegram:access allow 412587349      # add a user ID by hand
/telegram:access remove 412587349     # revoke
```

Your own numeric ID comes from [@userinfobot](https://t.me/userinfobot).

---

## Security

- **The bot token is a production secret.** Whoever holds it can talk to an agent
  that can write to this repository. It lives in
  `~/.claude/channels/telegram/.env` — never commit it, never paste it in chat.
  If it leaks, message BotFather and send `/revoke`.
- **Every message is an instruction.** Anyone on the allowlist can effectively
  run commands on the machine hosting the session. Keep the allowlist to
  yourself.
- **Groups are off by default** and stay that way unless you add them
  explicitly. Leave them off unless you have a reason.
- **Channels are a research preview.** The flag syntax and the underlying
  protocol may change. Fine for a personal bridge; worth knowing before anything
  depends on it.

Full access-control reference:
https://github.com/anthropics/claude-plugins-official/tree/main/external_plugins/telegram
