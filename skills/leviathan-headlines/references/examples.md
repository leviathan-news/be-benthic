# Leviathan News Headline Examples

## Formatting Examples

### Sentence Case

```
GMI: Uniswap governance proposes fee switch activation after years of debate
NGMI: Uniswap Governance Proposes Fee Switch Activation After Years Of Debate
```

### No Period at End

```
GMI: Arbitrum DAO passes proposal to distribute 75M ARB to gaming projects
NGMI: Arbitrum DAO passes proposal to distribute 75M ARB to gaming projects.
```

### Connecting Clauses

```
GMI: Protocol used treasury funds to purchase a metric fuckton of $CRV, and they
     announced that these tokens have been locked for four years
GMI: Protocol used treasury funds to purchase a metric fuckton of $CRV; they
     announced that the tokens have been locked for four years
NGMI: Protocol used treasury funds to purchase a metric fuckton of $CRV; and they
      announced that these tokens have been locked for four years
```

### Right-Branching (Core First)

```
GMI: French regulators introduce mandatory certification program for social media
     influencers promoting crypto and financial products, aiming to enhance industry
     professionalism
NGMI: French regulators, aiming to enhance industry professionalism and rein in
      social media influencers, introduce mandatory certification program for
      promoting crypto and financial products
```

The core "French regulators introduce mandatory certification program" is preserved
and leads the headline in the GMI version. The NGMI version buries the core after
a qualifying clause.

### Article Omission

```
GMI: OTC sale opportunity offered by Curve founder after purchasing mega-mansion
NGMI: An OTC sale opportunity offered by the Curve founder after purchasing a mega-mansion
```

### Active Voice

```
GMI: Lido DAO votes to sunset staking operations on Polygon
NGMI: Staking operations on Polygon voted to be sunset by Lido DAO
```

### Mainnet Capitalization

```
GMI: Claims are available only on Mainnet
GMI: Arbitrum's mainnet has been paused once again
NGMI: Claims are available only on mainnet (when referring to Ethereum)
NGMI: Arbitrum's Mainnet has been paused once again (when referring to non-Ethereum)
```

### @ Removal for Telegram

```
GMI: Vitalik Buterin proposes new account abstraction roadmap in latest blog post
NGMI: @VitalikButerin proposes new account abstraction roadmap in latest blog post
```

---

## Tone and Voice Examples

### Vivid Wording

Clear, powerful words make headlines irresistible.

```
GOOD: Euler Finance exploiter returns $100M in stolen funds as on-chain sleuths
      close in
WEAK: Euler Finance hacker sends back $100M after being identified
```

### Conversational Tone

Direct, human — not robotic or corporate.

```
GOOD: Curve founder's $100M DeFi position inches toward liquidation as $CRV
      tumbles 15%
WEAK: CRV price decline of 15% brings Curve Finance founder's collateralized
      position closer to liquidation threshold
```

### Internal Tension

Two elements at odds create mystery and compel reading.

```
GOOD: SEC approves spot Bitcoin ETFs after a decade of rejections, marking a
      historic shift in US crypto regulation
GOOD: Bankrupt FTX estate discovers $5.5B in recoverable assets — far more
      than creditors expected
GOOD: Layer 2 fees drop below $0.01 per transaction, yet Ethereum validators
      earn record revenue
```

### Context and Knowledge

Show off domain knowledge. Read the article, find the nugget.

```
WEAK: Vitalik publishes new blog post (just copying the title — tells reader nothing)
BETTER: Vitalik argues biometric proof-of-personhood is "the least dystopian"
        approach to Sybil resistance in new blog post
BEST: Vitalik concedes Worldcoin's biometric approach may be necessary, argues
      proof-of-personhood is "the least dystopian" option for Sybil resistance
```

---

## Sourcing Examples

### Primary Source Priority

```
SITUATION: Bloomberg reports on Binance listing a new token. Binance posted the
           announcement on their blog and tweeted about it.

BEST SOURCE:  Binance's tweet — https://twitter.com/binance/status/...
OK SOURCE:    Binance blog post
WORST SOURCE: Bloomberg article about the Binance listing

HEADLINE (for Binance tweet):
GMI: Binance lists $WLD, eyeball-scanning digital identity token from Worldcoin
     co-founded by Sam Altman
```

### Paywalled Content

```
SITUATION: WSJ has a breaking story behind a paywall.

ACTION: Use Archive.is to obtain a free copy, then link to the archived version.
```

### Complex Primary Sources

```
SITUATION: IRS publishes a revenue ruling as a dense PDF. CryptoTaxGuyETH breaks
           it down in a Twitter thread.

OPTION A: Link to IRS PDF (primary source, but impenetrable for most readers)
OPTION B: Link to CryptoTaxGuyETH's thread (accessible breakdown, references
          original)

PREFERRED: Option B when the primary source is too dense — link the accessible
           breakdown that references the original. The headline should still
           accurately represent the IRS ruling's content.
```

---

## Full Headline Construction Examples

### Example 1: DeFi Governance

```
SOURCE: Aave governance forum proposal to deploy v4 on Avalanche
HEADLINE: Aave governance submits proposal to deploy v4 on Avalanche, marking
          protocol's first expansion beyond Ethereum L2 ecosystem
```

Breakdown:
- Core: "Aave governance submits proposal to deploy v4 on Avalanche"
- Right-branching context: "marking protocol's first expansion..."
- Active voice, sentence case, no period, articles omitted where natural

### Example 2: Regulatory News

```
SOURCE: SEC enforcement action against DeFi protocol
HEADLINE: SEC files enforcement action against DeFi protocol for offering
          unregistered securities, first case targeting fully decentralized
          project
```

Breakdown:
- Core: "SEC files enforcement action against DeFi protocol"
- Context adds significance: "first case targeting fully decentralized project"
- Internal tension: "fully decentralized" vs "enforcement action"

### Example 3: Market/Protocol Event

```
SOURCE: Major liquidation cascade on lending protocol
HEADLINE: $450M in DeFi positions liquidated in 24 hours as ETH drops below
          $1,500; Aave and Compound absorb largest single-day bad debt since
          June 2022
```

Breakdown:
- Vivid numbers lead: "$450M"
- Two clauses connected by semicolon
- Historical context: "since June 2022"
- Specific protocols named for credibility

### Example 4: Using a Quote

```
SOURCE: Blog post from prominent DeFi figure
HEADLINE: Curve founder warns DeFi is "sleepwalking into a liquidity crisis"
          as protocol TVLs quietly shrink despite rising token prices
```

Breakdown:
- Quote adds authority and intrigue
- Internal tension: "shrink despite rising prices"
- Context shows understanding of broader DeFi dynamics

---

## Common Mistakes

### Announcement of Announcement

```
NGMI: Lido teases upcoming announcement about future staking changes
GMI: (Wait for the actual announcement, then cover the real news)
```

### Copying Headlines Verbatim

```
NGMI: "The Future of Proof-of-Personhood" (copied blog title)
GMI: Vitalik argues biometric proof-of-personhood is "the least dystopian"
     approach to solving Sybil resistance
```

### Missing Context

```
NGMI: New blog post about Worldcoin and identity (no context, no names, no stakes)
GMI: Vitalik concedes Worldcoin's biometric approach may be necessary, calls
     proof-of-personhood "least dystopian" path for identity verification
```

### Wrong Voice

```
NGMI: We believe this governance proposal will pass (first person)
GMI: Governance proposal expected to pass with 98% support after temperature check
```
