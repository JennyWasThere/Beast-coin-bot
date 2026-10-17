# AGENTS.md

Behavioral guidelines for any agentic coding assistant working in this repo.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

## 5. Write in ASD-STE100 (Simplified Technical English)

**Use this style in code comments, commit messages, and documentation.** It does not apply to normal chat replies.

- Write short sentences. State one fact or one instruction per sentence.
- Use active voice. Name the actor: "The bot forwards the message," not "The message is forwarded."
- Use simple, common words. Avoid jargon where a plain word works.
- Use present tense for facts and imperative mood for instructions.
- Avoid long noun strings. Break up clusters of more than three nouns in a row.
- Avoid "and," "or," and other joins that hide more than one idea. Split into separate sentences instead.
- Keep commit message bodies as a list of short, single-idea sentences, one change per line.

## Project Quick Facts

- `bot.py` is the whole bot: a single-file Discord bot in `discord.py`.
- Settings and watched hashes live in `config.json` (gitignored, created at runtime). `main()` loads them into `config` and `known_hashes`. The first start reads channel IDs from `.env`. See `.env.example` for the full list of variables.
- The bot serves one server, set by `GUILD_ID`.
- Run the bot: `python bot.py`. Run the tests: `python test_bot.py` (plain `assert`-based, no pytest required, though pytest also works if installed).
- Slash commands that change settings require "Manage Server" permission.
