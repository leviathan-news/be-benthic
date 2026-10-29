"""Repository-owned, read-only pool scanner executed inside Benthic's sandbox.

No model code is executed. Only validated parameters enter this program; RPC
responses remain untrusted until their chain, pool, range and shapes are checked.
"""
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import re
import time


ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")
HEX = re.compile(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)\Z")
MAX_LOGS = 20_000
# Curve two-coin pools, including the fee/price-scale fields in twocrypto-ng.
EVENTS = {
    "0x143f1f8e861fbdeddd5b46e844b7d3ac7b86a122f36e8c463859ee6811b1f29c": 6,
    "0xb2e76ae99761dc136e598d4a629bb347eccb9532a5f8bbd72e18467c3c34cc98": 4,
    "0x8b3e96f2b889fa771c53c981b40daf005f63f637f1869f707052d15a3dd97140": 4,
}


class RpcFailure(ValueError):
    pass


def quantity(value):
    if not isinstance(value, str) or not HEX.fullmatch(value) or len(value) > 66:
        raise RpcFailure("invalid_rpc_data")
    return int(value, 16)


def address(value):
    if not isinstance(value, str) or not ADDRESS.fullmatch(value):
        raise RpcFailure("invalid_rpc_data")
    return value.lower()


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def units(value, decimals):
    with localcontext() as context:
        context.prec = 100
        return format(Decimal(value).scaleb(-decimals), "f").rstrip("0").rstrip(".") if decimals else str(value)


class Rpc:
    """Finite requests and bytes, fixed endpoints, no redirects or proxy config."""
    def __init__(self, endpoints, *, timeout=95, max_requests=160, max_bytes=8_388_608):
        import requests
        from urllib.parse import urlsplit
        if not endpoints or len(endpoints) > 2:
            raise RpcFailure("invalid_rpc_config")
        for endpoint in endpoints:
            parsed = urlsplit(endpoint)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                    or parsed.password or parsed.port not in (None, 443)
                    or parsed.query or parsed.fragment):
                raise RpcFailure("invalid_rpc_config")
        self.endpoints = tuple(endpoints)
        self.selected_endpoint = None
        self.session = requests.Session()
        self.session.trust_env = False
        self.deadline = time.monotonic() + min(95, max(1, timeout))
        self.max_requests = min(160, max_requests)
        self.max_bytes = min(8_388_608, max_bytes)
        self.requests = self.bytes = 0
        self.digest = hashlib.sha256()

    @property
    def response_hash(self):
        return self.digest.hexdigest()

    def scan_capacity(self):
        # Leave room to recheck the tip and date examples after a partial scan.
        if self.deadline - time.monotonic() < 20:
            raise RpcFailure("rpc_timeout")
        if self.requests + 16 >= self.max_requests or self.bytes + 131_072 >= self.max_bytes:
            raise RpcFailure("rpc_budget_exhausted")

    def call(self, method, params):
        if method not in {"eth_chainId", "eth_getBlockByNumber", "eth_call", "eth_getLogs"}:
            raise RpcFailure("method_not_allowed")
        import requests
        for endpoint in ((self.selected_endpoint,) if self.selected_endpoint else self.endpoints):
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise RpcFailure("rpc_timeout")
            if self.requests >= self.max_requests or self.bytes >= self.max_bytes:
                raise RpcFailure("rpc_budget_exhausted")
            self.requests += 1
            body = {"jsonrpc": "2.0", "id": self.requests, "method": method, "params": params}
            try:
                with self.session.post(endpoint, json=body, timeout=min(10, remaining),
                                       stream=True, allow_redirects=False) as response:
                    if response.status_code != 200:
                        continue
                    raw = bytearray()
                    for chunk in response.iter_content(16_384):
                        self.bytes += len(chunk)
                        if time.monotonic() >= self.deadline:
                            raise RpcFailure("rpc_timeout")
                        if self.bytes > self.max_bytes or len(raw) + len(chunk) > 1_048_576:
                            raise RpcFailure("rpc_budget_exhausted")
                        raw.extend(chunk)
                self.digest.update(json.dumps(body, sort_keys=True).encode())
                self.digest.update(raw)
                value = json.loads(raw)
            except (requests.RequestException, UnicodeError, json.JSONDecodeError):
                continue
            if (not isinstance(value, dict) or value.get("jsonrpc") != "2.0"
                    or type(value.get("id")) is not int or value["id"] != body["id"]):
                raise RpcFailure("invalid_rpc_data")
            if "error" in value:
                continue
            if "result" not in value:
                raise RpcFailure("invalid_rpc_data")
            if method == "eth_chainId":
                # A scan stays on one provider. A mid-scan fallback must not
                # splice records from an independently advancing chain view.
                self.selected_endpoint = endpoint
            return value["result"]
        raise RpcFailure("rpc_unavailable")


def header(rpc, number):
    value = rpc.call("eth_getBlockByNumber", [number if number == "latest" else hex(number), False])
    if not isinstance(value, dict) or not HASH.fullmatch(str(value.get("hash", ""))):
        raise RpcFailure("invalid_rpc_data")
    n, timestamp = quantity(value.get("number")), quantity(value.get("timestamp"))
    if number != "latest" and n != number:
        raise RpcFailure("invalid_rpc_data")
    return n, timestamp, value["hash"].lower()


def lower_bound(rpc, timestamp, high, cache):
    lo, hi = 0, high + 1
    while lo < hi:
        mid = (lo + hi) // 2
        if mid not in cache:
            cache[mid] = header(rpc, mid)
        if cache[mid][1] < timestamp:
            lo = mid + 1
        else:
            hi = mid
    return lo


def call_word(rpc, contract, data, block):
    value = rpc.call("eth_call", [{"to": contract, "data": data}, hex(block)])
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise RpcFailure("unsupported_pool")
    return value[2:].lower()


def token_metadata(rpc, token, block):
    decimals = int(call_word(rpc, token, "0x313ce567", block), 16)
    if decimals > 36:
        raise RpcFailure("unsupported_token")
    # Symbols are optional untrusted labels. Addresses remain the identity.
    symbol = None
    try:
        raw = rpc.call("eth_call", [{"to": token, "data": "0x95d89b41"}, hex(block)])
        data = bytes.fromhex(raw[2:]) if isinstance(raw, str) and len(raw) <= 1026 else b""
        if len(data) == 32:
            label = data.rstrip(b"\0")
        elif len(data) >= 64 and int.from_bytes(data[:32], "big") == 32:
            length = int.from_bytes(data[32:64], "big")
            label = data[64:64 + length] if 0 < length <= 24 and len(data) >= 64 + length else b""
        else:
            label = b""
        decoded = label.decode("ascii")
        if re.fullmatch(r"[A-Za-z0-9._-]{1,24}", decoded):
            symbol = decoded
    except (RpcFailure, ValueError, UnicodeError):
        pass  # An unavailable display label never substitutes for an address.
    return {"address": token, "decimals": decimals, "symbol": symbol}


def decode_log(row, pool, first, last):
    if not isinstance(row, dict) or address(row.get("address")) != pool or row.get("removed") is not False:
        raise RpcFailure("invalid_rpc_data")
    topics = row.get("topics")
    if (not isinstance(topics, list) or len(topics) != 2
            or topics[0] not in EVENTS or not HASH.fullmatch(str(topics[1]))
            or topics[1][2:26] != "0" * 24):
        raise RpcFailure("invalid_rpc_data")
    data = row.get("data")
    words = EVENTS[topics[0]]
    if not isinstance(data, str) or not re.fullmatch(r"0x[0-9a-fA-F]{" + str(words * 64) + "}", data):
        raise RpcFailure("invalid_rpc_data")
    values = [int(data[i:i + 64], 16) for i in range(2, len(data), 64)]
    block, index = quantity(row.get("blockNumber")), quantity(row.get("logIndex"))
    tx, block_hash = row.get("transactionHash"), row.get("blockHash")
    if (not first <= block <= last or not HASH.fullmatch(str(tx))
            or not HASH.fullmatch(str(block_hash)) or values[0] not in (0, 1)
            or values[2] != 1 - values[0] or values[1] <= 0 or values[3] <= 0):
        raise RpcFailure("invalid_rpc_data")
    return {"caller": address("0x" + topics[1][-40:]), "sold_id": values[0],
            "sold": values[1], "bought_id": values[2], "bought": values[3],
            "block": block, "block_hash": block_hash.lower(), "tx": tx.lower(), "index": index}


def scan_pool(request, rpc, *, chunk_size=50_000):
    """Scan newest ranges first; only whole validated ranges enter aggregates."""
    identity = {key: request[key] for key in ("network", "chain_id", "pool", "start", "end")}
    try:
        return _scan_pool(request, rpc, identity, chunk_size)
    except RpcFailure as exc:
        return {"status": "error", "request": identity, "failure": str(exc),
                "response_hash": rpc.response_hash}


def _scan_pool(request, rpc, identity, chunk_size):
    pool = address(request["pool"])
    if quantity(rpc.call("eth_chainId", [])) != request["chain_id"]:
        raise RpcFailure("chain_mismatch")
    head = header(rpc, "latest")
    end_header = header(rpc, max(0, head[0] - 64))
    cache = {end_header[0]: end_header}
    # Freeze the tip. The receipt states the actual covered timestamp, never
    # claims coverage of unconfirmed blocks between that tip and wall time.
    if request["end"] < end_header[1]:
        end = lower_bound(rpc, request["end"] + 1, end_header[0], cache) - 1
        if end < 0:
            raise RpcFailure("invalid_interval")
        end_header = header(rpc, end)
        cache[end] = end_header
    end = end_header[0]
    first = lower_bound(rpc, request["start"], end, cache)
    if first > end:
        raise RpcFailure("no_confirmed_interval")
    coins = []
    for i in range(2):
        word = call_word(rpc, pool, "0xc6610657" + f"{i:064x}", end)
        if word[:24] != "0" * 24:
            raise RpcFailure("unsupported_pool")
        coins.append(address("0x" + word[-40:]))
    if coins[0] == coins[1] or "0x" + "0" * 40 in coins:
        raise RpcFailure("unsupported_pool")
    target = request.get("target_token")
    if target is None:
        matches = set(coins) & {address(v) for v in request["known_tokens"].values()}
        if len(matches) != 1:
            raise RpcFailure("ambiguous_token")
        target = matches.pop()
    target = address(target)
    if target not in coins:
        raise RpcFailure("asset_not_in_pool")
    target_index = coins.index(target)
    token = token_metadata(rpc, target, end)
    quote = token_metadata(rpc, coins[1 - target_index], end)
    rows, seen, cursor, covered, failure = [], set(), end, end + 1, None
    while cursor >= first:
        lo = max(first, cursor - chunk_size + 1)
        try:
            if hasattr(rpc, "scan_capacity"):
                rpc.scan_capacity()
            values = rpc.call("eth_getLogs", [{"address": pool, "fromBlock": hex(lo),
                              "toBlock": hex(cursor), "topics": [list(EVENTS)]}])
            if not isinstance(values, list):
                raise RpcFailure("invalid_rpc_data")
            if len(rows) + len(values) > MAX_LOGS:
                raise RpcFailure("rpc_budget_exhausted")
            decoded = [decode_log(row, pool, lo, cursor) for row in values]
            identities = {(row["tx"], row["index"]) for row in decoded}
            if len(identities) != len(decoded) or seen.intersection(identities):
                raise RpcFailure("invalid_rpc_data")
            seen.update(identities)
            rows.extend(decoded)
            covered = lo
            cursor = lo - 1
        except RpcFailure as exc:
            if str(exc) == "invalid_rpc_data":
                raise  # Corruption is not a usable partial observation.
            failure = str(exc)
            break
    if covered > end:
        raise RpcFailure(failure or "rpc_unavailable")
    if not rows:
        # A coins() interface alone does not establish the pool's event ABI.
        # No recognized events cannot prove that an arbitrary pool had no swaps.
        raise RpcFailure("unsupported_or_empty_pool")
    # Re-read the frozen tip before accepting its history. A changed hash
    # invalidates the result instead of mixing two branches of the chain.
    if header(rpc, end)[2] != end_header[2]:
        raise RpcFailure("chain_reorganized")
    covered_header = cache.get(covered) or header(rpc, covered)
    callers = defaultdict(lambda: {"buys": 0, "sells": 0, "token_bought": 0,
                                   "token_sold": 0, "quote_spent": 0, "quote_received": 0})
    examples = defaultdict(list)
    for row in rows:
        stat = callers[row["caller"]]
        if row["bought_id"] == target_index:
            stat["buys"] += 1
            stat["token_bought"] += row["bought"]
            stat["quote_spent"] += row["sold"]
            examples[row["caller"]].append(row)
        else:
            stat["sells"] += 1
            stat["token_sold"] += row["sold"]
            stat["quote_received"] += row["bought"]
    def render(stat):
        return {k: value if k in {"buys", "sells"} else units(value, token["decimals"]
                if k.startswith("token_") else quote["decimals"]) for k, value in stat.items()}
    participants = []
    buyers = sorted(((a, s) for a, s in callers.items() if s["buys"]),
                    key=lambda item: (-item[1]["token_bought"], item[0]))
    for caller, stat in buyers[:5]:
        item = {"caller": caller, **render(stat),
                "token_net": units(stat["token_bought"] - stat["token_sold"], token["decimals"])}
        sample = []
        for row in sorted(examples[caller], key=lambda r: (-r["bought"], r["block"], r["index"]))[:2]:
            block = cache.get(row["block"]) or header(rpc, row["block"])
            cache[row["block"]] = block
            if block[2] != row["block_hash"] or not request["start"] <= block[1] <= end_header[1]:
                raise RpcFailure("invalid_rpc_data")
            sample.append({"tx": row["tx"], "utc": utc(block[1]),
                           "token_bought": units(row["bought"], token["decimals"]),
                           "quote_spent": units(row["sold"], quote["decimals"])})
        item["examples"] = sample
        participants.append(item)
    totals = {k: sum(s[k] for s in callers.values()) for k in
              ("buys", "sells", "token_bought", "token_sold", "quote_spent", "quote_received")}
    result = {"status": "partial" if failure else "ok", "request": identity,
              "token": token, "quote_token": quote, "totals": render(totals),
              "buyer_callers": len(buyers), "participants": participants,
              "participant_semantics": "pool event caller; not proven ultimate beneficiary",
              "participant_selection": "top five by token_bought descending within covered interval",
              "coverage": {"complete": failure is None, "from_block": covered, "to_block": end,
                           "from_utc": utc(covered_header[1]), "through_utc": utc(end_header[1]),
                           "tip_hash": end_header[2], "participant_list_truncated": len(buyers) > 5},
              "failure": failure, "response_hash": rpc.response_hash}
    if len(json.dumps(result, separators=(",", ":")).encode()) > 7_500:
        raise RpcFailure("result_too_large")
    return result


def run_request(request):
    rpc = Rpc(request["rpcs"], timeout=request["timeout"])
    return scan_pool(request, rpc)
