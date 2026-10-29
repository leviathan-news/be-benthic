"""Validate model-selected scope before running the fixed on-chain collector."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
NETWORKS = {"eth": "ethereum", "ethereum": "ethereum", "fraxtal": "fraxtal",
            "arbitrum": "arbitrum", "base": "base", "optimism": "optimism", "linea": "linea"}
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
NUMBER = re.compile(r"-?[0-9]{1,90}(?:\.[0-9]{1,36})?\Z")
FAILURES = frozenset({"rpc_unavailable", "rpc_timeout", "rpc_budget_exhausted", "invalid_rpc_data",
                      "chain_mismatch", "unsupported_pool", "unsupported_token", "ambiguous_token",
                      "asset_not_in_pool", "chain_reorganized", "result_too_large", "invalid_interval",
                      "no_confirmed_interval", "sandbox_unavailable", "planner_unavailable",
                      "unsupported_or_empty_pool"})


@dataclass(frozen=True)
class PoolCandidate:
    network: str
    pool: str
    url: str


@dataclass(frozen=True)
class PoolPlan:
    network: str
    pool: str
    target_token: str | None
    start: int
    end: int


def pool_candidates(urls):
    candidates = []
    for value in urls:
        try:
            parsed = urlsplit(value)
            if (parsed.scheme != "https" or parsed.hostname not in {"geckoterminal.com", "www.geckoterminal.com"}
                    or parsed.username or parsed.password or parsed.port not in (None, 443)):
                continue
            match = re.fullmatch(r"/([a-z0-9_-]+)/pools/(0x[0-9a-fA-F]{40})/?", parsed.path)
            if match is None or match[1] not in NETWORKS:
                continue
            item = PoolCandidate(NETWORKS[match[1]], match[2].lower(),
                                 f"https://www.geckoterminal.com/{match[1]}/pools/{match[2].lower()}")
            if not any((x.network, x.pool) == (item.network, item.pool) for x in candidates):
                candidates.append(item)
        except (TypeError, ValueError):
            continue
    return tuple(candidates[:8])


def parse_plan(raw, candidates, *, now, allowed_tokens=()):
    if not isinstance(raw, str) or len(raw) > 2048:
        raise ValueError("invalid on-chain plan")
    value = json.loads(raw)
    if value == {"operation": "none"}:
        return None
    if not isinstance(value, dict) or set(value) != {"operation", "pool_index", "target_token", "start_utc"}:
        raise ValueError("invalid on-chain plan")
    index, token = value["pool_index"], value["target_token"]
    if (value["operation"] != "pool_flows" or type(index) is not int or not 0 <= index < len(candidates)
            or (token is not None and (not isinstance(token, str) or not ADDRESS.fullmatch(token)))):
        raise ValueError("invalid on-chain plan")
    if token is not None and token.lower() not in {v.lower() for v in allowed_tokens}:
        raise ValueError("target asset not witnessed in human task")
    stamp = value["start_utc"]
    if not isinstance(stamp, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", stamp):
        raise ValueError("invalid on-chain interval")
    start = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if not 0 < (now - start).total_seconds() <= 180 * 86400:
        raise ValueError("on-chain interval outside budget")
    candidate = candidates[index]
    return PoolPlan(candidate.network, candidate.pool, token.lower() if token else None,
                    int(start.timestamp()), int(now.timestamp()))


def build_request(plan, *, timeout):
    chains = json.loads((ROOT / "sandbox/chains.json").read_text())
    cfg = chains[plan.network]
    allowed = {line.strip() for line in (ROOT / "sandbox/allowed-hosts.txt").read_text().splitlines()
               if line.strip() and not line.lstrip().startswith("#")}
    if any(urlsplit(url).hostname not in allowed for url in cfg["rpcs"]):
        raise ValueError("RPC is outside sandbox allowlist")
    known = {name: data["address"].lower() for name, data in cfg.get("tokens", {}).items()
             if isinstance(data, dict) and ADDRESS.fullmatch(str(data.get("address", "")))}
    return {**asdict(plan), "chain_id": cfg["chain_id"], "rpcs": cfg["rpcs"],
            "known_tokens": known, "timeout": min(95, max(1, timeout))}


def worker_code(request):
    # The model supplies neither source code nor a command. Serialize data as a
    # Python string literal so quotes/newlines cannot escape the fixed program.
    source = (ROOT / "onchain_worker.py").read_text()
    return source + "\nprint(json.dumps(run_request(json.loads(" + repr(json.dumps(request)) + ")),separators=(',',':')))\n"


def validate_result(raw, request):
    if not isinstance(raw, str) or len(raw.encode()) > 7_500:
        raise ValueError("oversized on-chain result")
    value = json.loads(raw)
    identity = {key: request[key] for key in ("network", "chain_id", "pool", "start", "end")}
    if (not isinstance(value, dict) or value.get("request") != identity
            or value.get("status") not in {"ok", "partial", "error"}
            or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("response_hash", "")))):
        raise ValueError("unbound on-chain result")
    if value["status"] == "error":
        if set(value) != {"status", "request", "failure", "response_hash"} or value["failure"] not in FAILURES:
            raise ValueError("invalid on-chain failure")
        return value
    expected = {"status", "request", "token", "quote_token", "totals", "buyer_callers", "participants",
                "participant_semantics", "participant_selection", "coverage", "failure", "response_hash"}
    if set(value) != expected or value["participant_semantics"] != "pool event caller; not proven ultimate beneficiary":
        raise ValueError("invalid on-chain result")
    if value["participant_selection"] != "top five by token_bought descending within covered interval":
        raise ValueError("invalid participant selection")
    for key in ("token", "quote_token"):
        token = value[key]
        if (not isinstance(token, dict) or set(token) != {"address", "decimals", "symbol"}
                or not ADDRESS.fullmatch(str(token["address"])) or type(token["decimals"]) is not int
                or not 0 <= token["decimals"] <= 36 or (token["symbol"] is not None
                and not re.fullmatch(r"[A-Za-z0-9._-]{1,24}", str(token["symbol"])))):
            raise ValueError("invalid token metadata")
    if request["target_token"] is not None and value["token"]["address"] != request["target_token"]:
        raise ValueError("wrong result asset")
    coverage = value["coverage"]
    if (not isinstance(coverage, dict) or set(coverage) != {"complete", "from_block", "to_block",
            "from_utc", "through_utc", "tip_hash", "participant_list_truncated"}
            or type(coverage["complete"]) is not bool or type(coverage["participant_list_truncated"]) is not bool
            or coverage["complete"] != (value["status"] == "ok")
            or type(coverage["from_block"]) is not int or type(coverage["to_block"]) is not int
            or not 0 <= coverage["from_block"] <= coverage["to_block"]
            or not re.fullmatch(r"0x[0-9a-f]{64}", str(coverage["tip_hash"]))):
        raise ValueError("invalid coverage")
    for key in ("from_utc", "through_utc"):
        datetime.fromisoformat(coverage[key].replace("Z", "+00:00"))
    if (value["status"] == "ok" and value["failure"] is not None) or (
            value["status"] == "partial" and value["failure"] not in FAILURES):
        raise ValueError("invalid partial scan failure")
    if type(value["buyer_callers"]) is not int or not 0 <= value["buyer_callers"] <= 20000:
        raise ValueError("invalid participant count")
    rows = value["participants"]
    if not isinstance(rows, list) or len(rows) != min(5, value["buyer_callers"]):
        raise ValueError("invalid participants")
    stat_keys = {"buys", "sells", "token_bought", "token_sold", "quote_spent", "quote_received"}
    for stat in [value["totals"], *rows]:
        if not isinstance(stat, dict) or set(stat) != (stat_keys if stat is value["totals"] else
                                                     stat_keys | {"caller", "token_net", "examples"}):
            raise ValueError("invalid aggregate")
        for key in stat_keys:
            entry = stat[key]
            if key in {"buys", "sells"}:
                if type(entry) is not int or not 0 <= entry <= 20000:
                    raise ValueError("invalid trade count")
            elif not isinstance(entry, str) or not NUMBER.fullmatch(entry) or entry.startswith("-"):
                raise ValueError("invalid amount")
    for row in rows:
        if (not ADDRESS.fullmatch(str(row["caller"])) or not NUMBER.fullmatch(str(row["token_net"]))
                or not isinstance(row["examples"], list) or len(row["examples"]) > 2):
            raise ValueError("invalid participant")
        for example in row["examples"]:
            if (not isinstance(example, dict) or set(example) != {"tx", "utc", "token_bought", "quote_spent"}
                    or not re.fullmatch(r"0x[0-9a-f]{64}", str(example["tx"]))
                    or any(not NUMBER.fullmatch(str(example[k])) or str(example[k]).startswith("-")
                           for k in ("token_bought", "quote_spent"))):
                raise ValueError("invalid example transaction")
            datetime.fromisoformat(example["utc"].replace("Z", "+00:00"))
    return value


def receipt_text(result):
    return json.dumps(result, ensure_ascii=True, separators=(",", ":"))


def usable_receipt(item, *, url=None):
    """Only runtime-created, hash-bound chain results can satisfy source work."""
    if (item.kind != "runtime_receipt" or not item.source_ref.startswith("runtime:onchain:")
            or item.content_hash != hashlib.sha256(item.text.encode()).hexdigest()
            or item.source_ref != "runtime:onchain:" + item.content_hash[:20]):
        return False
    try:
        value = json.loads(item.text)
        if value.get("status") not in {"ok", "partial"}:
            return False
        if url is not None:
            return any(c.network == value["request"]["network"] and c.pool == value["request"]["pool"]
                       for c in pool_candidates((url,)))
        return True
    except (ValueError, TypeError, KeyError, AttributeError):
        return False
