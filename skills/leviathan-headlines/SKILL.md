---
name: leviathan-headlines
description: >
  This skill should be used when the user asks to "write a headline", "craft a headline",
  "create a LN headline", "review a headline", "fix this headline", "check this headline",
  "rewrite in LN style", "headline for this article", "summarize for Leviathan News",
  "submit a headline", "improve this headline", mentions "Leviathan News", "LN headline",
  "LN style", or provides an article/URL/tweet and expects a headline to be generated
  or reviewed following Leviathan News editorial standards.
version: 0.1.0
---

# Leviathan News Headline Crafting & Review

Craft and review headlines following Leviathan News editorial standards. LN headlines are not
typical news headlines — they summarize articles, provide context, demonstrate crypto domain
expertise, and intrigue readers with a seasoned DeFi veteran's voice.

## Core Rules (Non-Negotiable)

1. **Sentence case** — capitalize first word and proper nouns only
2. **No period** at the end of the headline
3. **75-150 characters** — enforced range
4. **Right-branching** — lead with the core (who + what), branch right with context
5. **Active voice** — never passive unless quoting
6. **Omit articles** (a, an, the) wherever natural
7. **No first person** — never use I, we, our
8. **One URL** per headline (multiple URLs trigger QA flag)
9. **Remove @ symbols** for Telegram posting
10. **Never copy headlines verbatim** — always rephrase in LN voice
11. **No announcements of announcements** — wait for the real reveal

See **`references/examples.md`** for GMI/NGMI demonstrations of each rule.

## Capitalization Quick Reference

| Term | Rule |
|------|------|
| Mainnet | Capitalized for Ethereum only |
| mainnet | Lowercase for other chains |
| DeFi | Capital D, capital F |
| ether | Lowercase (the currency); capitalize only at sentence start |
| ETH / $ETH | Always capitalized; use $ prefix for the ticker |
| proof-of-stake | Hyphenated, lowercase |
| onchain/offchain | One word, no hyphen |
| The Merge | Both capitalized |
| smart contract | Lowercase |
| ZK-proof | ZK capitalized, hyphenated |

For complete terminology: **`references/ethereum-terminology.md`**

## Writing Workflow

### Crafting a New Headline

1. **Read the source material** thoroughly — article, tweet, governance proposal, or document
2. **Identify the core** — the who and the what (this becomes the headline lead)
3. **Find the angle** — what makes this newsworthy? Look for:
   - A compelling quote (especially from prominent figures)
   - Internal tension (two elements at odds)
   - Historical significance ("first time since...", "largest ever...")
   - Financial impact (dollar amounts, percentage changes)
4. **Draft the headline** following the structure:
   `[Core: who + what] + [context/angle/significance]`
5. **Apply editorial rules** — sentence case, no period, omit articles, active voice
6. **Validate** — run the validation script at `scripts/validate-headline.sh`
7. **Check character count** — must be 75-150 characters

### Reviewing an Existing Headline

1. **Run validation** — execute `scripts/validate-headline.sh` against the headline
2. **Check formatting** — sentence case, no period, no articles, active voice
3. **Evaluate the core** — is it right-branching? Is the who+what immediately clear?
4. **Assess sourcing** — is the linked source primary? Would a better source exist?
5. **Judge tone** — does it sound like a seasoned crypto veteran? Is it vivid, direct,
   and slightly conversational without being unprofessional?
6. **Verify accuracy** — does the headline faithfully represent the source material?
7. **Provide corrections** with clear GMI/NGMI comparisons

## Tone and Voice

LN's voice reflects a community of smart, erudite, technical crypto veterans with humor.
The reader should feel like a sharp, informed friend is giving them the rundown — not a wire
service, not a PR team, and definitely not a bot. Domain expertise should be evident in word
choice: use precise DeFi terminology naturally, reference historical parallels when relevant,
and demonstrate that the headline writer actually read and understood the source material.

Humor is welcome when the situation calls for it — a wry observation, an ironic juxtaposition,
a well-placed understatement. But never at the expense of accuracy, and never forced. The
default register is authoritative-but-approachable. Think "the smartest person at the DeFi
conference bar" rather than "corporate comms department."

Three techniques to achieve this voice:

- **Vivid wording** — choose powerful, precise words that make headlines irresistible. Prefer
  "plummets" over "decreases," "liquidation cascade" over "position closures," "war chest"
  over "treasury balance." Strong verbs do more work than adjectives.
- **Conversational tone** — write like a knowledgeable human chatting with the reader. This
  is not disrespectful or unprofessional — it is direct, confident, and human. Avoid corporate
  jargon, buzzwords, and hedging language ("reportedly," "is said to," "appears to").
- **Internal tension** — create mystery through contradictions that compel reading. When two
  elements of the headline are at odds ("fees drop below $0.01 per transaction, yet validators
  earn record revenue"), the reader must click to resolve the puzzle.

See **`references/examples.md`** for tone demonstrations and full headline construction walkthroughs.

## Clause Connection

When connecting two clauses in a headline:

```
CORRECT: ...purchased $CRV, and they announced tokens locked for four years
CORRECT: ...purchased $CRV; they announced tokens locked for four years
WRONG:   ...purchased $CRV; and they announced tokens locked for four years
```

For detailed formatting rules, see **`references/style-guide.md`**.

## Sourcing Rules

Always use the primary source. Follow this hierarchy:

1. Official announcement (blog, governance forum, project tweet)
2. Primary document (regulatory filing, court document)
3. Accessible breakdown (credible analyst thread — when primary is too dense)
4. Reputable news outlet (last resort)

Avoid WatcherGuru. Use Archive.is for paywalled content.

The headline must match its linked content. A Binance listing tweet gets a
listing-focused headline, not background context about the token.

For detailed sourcing hierarchy and examples, see **`references/style-guide.md`**.

## Content Scope

LN is DeFi and Ethereum focused. Cover:
- Major DeFi protocol events and governance
- Ethereum upgrades and ecosystem developments
- Significant regulatory actions (major items only)
- Protocol hacks, exploits, and security events
- Credible DeFi opinions that add value

Skip: minor altcoin news, celebrity endorsements, announcements of announcements.

## Additional Resources

### Reference Files

For detailed rules and terminology, consult:
- **`references/style-guide.md`** — Complete LN style guide with all formatting rules,
  sourcing guidelines, and content scope details
- **`references/ethereum-terminology.md`** — Full Ethereum/crypto terminology table,
  ticker conventions, acronym reference
- **`references/examples.md`** — Comprehensive GMI/NGMI examples covering every rule,
  tone demonstrations, full headline construction walkthroughs

### Validation

- **`scripts/validate-headline.sh`** — Automated headline validation checking character
  count, sentence case, trailing period, article usage, and common issues

## Output Format

When crafting headlines, present output as:

```
HEADLINE: [the headline text]
CHARS: [character count]
SOURCE: [primary source URL]
NOTES: [brief explanation of angle chosen, any editorial decisions]
```

When reviewing headlines, present output as:

```
ORIGINAL: [submitted headline]
ISSUES: [list of rule violations found]
REVISED: [corrected headline]
CHARS: [character count]
EXPLANATION: [what changed and why]
```
