"""Historical swap collection must preserve scope, units and attribution."""
import json
from datetime import datetime, timezone

import pytest

POOL = "0x" + "1" * 40
TOKEN = "0x" + "2" * 40
QUOTE = "0x" + "3" * 40
CALLER = "0x" + "4" * 40
TOPIC = "0x143f1f8e861fbdeddd5b46e844b7d3ac7b86a122f36e8c463859ee6811b1f29c"
START = 1785542400  # 2026-08-01 UTC, independent fixture anchor.


def log(block, *, bought=1, caller=CALLER, index=0):
    values = [1 - bought, 1000000 if bought else 3000000000000000000,
              bought, 2000000000000000000 if bought else 4000000, 0, 0]
    return {"address": POOL, "topics": [TOPIC, "0x" + "0" * 24 + caller[2:]],
            "data": "0x" + "".join(f"{v:064x}" for v in values),
            "blockNumber": hex(block), "transactionHash": "0x" + f"{block:064x}",
            "blockHash": "0x" + f"{block + 1000:064x}", "logIndex": hex(index),
            "removed": False}


class RpcFixture:
    """The transport is external; decoding, scanning and aggregation stay real."""
    def __init__(self, entries=(), fail_range=None):
        self.entries = entries
        self.fail_range = fail_range
        self.calls = []
        self.response_hash = "a" * 64

    def call(self, method, params):
        from onchain_worker import RpcFailure
        self.calls.append((method, params))
        if method == "eth_chainId":
            return "0xfc"
        if method == "eth_getBlockByNumber":
            number = 300 if params[0] == "latest" else int(params[0], 16)
            return {"number": hex(number), "timestamp": hex(START + number * 2),
                    "hash": "0x" + f"{number + 1000:064x}"}
        if method == "eth_call":
            call = params[0]
            if call["data"].startswith("0xc6610657"):
                coin = QUOTE if int(call["data"][10:], 16) == 0 else TOKEN
                return "0x" + "0" * 24 + coin[2:]
            if call["data"] == "0x313ce567":
                return "0x" + f"{18 if call['to'] == TOKEN else 6:064x}"
            if call["data"] == "0x95d89b41":
                return "0x" + (b"HARBOR" if call["to"] == TOKEN else b"USD").hex().ljust(64, "0")
        if method == "eth_getLogs":
            lo, hi = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            if self.fail_range is not None and lo <= self.fail_range <= hi:
                raise RpcFailure("rpc_unavailable")
            return [x for x in self.entries if lo <= int(x["blockNumber"], 16) <= hi]
        raise AssertionError((method, params))


def request():
    return {"network": "fraxtal", "chain_id": 252, "pool": POOL,
            "target_token": TOKEN, "start": START + 100, "end": START + 600,
            "known_tokens": {"HARBOR": TOKEN}}


def test_historical_pool_scan_aggregates_buys_and_sells_in_token_units():
    from onchain_worker import scan_pool
    rpc = RpcFixture([log(100), log(101, bought=0), log(200)])
    result = scan_pool(request(), rpc, chunk_size=50)
    assert result["status"] == "ok"
    assert result["coverage"]["complete"] is True
    assert result["totals"] == {"buys": 2, "sells": 1, "token_bought": "4",
                                "token_sold": "3", "quote_spent": "2", "quote_received": "4"}
    assert result["participants"][0]["caller"] == CALLER
    assert result["participants"][0]["token_net"] == "1"
    assert result["participant_semantics"] == "pool event caller; not proven ultimate beneficiary"
    assert result["coverage"]["from_block"] == 50
    assert result["coverage"]["to_block"] == 236
    assert result["token"]["address"] == TOKEN


def test_partial_scan_keeps_only_complete_contiguous_recent_ranges():
    from onchain_worker import scan_pool
    result = scan_pool(request(), RpcFixture([log(100), log(200)], fail_range=120), chunk_size=50)
    assert result["status"] == "partial"
    assert result["coverage"]["complete"] is False
    assert result["coverage"]["from_block"] == 137
    assert result["totals"]["buys"] == 1
    assert result["failure"] == "rpc_unavailable"


def test_top_five_selection_preserves_complete_totals_and_explicit_ranking_scope():
    from onchain_worker import scan_pool
    from onchain_evidence import validate_result
    entries = [log(100 + i, caller="0x" + f"{i:040x}", index=j)
               for i in range(1, 8) for j in range(i)]
    result = scan_pool(request(), RpcFixture(entries))
    assert result["coverage"]["complete"] is True
    assert result["coverage"]["participant_list_truncated"] is True
    assert result["buyer_callers"] == 7
    assert result["totals"]["buys"] == 28
    assert [p["caller"] for p in result["participants"]] == ["0x" + f"{i:040x}" for i in (7, 6, 5, 4, 3)]
    assert result["participant_selection"] == "top five by token_bought descending within covered interval"
    assert validate_result(json.dumps(result), request()) == result
    result["participant_selection"] = "arbitrary sample"
    with pytest.raises(ValueError):
        validate_result(json.dumps(result), request())


@pytest.mark.parametrize("change", [
    {"address": "0x" + "9" * 40}, {"removed": True},
    {"topics": [TOPIC]}, {"data": "0x00"}, {"blockNumber": "0x999999"},
])
def test_unbound_or_malformed_events_never_become_observed_trades(change):
    from onchain_worker import decode_log, RpcFailure
    row = dict(log(100), **change)
    with pytest.raises(RpcFailure):
        decode_log(row, POOL, 50, 150)


def test_duplicate_logs_reject_the_whole_range_before_counting():
    from onchain_worker import scan_pool
    result = scan_pool(request(), RpcFixture([log(200), log(200)]), chunk_size=50)
    assert result["status"] == "error"
    assert "totals" not in result
    assert result["failure"] == "invalid_rpc_data"


def test_row_budget_preserves_validated_recent_ranges(monkeypatch):
    import onchain_worker
    monkeypatch.setattr(onchain_worker, 'MAX_LOGS', 2, raising=False)
    rows = [log(200), log(190), log(150)]
    result = onchain_worker.scan_pool(request(), RpcFixture(rows), chunk_size=50)
    assert result['status'] == 'partial'
    assert result['coverage']['from_block'] == 187
    assert result['totals']['buys'] == 2
    assert result['failure'] == 'rpc_budget_exhausted'


def test_wrong_chain_and_nonmember_asset_fail_without_trade_scan():
    from onchain_worker import scan_pool
    for update in ({"chain_id": 1}, {"target_token": "0x" + "9" * 40}):
        rpc = RpcFixture()
        result = scan_pool(dict(request(), **update), rpc)
        assert result["status"] == "error"
        assert not any(method == "eth_getLogs" for method, _ in rpc.calls)


def test_planner_cannot_invent_pool_rpc_or_extend_time_budget():
    from onchain_evidence import parse_plan, PoolCandidate
    candidate = PoolCandidate("fraxtal", POOL, "https://www.geckoterminal.com/fraxtal/pools/" + POOL)
    raw = {"operation": "pool_flows", "pool_index": 0, "target_token": TOKEN,
           "start_utc": "2026-08-01T00:00:00Z"}
    now = datetime(2026, 9, 22, tzinfo=timezone.utc)
    assert parse_plan(json.dumps(raw), (candidate,), now=now, allowed_tokens=(TOKEN,)).pool == POOL
    for change in ({"pool_index": 1}, {"pool_index": True}, {"rpc": "http://127.0.0.1"},
                   {"start_utc": "2020-01-01T00:00:00Z"}, {"start_utc": "2027-01-01T00:00:00Z"}):
        with pytest.raises(ValueError):
            parse_plan(json.dumps(dict(raw, **change)), (candidate,), now=now, allowed_tokens=(TOKEN,))
    with pytest.raises(ValueError, match='not witnessed'):
        parse_plan(json.dumps(raw), (candidate,), now=now)


def test_empty_unrecognized_event_abi_does_not_claim_zero_buys():
    from onchain_worker import scan_pool
    value = scan_pool(request(), RpcFixture())
    assert value['status'] == 'error'
    assert value['failure'] == 'unsupported_or_empty_pool'
    assert 'totals' not in value


def test_reorganized_tip_invalidates_already_collected_history():
    from onchain_worker import scan_pool
    class ReorganizedRpc(RpcFixture):
        def call(self, method, params):
            value = super().call(method, params)
            if method == 'eth_getBlockByNumber' and params[0] == hex(236):
                if sum(m == method and p[0] == hex(236) for m, p in self.calls) > 1:
                    return dict(value, hash='0x' + 'f' * 64)
            return value
    value = scan_pool(request(), ReorganizedRpc([log(100)]))
    assert value['status'] == 'error'
    assert value['failure'] == 'chain_reorganized'
    assert 'totals' not in value


@pytest.mark.parametrize('field,value', [
    ('request', {}), ('totals', {'buys': 0}), ('participants', []),
    ('response_hash', 'unverified'), ('coverage', {'complete': True}),
])
def test_result_seam_rejects_wrong_scope_or_damaged_aggregates(field, value):
    from onchain_worker import scan_pool
    from onchain_evidence import validate_result
    result = scan_pool(request(), RpcFixture([log(100)]))
    result[field] = value
    with pytest.raises(ValueError):
        validate_result(json.dumps(result), request())


def test_pool_candidate_rejects_lookalike_hosts_and_unsupported_networks():
    from onchain_evidence import pool_candidates
    urls = ["https://www.geckoterminal.com/fraxtal/pools/" + POOL,
            "https://geckoterminal.com.evil.test/fraxtal/pools/" + POOL,
            "https://www.geckoterminal.com/unknown/pools/" + POOL]
    assert [(x.network, x.pool) for x in pool_candidates(urls)] == [("fraxtal", POOL)]


class HttpResponse:
    def __init__(self, value, status=200):
        self.status_code = status
        self.raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return None
    def iter_content(self, size):
        for i in range(0, len(self.raw), size):
            yield self.raw[i:i + size]


def test_rpc_pins_provider_after_chain_validation_and_never_follows_redirects(monkeypatch):
    import requests
    from onchain_worker import Rpc, RpcFailure
    calls = []
    class Session:
        def post(self, endpoint, **kwargs):
            calls.append((endpoint, kwargs))
            if len(calls) == 1:
                return HttpResponse({'jsonrpc': '2.0', 'id': 1, 'result': '0xfc'})
            return HttpResponse(b'', status=302)
    monkeypatch.setattr(requests, 'Session', Session)
    rpc = Rpc(['https://rpc.frax.com', 'https://fraxtal-rpc.publicnode.com'])
    assert rpc.call('eth_chainId', []) == '0xfc'
    with pytest.raises(RpcFailure, match='rpc_unavailable'):
        rpc.call('eth_getLogs', [{}])
    assert [call[0] for call in calls] == ['https://rpc.frax.com', 'https://rpc.frax.com']
    assert all(call[1]['allow_redirects'] is False for call in calls)
    assert rpc.session.trust_env is False


@pytest.mark.parametrize('kind', ['id', 'bytes', 'method', 'requests'])
def test_rpc_rejects_mismatched_responses_and_enforces_finite_limits(monkeypatch, kind):
    import requests
    from onchain_worker import Rpc, RpcFailure
    calls = []
    class Session:
        def post(self, endpoint, **kwargs):
            calls.append(endpoint)
            if kind == 'bytes':
                return HttpResponse(b'x' * 65536)
            return HttpResponse({'jsonrpc': '2.0', 'id': True if kind == 'id' else kwargs['json']['id'], 'result': '0xfc'})
    monkeypatch.setattr(requests, 'Session', Session)
    rpc = Rpc(['https://rpc.frax.com'], max_bytes=32768, max_requests=1)
    if kind == 'requests':
        assert rpc.call('eth_chainId', []) == '0xfc'
    with pytest.raises(RpcFailure):
        rpc.call('eth_sendRawTransaction' if kind == 'method' else 'eth_chainId', [])
    assert len(calls) == (0 if kind == 'method' else 1)
