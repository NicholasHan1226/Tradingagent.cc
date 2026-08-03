"""Injected-client, receipt-bound fut_index_daily current snapshot reader."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
import hashlib, json, re
from types import MappingProxyType
from typing import Any, Mapping
from shared.data.sharedsignals_v1 import QueryRequest, SharedSignalsV1Client, SharedSignalsV1Error
from shared.data.tradingdatas_pagination import PaginationContractError, collect_query_pages

DATASET_ID="cn.dataset.fut_index_daily"; SCHEMA_MAJOR=1
IDENTITY_FIELDS=("trade_date","ts_code"); QUERY_ORDER=("trade_date:asc","ts_code:asc")
QUERY_FIELDS=("trade_date","ts_code","close","open","high","low","pre_close","change","pct_chg","vol","amount")
RAW_INDEX_FIELDS=QUERY_FIELDS[2:]; PAGE_LIMIT=500; MAX_PAGES=3; MAX_ROWS=1000
_DAY=re.compile(r"^[0-9]{8}$"); _SHA=re.compile(r"^[0-9a-f]{64}$")
class FutIndexDailyCurrentSnapshotConsumerError(ValueError): pass
@dataclass(frozen=True)
class FutIndexDailyRawFact:
 trade_date:str; ts_code:str; receipt_id:str; lineage_sha256:str; raw_values:Mapping[str,Any]
 stable:bool=False; pit_authority:bool=False; session_authority:bool=False; rollover_authority:bool=False; simulation_ready:bool=False; runtime_eligible:bool=False; execution_eligible:bool=False; trading_eligible:bool=False
 def __post_init__(self):
  object.__setattr__(self,"raw_values",MappingProxyType(dict(self.raw_values)))
  if any((self.stable,self.pit_authority,self.session_authority,self.rollover_authority,self.simulation_ready,self.runtime_eligible,self.execution_eligible,self.trading_eligible)): raise FutIndexDailyCurrentSnapshotConsumerError("raw_fact_authority_invalid")
@dataclass(frozen=True)
class FutIndexDailyCurrentSnapshot:
 dataset_id:str; schema_major:int; catalog_version:str; trade_date:str; receipt_id:str; lineage_sha256:str; data_through:datetime; observed_at:datetime; decision_time:datetime; page_count:int; row_count:int; terminal_pagination:bool; replay_verified:bool; semantic_sha256:str; pagination_trace_sha256:str
 as_of:None=None; stable:bool=False; pit_authority:bool=False; session_authority:bool=False; rollover_authority:bool=False; simulation_ready:bool=False; runtime_eligible:bool=False; execution_eligible:bool=False; trading_eligible:bool=False; facts:tuple[FutIndexDailyRawFact,...]=field(default_factory=tuple)
 def __post_init__(self):
  if not (_time(self.data_through,"snapshot_data_through")<=_time(self.observed_at,"snapshot_observed_at")<=_decision(self.decision_time)): raise FutIndexDailyCurrentSnapshotConsumerError("snapshot_time_order_invalid")
  if self.dataset_id!=DATASET_ID or self.schema_major!=1 or not(1<=self.page_count<=MAX_PAGES and 1<=self.row_count<=MAX_ROWS) or not self.terminal_pagination or not self.replay_verified or self.as_of is not None or len(self.facts)<1 or any((self.stable,self.pit_authority,self.session_authority,self.rollover_authority,self.simulation_ready,self.runtime_eligible,self.execution_eligible,self.trading_eligible)): raise FutIndexDailyCurrentSnapshotConsumerError("snapshot_authority_invalid")
def load_fut_index_daily_current_snapshot(*,client:SharedSignalsV1Client,trade_date:str,expected_catalog_version:str,expected_receipt_id:str,expected_lineage_sha256:str,decision_time:datetime)->FutIndexDailyCurrentSnapshot:
 if not isinstance(client,SharedSignalsV1Client): raise TypeError("client must be SharedSignalsV1Client")
 day=_day_text(trade_date); decision=_decision(decision_time); receipt=_text(expected_receipt_id,"expected_receipt_id"); lineage=_sha_text(expected_lineage_sha256,"expected_lineage_sha256")
 try:
  catalog=client.get_catalog()
  if catalog.catalog_version!=_text(expected_catalog_version,"expected_catalog_version"): raise FutIndexDailyCurrentSnapshotConsumerError("catalog_version_mismatch")
  _catalog([r for r in catalog.data if r.get("dataset_id")==DATASET_ID])
  q=QueryRequest(dataset_id=DATASET_ID,schema_major=1,fields=QUERY_FIELDS,filters={"trade_date":{"eq":day}},as_of=None,order=QUERY_ORDER,limit=PAGE_LIMIT)
  first=collect_query_pages(client=client,request=q,identity_fields=IDENTITY_FIELDS,max_pages=MAX_PAGES,max_rows=MAX_ROWS); replay=collect_query_pages(client=client,request=q,identity_fields=IDENTITY_FIELDS,max_pages=MAX_PAGES,max_rows=MAX_ROWS)
 except FutIndexDailyCurrentSnapshotConsumerError: raise
 except PaginationContractError as e: raise FutIndexDailyCurrentSnapshotConsumerError(str(e)) from e
 except SharedSignalsV1Error as e: raise FutIndexDailyCurrentSnapshotConsumerError("tradingdatas_read_failed") from e
 if first.semantic_sha256!=replay.semantic_sha256 or first.semantic_trace_sha256!=replay.semantic_trace_sha256: raise FutIndexDailyCurrentSnapshotConsumerError("replay_drift")
 dt,obs=_metadata(first.envelope.metadata,receipt,lineage,decision); facts=[]
 for row in first.envelope.data:
  if _day_text(row.get("trade_date"))!=day: raise FutIndexDailyCurrentSnapshotConsumerError("trade_date_partition_drift")
  missing=[x for x in RAW_INDEX_FIELDS if x not in row]
  if missing: raise FutIndexDailyCurrentSnapshotConsumerError("raw_field_missing")
  facts.append(FutIndexDailyRawFact(day,_text(row.get("ts_code"),"row.ts_code"),receipt,lineage,{x:row[x] for x in RAW_INDEX_FIELDS}))
 return FutIndexDailyCurrentSnapshot(first.envelope.dataset_id,1,first.envelope.catalog_version,day,receipt,lineage,dt,obs,decision,first.page_count,first.row_count,first.envelope.next_cursor is None,True,first.semantic_sha256,first.pagination_trace_sha256,facts=tuple(facts))
def _catalog(rows):
 if len(rows)!=1: raise FutIndexDailyCurrentSnapshotConsumerError("catalog_dataset_missing_or_duplicate")
 r=rows[0]
 if r.get("schema_major")!=1: raise FutIndexDailyCurrentSnapshotConsumerError("catalog_schema_invalid")
 if tuple(r.get("identity_fields",()))!=IDENTITY_FIELDS: raise FutIndexDailyCurrentSnapshotConsumerError("catalog_identity_invalid")
 if tuple(r.get("default_order",()))!=QUERY_ORDER: raise FutIndexDailyCurrentSnapshotConsumerError("catalog_order_invalid")
 if not set(QUERY_FIELDS).issubset(r.get("default_fields",())): raise FutIndexDailyCurrentSnapshotConsumerError("catalog_raw_fields_missing")
 if "eq" not in r.get("filter_operators",{}).get("trade_date",()): raise FutIndexDailyCurrentSnapshotConsumerError("catalog_trade_date_filter_invalid")
def _metadata(m,receipt,lineage,decision):
 if m.state.strip().lower()!="ready" or m.degraded is not False or m.freshness.get("state")!="fresh" or m.freshness.get("stale") is not False or m.quality.get("state")!="valid" or m.quality.get("valid") is not True: raise FutIndexDailyCurrentSnapshotConsumerError("metadata_contract_invalid")
 if m.receipt_id!=receipt: raise FutIndexDailyCurrentSnapshotConsumerError("receipt_mismatch")
 if not isinstance(m.lineage,Mapping) or m.lineage.get("complete") is not True or m.lineage.get("provider_neutral") is not True: raise FutIndexDailyCurrentSnapshotConsumerError("lineage_incomplete")
 if _hash(m.lineage)!=lineage: raise FutIndexDailyCurrentSnapshotConsumerError("lineage_mismatch")
 dt=_time(m.data_through,"metadata_data_through"); obs=_time(m.observed_at,"metadata_observed_at")
 if not dt<=obs<=decision: raise FutIndexDailyCurrentSnapshotConsumerError("metadata_time_order_invalid")
 return dt,obs
def _text(v,n):
 if not isinstance(v,str) or not v.strip() or v!=v.strip(): raise FutIndexDailyCurrentSnapshotConsumerError(f"{n}_invalid")
 return v
def _day_text(v):
 v=_text(v,"trade_date")
 if not _DAY.fullmatch(v): raise FutIndexDailyCurrentSnapshotConsumerError("trade_date_invalid")
 return v
def _sha_text(v,n):
 v=_text(v,n)
 if not _SHA.fullmatch(v): raise FutIndexDailyCurrentSnapshotConsumerError(f"{n}_invalid")
 return v
def _time(v,n):
 if isinstance(v,datetime): x=v
 else:
  if not isinstance(v,str) or not v.strip(): raise FutIndexDailyCurrentSnapshotConsumerError(f"{n}_missing")
  try: x=datetime.fromisoformat(v.strip().replace("Z","+00:00"))
  except ValueError as e: raise FutIndexDailyCurrentSnapshotConsumerError(f"{n}_invalid") from e
 if x.tzinfo is None or x.utcoffset() is None: raise FutIndexDailyCurrentSnapshotConsumerError(f"{n}_timezone_invalid")
 return x
def _decision(v):
 if not isinstance(v,datetime): raise FutIndexDailyCurrentSnapshotConsumerError("decision_time_invalid")
 if v.tzinfo is None or v.utcoffset() is None: raise FutIndexDailyCurrentSnapshotConsumerError("decision_time_timezone_invalid")
 return v
def _hash(v): return hashlib.sha256(json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
