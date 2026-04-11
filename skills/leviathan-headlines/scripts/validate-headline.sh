#!/usr/bin/env bash
# validate-headline.sh — Validates a Leviathan News headline against editorial rules.
#
# Usage: ./validate-headline.sh "Your headline text here"
#
# Checks performed:
#   1. Character count (must be 75-150)
#   2. No trailing period
#   3. Sentence case (flags Title Case violations)
#   4. Article usage (flags leading articles: a, an, the)
#   5. First person detection (flags I, we, our, etc.)
#   6. @ symbol detection (must be removed for Telegram)
#   7. Passive voice indicators (flags common passive constructions)
#   8. Multiple URL detection (only one URL allowed)
#   9. Semicolon+and detection (invalid clause connection)
#  10. Mainnet capitalization check
#
# Exit codes:
#   0 — All checks passed (or passed with warnings)
#   1 — One or more checks failed
#   2 — No headline provided

# Do not use set -e because grep returning no match (exit 1) is expected behavior.
# pipefail is fine since we handle grep exit codes explicitly.
set -uo pipefail

# Color codes for terminal output
RED='\033[0;31m'
YELLOW='\033[0;33m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m' # No color

# Track overall pass/fail state
ERRORS=0
WARNINGS=0

# --- Helper functions ---

# Print a FAIL message and increment error count
fail() {
  echo -e "  ${RED}FAIL${NC}: $1"
  ERRORS=$((ERRORS + 1))
}

# Print a WARN message and increment warning count
warn() {
  echo -e "  ${YELLOW}WARN${NC}: $1"
  WARNINGS=$((WARNINGS + 1))
}

# Print a PASS message
pass() {
  echo -e "  ${GREEN}PASS${NC}: $1"
}

# Strip trailing punctuation from a word (commas, periods, semicolons, colons, etc.)
# This allows proper noun matching to work when words are followed by punctuation.
strip_punctuation() {
  echo "$1" | sed 's/[,.:;!?'"'"'"]$//'
}

# --- Input validation ---

if [ $# -eq 0 ]; then
  echo -e "${RED}Error${NC}: No headline provided."
  echo "Usage: $0 \"Your headline text here\""
  exit 2
fi

HEADLINE="$1"

echo -e "${CYAN}=== Leviathan News Headline Validator ===${NC}"
echo -e "Headline: \"${HEADLINE}\""
echo ""

# --- Check 1: Character count (75-150) ---

CHAR_COUNT=${#HEADLINE}
echo -e "${CYAN}[1/10] Character Count${NC}"
if [ "$CHAR_COUNT" -lt 75 ]; then
  fail "Too short — ${CHAR_COUNT} characters (minimum 75)"
elif [ "$CHAR_COUNT" -gt 150 ]; then
  fail "Too long — ${CHAR_COUNT} characters (maximum 150)"
else
  pass "Character count: ${CHAR_COUNT} (within 75-150 range)"
fi

# --- Check 2: No trailing period ---

echo -e "${CYAN}[2/10] Trailing Period${NC}"
PERIOD_PATTERN='\.$'
if [[ "$HEADLINE" =~ $PERIOD_PATTERN ]]; then
  fail "Headline ends with a period — remove it"
else
  pass "No trailing period"
fi

# --- Check 3: Sentence case detection ---
# Flags words that are capitalized but shouldn't be (not first word, not proper nouns).
# Heuristic — proper nouns will generate false positives that need manual review.

echo -e "${CYAN}[3/10] Sentence Case${NC}"

# Known proper nouns in crypto/news — skip these to reduce false positives
KNOWN_PROPER=(
  "Ethereum" "Bitcoin" "Solana" "Avalanche" "Polygon" "Arbitrum" "Optimism"
  "Mainnet" "Uniswap" "Aave" "Compound" "Lido" "Curve" "MakerDAO" "Maker"
  "Chainlink" "Synthetix" "Yearn" "Sushi" "SushiSwap" "Balancer" "Gnosis"
  "Coinbase" "Binance" "Kraken" "Gemini" "FTX" "OpenSea" "MetaMask"
  "Vitalik" "Buterin" "Bankless" "Bloomberg" "Worldcoin" "Sam" "Altman"
  "January" "February" "March" "April" "May" "June" "July" "August"
  "September" "October" "November" "December" "Monday" "Tuesday" "Wednesday"
  "Thursday" "Friday" "Saturday" "Sunday" "US" "EU" "UK" "SEC" "CFTC"
  "IRS" "Fed" "Federal" "Reserve" "Congress" "Senate" "Treasury"
  "French" "American" "European" "Chinese" "Japanese" "Korean" "German"
  "Telegram" "Twitter" "Discord" "GitHub" "Google" "Apple" "Microsoft"
  "Merge" "Shanghai" "Dencun" "Pectra" "Layer" "Archive" "Leviathan"
  "Starknet" "Base" "Blast" "Linea" "Mantle" "Scroll" "ZkSync"
  "Euler" "Finance" "Bankrupt" "OTC" "Hinman" "Sybil" "Celsius" "Terra" "Luna"
  "Tether" "Circle" "Ripple" "Cardano" "Polkadot" "Cosmos" "Near"
  "EigenLayer" "Eigenlayer" "Pendle" "Ethena" "Jupiter" "Morpho"
  "News" "Exchange" "Network" "Protocol" "Foundation" "Labs" "Capital"
  "Ventures" "Research" "Partners" "Markets" "Digital" "Fund" "DAO"
)

# Split headline into words
read -ra WORDS <<< "$HEADLINE"
TITLE_CASE_VIOLATIONS=()

# Regex patterns stored in variables to avoid bash parsing issues with special chars
ALLCAPS_PATTERN='^[A-Z][A-Z0-9$-]+$'
TICKER_PATTERN='^\$'
UPPERCASE_START_PATTERN='^[A-Z][a-z]'
ENDS_WITH_SEMICOLON_COLON='^.*[;:]$'

for i in "${!WORDS[@]}"; do
  # Skip the first word (always capitalized in sentence case)
  if [ "$i" -eq 0 ]; then
    continue
  fi

  WORD="${WORDS[$i]}"

  # Strip trailing punctuation for comparison purposes
  CLEAN_WORD=$(strip_punctuation "$WORD")

  # Skip all-caps words (acronyms/tickers like ETH, SEC, DAO)
  if [[ "$CLEAN_WORD" =~ $ALLCAPS_PATTERN ]]; then
    continue
  fi

  # Skip words starting with $ (tickers like $ETH)
  if [[ "$CLEAN_WORD" =~ $TICKER_PATTERN ]]; then
    continue
  fi

  # Skip "DeFi" — special mixed-case convention
  if [ "$CLEAN_WORD" = "DeFi" ]; then
    continue
  fi

  # Skip words following a semicolon or colon (new clause start, capitalization OK)
  if [ "$i" -gt 0 ]; then
    PREV_WORD="${WORDS[$((i - 1))]}"
    if [[ "$PREV_WORD" =~ $ENDS_WITH_SEMICOLON_COLON ]]; then
      continue
    fi
  fi

  # Check if the word starts with an uppercase letter followed by lowercase
  if [[ "$CLEAN_WORD" =~ $UPPERCASE_START_PATTERN ]]; then
    IS_PROPER=false
    for PROPER in "${KNOWN_PROPER[@]}"; do
      if [ "$CLEAN_WORD" = "$PROPER" ]; then
        IS_PROPER=true
        break
      fi
    done

    if ! $IS_PROPER; then
      TITLE_CASE_VIOLATIONS+=("$CLEAN_WORD")
    fi
  fi
done

if [ ${#TITLE_CASE_VIOLATIONS[@]} -gt 0 ]; then
  warn "Possible Title Case violations (verify these aren't proper nouns): ${TITLE_CASE_VIOLATIONS[*]}"
else
  pass "Sentence case appears correct"
fi

# --- Check 4: Leading articles ---

echo -e "${CYAN}[4/10] Article Usage${NC}"
LEADING_ARTICLE_PATTERN='^(A|An|The) '
if [[ "$HEADLINE" =~ $LEADING_ARTICLE_PATTERN ]]; then
  fail "Headline starts with an article — omit \"${BASH_REMATCH[1]}\""
else
  pass "No leading article"
fi

# Check for excessive article usage within the headline
ARTICLE_COUNT=$(echo "$HEADLINE" | grep -oi '\ba\b\|\ban\b\|\bthe\b' | wc -l | tr -d ' ')
if [ "$ARTICLE_COUNT" -gt 2 ]; then
  warn "Found ${ARTICLE_COUNT} articles (a/an/the) — consider omitting some for tighter copy"
fi

# --- Check 5: First person detection ---

echo -e "${CYAN}[5/10] First Person${NC}"
FIRST_PERSON_MATCHES=$(echo "$HEADLINE" | grep -oiE '\b(we|our|my|us|ourselves)\b' | tr '\n' ', ' | sed 's/,$//' || true)
if [ -n "$FIRST_PERSON_MATCHES" ]; then
  fail "First person detected: ${FIRST_PERSON_MATCHES}"
else
  # Check for standalone "I" more carefully (avoid matching "I" inside words)
  if echo "$HEADLINE" | grep -qE '(^| )I( |$|,|;)'; then
    fail "First person detected: I"
  else
    pass "No first person"
  fi
fi

# --- Check 6: @ symbol detection ---

echo -e "${CYAN}[6/10] @ Symbol${NC}"
if [[ "$HEADLINE" == *"@"* ]]; then
  fail "Contains @ symbol — remove for Telegram compatibility"
else
  pass "No @ symbols"
fi

# --- Check 7: Passive voice indicators ---
# Heuristic check — detects common "to be + past participle" constructions.

echo -e "${CYAN}[7/10] Passive Voice${NC}"
PASSIVE_MATCHES=$(echo "$HEADLINE" | grep -oiE '(was|were|been|being|is|are) (being )?(announced|approved|blocked|bought|built|called|changed|claimed|closed|completed|confirmed|created|decided|denied|deployed|discovered|distributed|done|expected|filed|found|given|granted|held|identified|introduced|issued|launched|listed|locked|made|moved|offered|opened|passed|paused|placed|posted|proposed|published|purchased|reached|received|released|removed|reported|revealed|reviewed|ruled|seen|sent|set|shown|sold|started|stopped|submitted|taken|told|transferred|unveiled|updated|used|voted)' | head -3 | tr '\n' ', ' | sed 's/,$//' || true)

if [ -n "$PASSIVE_MATCHES" ]; then
  warn "Possible passive voice: \"${PASSIVE_MATCHES}\" — consider active voice"
else
  pass "No obvious passive voice detected"
fi

# --- Check 8: Multiple URL detection ---

echo -e "${CYAN}[8/10] URL Count${NC}"
URL_COUNT=$(echo "$HEADLINE" | grep -oE 'https?://[^ ]+' | wc -l | tr -d ' ')
if [ "$URL_COUNT" -gt 1 ]; then
  fail "Contains ${URL_COUNT} URLs — maximum 1 allowed (will trigger QA flag)"
elif [ "$URL_COUNT" -eq 1 ]; then
  pass "Single URL detected"
else
  pass "No URLs in headline text"
fi

# --- Check 9: Semicolon + and detection ---

echo -e "${CYAN}[9/10] Clause Connection${NC}"
if echo "$HEADLINE" | grep -qE '; and '; then
  fail "Contains \"; and\" — use either a semicolon OR comma+and, not both"
else
  pass "Clause connection is valid"
fi

# --- Check 10: Mainnet capitalization ---

echo -e "${CYAN}[10/10] Mainnet Capitalization${NC}"
MAINNET_ISSUES=0

# Flag "Ethereum mainnet" (should be "Ethereum Mainnet")
if echo "$HEADLINE" | grep -qE "Ethereum's mainnet|Ethereum mainnet"; then
  warn "\"Ethereum mainnet\" found — should be \"Ethereum Mainnet\" (both capitalized)"
  MAINNET_ISSUES=1
fi

# Flag standalone lowercase "mainnet" without a chain possessive (might refer to Ethereum)
if echo "$HEADLINE" | grep -qE '(^| )mainnet( |$)'; then
  if ! echo "$HEADLINE" | grep -qE "'s mainnet"; then
    warn "Lowercase \"mainnet\" detected — capitalize if referring to Ethereum Mainnet"
    MAINNET_ISSUES=1
  fi
fi

# Flag capitalized "Mainnet" with non-Ethereum chain possessive
if echo "$HEADLINE" | grep -qE "(Arbitrum|Optimism|Polygon|Solana|Avalanche|Base|Blast)'s Mainnet"; then
  warn "Capitalized \"Mainnet\" with non-Ethereum chain — should be lowercase \"mainnet\""
  MAINNET_ISSUES=1
fi

if [ "$MAINNET_ISSUES" -eq 0 ]; then
  pass "Mainnet capitalization looks correct"
fi

# --- Summary ---

echo ""
echo -e "${CYAN}=== Summary ===${NC}"
echo -e "Characters: ${CHAR_COUNT}"
echo -e "Errors:     ${ERRORS}"
echo -e "Warnings:   ${WARNINGS}"

if [ "$ERRORS" -gt 0 ]; then
  echo -e "${RED}RESULT: FAILED — ${ERRORS} error(s) must be fixed${NC}"
  exit 1
elif [ "$WARNINGS" -gt 0 ]; then
  echo -e "${YELLOW}RESULT: PASSED with ${WARNINGS} warning(s) — review recommended${NC}"
  exit 0
else
  echo -e "${GREEN}RESULT: PASSED — headline meets all LN editorial standards${NC}"
  exit 0
fi
