from __future__ import annotations
import copy, hashlib, json
from datetime import datetime
import pytest
from CNFutures.fut_index_daily_current_snapshot import FutIndexDailyCurrentSnapshotConsumerError, load_fut_index_daily_current_snapshot
from shared.data.sharedsignals_v1 import HTTPResponse, SharedSignalsV1Client, SharedSignalsV1Config
D="20260803"; V="fixture-index"; R="receipt:index"; L={"complete":True,"provider_neutral":True}; T=datetime.fromisoformat("2026-08-03T21:00:00+00:00"); F=("trade_date","ts_code","close","open","high","low","pre_close","change","pct_chg","vol","amount")
def h(x): return hashlib.sha256(json.dumps(x,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
def rows(): return [{"trade_date":D,"ts_code":"NH001.CI","close":1,"open":1,"high":1,"low":1,"pre_close":1,"change":0,"pct_chg":0,"vol":1,"amount":1}]
def cat(): return {"dataset_id":"cn.dataset.fut_index_daily","schema_major":1,"default_fields":list(F),"identity_fields":["trade_date","ts_code"],"default_order":["trade_date:asc","ts_code:asc"],"filter_operators":{"trade_date":["eq"]}}
def meta(**k):
 x={"state":"ready","degraded":False,"reasons":[],"freshness":{"state":"fresh","stale":False},"quality":{"state":"valid","valid":True},"receipt_id":R,"lineage":copy.deepcopy(L),"data_through":"2026-08-03T20:00:00+00:00","observed_at":"2026-08-03T20:01:00+00:00"}; x.update(k); return x
class X:
 def __init__(self,**k): self.r=k.get("rows",rows()); self.c=k.get("catalog",cat()); self.m=k.get("metadata",meta()); self.replay=k.get("replay",False); self.terminal=k.get("terminal",True); self.n=0
 def __call__(self,**kw):
  if kw["method"]=="GET": return HTTPResponse(200,{"api_version":"v1","catalog_version":V,"request_id":"c","data":[self.c]})
  b=kw["json_body"]; assert b["filters"]=={"trade_date":{"eq":D}} and b["fields"]==list(F) and "as_of" not in b
  self.n+=1; z=copy.deepcopy(self.r)
  if self.replay and self.n==2:z[0]["close"]=2
  return HTTPResponse(200,{"api_version":"v1","catalog_version":V,"request_id":"q","dataset_id":"cn.dataset.fut_index_daily","data":z,"next_cursor":None if self.terminal else "next","metadata":self.m})
def load(x): return load_fut_index_daily_current_snapshot(client=SharedSignalsV1Client(SharedSignalsV1Config(base_url="https://x.invalid",expected_catalog_version=V,dataset_ids=frozenset({"cn.dataset.fut_index_daily"}),access_policy_id="x",max_limit=500,cache_ttl_seconds=0),transport=x),trade_date=D,expected_catalog_version=V,expected_receipt_id=R,expected_lineage_sha256=h(L),decision_time=T)
def test_maps_raw_facts_and_time_provenance():
 s=load(X()); assert s.facts[0].raw_values["close"]==1 and s.data_through<=s.observed_at<=s.decision_time and not any((s.stable,s.pit_authority,s.session_authority,s.rollover_authority,s.simulation_ready,s.runtime_eligible,s.execution_eligible,s.trading_eligible))
@pytest.mark.parametrize("mut,reason",[(lambda c:c.update(schema_major=2),"catalog_schema_invalid"),(lambda c:c.update(identity_fields=["ts_code"]),"catalog_identity_invalid"),(lambda c:c.update(default_order=["ts_code:asc"]),"catalog_order_invalid")])
def test_rejects_catalog_drift(mut,reason):
 c=cat();mut(c)
 with pytest.raises(FutIndexDailyCurrentSnapshotConsumerError,match=reason):load(X(catalog=c))
@pytest.mark.parametrize("m,reason",[(meta(state="partial"),"metadata_contract_invalid"),(meta(receipt_id="x"),"receipt_mismatch"),(meta(lineage={"complete":True,"provider_neutral":True,"revision":"other"}),"lineage_mismatch"),(meta(data_through="2026-08-03T20:02:00+00:00",observed_at="2026-08-03T20:01:00+00:00"),"tradingdatas_read_failed"),(meta(data_through="2026-08-03T20:00:00"),"tradingdatas_read_failed")])
def test_rejects_metadata_drift(m,reason):
 with pytest.raises(FutIndexDailyCurrentSnapshotConsumerError,match=reason):load(X(metadata=m))
def test_rejects_replay_and_identity_drift():
 with pytest.raises(FutIndexDailyCurrentSnapshotConsumerError,match="replay_drift"):load(X(replay=True))
 r=rows();r[0].pop("ts_code")
 with pytest.raises(FutIndexDailyCurrentSnapshotConsumerError):load(X(rows=r))
 with pytest.raises(FutIndexDailyCurrentSnapshotConsumerError):load(X(rows=rows()*2))
 r=rows();r[0].pop("close")
 with pytest.raises(FutIndexDailyCurrentSnapshotConsumerError,match="raw_field_missing"):load(X(rows=r))
 with pytest.raises(FutIndexDailyCurrentSnapshotConsumerError):load(X(terminal=False))
 with pytest.raises(FutIndexDailyCurrentSnapshotConsumerError,match="decision_time_timezone_invalid"):load_fut_index_daily_current_snapshot(client=SharedSignalsV1Client(SharedSignalsV1Config(base_url="https://x.invalid",expected_catalog_version=V,dataset_ids=frozenset({"cn.dataset.fut_index_daily"}),access_policy_id="x",max_limit=500,cache_ttl_seconds=0),transport=X()),trade_date=D,expected_catalog_version=V,expected_receipt_id=R,expected_lineage_sha256=h(L),decision_time=datetime(2026,8,3))
def test_rejects_any_authority_lift():
 from dataclasses import replace
 snapshot=load(X())
 with pytest.raises(FutIndexDailyCurrentSnapshotConsumerError,match="snapshot_authority_invalid"):replace(snapshot,stable=True)
