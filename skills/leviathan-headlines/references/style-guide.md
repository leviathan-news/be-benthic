# Leviathan News Complete Style Guide

## Grammar and Style Foundation

Follow the NYT Manual of Style and Usage as the baseline for all spelling, punctuation,
and grammar decisions. Supplement with the guides listed below when NYT is silent on a topic.

### Supplementary Style References

- Ethereum.org Style Guide (for Ethereum-specific terminology)
- Ethereum Guide for Content Standardization (for consistent crypto terminology)
- Google Developer Documentation Style Guide (for technical writing patterns)

---

## Headline Formatting Rules

### Sentence Case

Capitalize only the first word and proper nouns. Never use Title Case.
Use https://titlecaseconverter.com/sentence-case/ to verify when uncertain.
See `examples.md` for GMI/NGMI sentence case demonstrations.

### No Terminal Punctuation

Never end a headline with a period. Other punctuation (question marks, exclamation points)
should be used sparingly and only when editorially justified.
See `examples.md` for terminal punctuation examples.

### Character Count

Target between 75 and 150 characters. This range ensures headlines are:
- Long enough to provide context and intrigue
- Short enough to display cleanly across platforms (Telegram, Twitter, website)

### Connecting Multiple Clauses

When a headline contains two sentences, connect them with either:

1. A semicolon
2. A comma followed by "and"

Never combine a semicolon with "and." See `examples.md` for clause connection examples.

### Right-Branching Structure

Every headline has a "core" — the who and what. Place the core at the beginning
of the headline, then branch right with additional context and details.

The core (who + what) leads. Supporting details follow. Never bury the core in the
middle or end of a headline. See `examples.md` for right-branching demonstrations.

### Omit Articles

Drop "a," "an," and "the" to tighten headlines and boost impact.
See `examples.md` for article omission demonstrations.

### Active Voice

Prioritize active voice over passive voice for clarity and engagement.
See `examples.md` for active vs. passive voice demonstrations.

### No First Person

First person ("I," "we," "our") must not appear in the feed. Headlines are objective
third-party observations.

### Emoji Usage

Use emojis sparingly. They should enhance, not replace, meaning. Never rely on an emoji
to convey critical information.

---

## URL and Mention Rules

### One URL Per Headline

Only one URL per headline submission. Multiple URLs trigger a QA flag.

### Remove @ Symbols for Telegram

Strip the "@" symbol when posting to Telegram. Including @ causes the headline tweet
to appear in the mentioned person's feed, creating unwanted noise.
See `examples.md` for @ removal demonstrations.

---

## Capitalization Rules

### "Mainnet" Capitalization

- **Ethereum Mainnet**: Capitalize "Mainnet" when used as a synonym for the Ethereum chain
  ```
  GMI: Claims are available only on Mainnet
  ```
- **Other chain mainnets**: Lowercase
  ```
  GMI: Arbitrum's mainnet has been paused once again
  ```
- **"Ethereum Mainnet"**: Both words capitalized when used together

### Standard Crypto Terminology

- Token tickers always capitalized with $ prefix: $ETH, $CRV, $ARB
- Protocol names capitalized: Uniswap, Aave, Compound, MakerDAO
- "DeFi" — capital D, capital F, lowercase e and i
- "Ethereum" always capitalized
- "ether" lowercase unless beginning a sentence; "ETH" always caps
- "proof-of-work" / "proof-of-stake" — hyphenated, lowercase unless sentence-start
- "smart contract" — lowercase unless sentence-start
- "The Merge" — both capitalized as a proper noun
- "zero-knowledge" — hyphenated, lowercase unless sentence-start
- "ZK-proof" / "ZK-rollup" — "ZK" capitalized, hyphenated
- "onchain" / "offchain" — single words, no hyphens, no spaces

---

## Rephrasing

Never copy headlines verbatim from source material. Read the article, understand it,
and rephrase the core information in original language that reflects the LN voice.

There are rare cases where verbatim copying is appropriate (direct quotes from major
figures), but default to original phrasing.

---

## Lengthy Headlines

Headlines for Leviathan News can be longer than traditional news headlines because
they serve a dual purpose:
1. Summarize the article
2. Provide context and clever interpretation

### Two Guiding Factors

1. **The Core**: Every headline has a core that gives visitors a quick sense of who
   and what the headline is about. Identify the core before writing.
2. **Right-Branching**: Begin with the core, add detail after. Never interrupt the
   core with parenthetical or qualifying clauses.

---

## Sourcing Rules

### Always Use the Primary Source

If Cointelegraph or The Block reports on something, find and link to the original source:
- Official blog posts
- Governance proposals
- Original tweets/announcements
- Regulatory documents

### Source Hierarchy

1. Official announcement (blog, governance forum, tweet from the project)
2. Primary document (regulatory filing, court document, IRS publication)
3. Accessible breakdown (thread by a credible analyst, if primary is too dense)
4. Reputable news outlet (as last resort)

### Sources to Avoid

- WatcherGuru (complaints received about reliability)
- Secondary aggregators when primary source is available

### Paywalled Content

Use Archive.is to obtain free copies of paywalled articles (Bloomberg, WSJ, NYT).

### Source vs. Headline Alignment

The headline must match the linked content. If linking to a Binance listing tweet,
the headline should lead with the listing, not background context about the token.

When the primary source is too complex for the audience (e.g., raw IRS PDFs), consider
linking to a credible breakdown thread that also references the original.
See `examples.md` for sourcing alignment demonstrations.

---

## Content Scope

### Topic Focus

Leviathan News is DeFi and Ethereum focused. Non-DeFi/ETH topics should be significant
enough to warrant coverage:

- **Always cover**: Major DeFi protocol events, Ethereum upgrades, significant governance
- **Cover selectively**: Regulatory/Fed news (major items only), cross-chain events
  affecting DeFi
- **Generally skip**: Minor altcoin news, celebrity crypto endorsements, trivial exchange
  listings

### Newsworthiness Criteria

- DeFi opinions and analyses from credible industry figures
- Governance proposals and votes
- Risk assessments and evaluations
- Significant regulatory developments
- Protocol hacks, exploits, and security events
- Major funding rounds or treasury actions

### Announcements

Never post announcements of announcements. Wait for the actual reveal/details before
covering.

### Opinion vs. Reporting

Sharing opinions from credible DeFi figures is acceptable and differentiates LN from
standard crypto media. If posting an opinion, the headline should make clear it
represents someone's view, not established fact.

---

## QA and Submission

### Bot Usage

All bot functions (submit, kill, resubmit, edit) are fair game for use and open to
discussion. The sole exception is the "approve" button — pressing approve signifies
personal accountability for the headline's accuracy and quality.

### Duplicate Checking

Use QA tools to verify a headline isn't duplicating existing coverage. Check the LN
website and search using the same tag/topic before submitting.
