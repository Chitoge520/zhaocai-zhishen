from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from itertools import combinations
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from .audit_schema import normalize_text, stable_identifier

QUOTE_ANALYSIS_SCHEMA_VERSION = "bid-audit-quote-analysis/v1"
QUOTE_FEATURE_SCHEMA_VERSION = "bid-audit-quote-feature/v1"
QUOTE_ALGORITHM_VERSION = "quote-pattern-unsupervised/1.0.0"
TOTAL_SIGNALS = ("control_price_ratio", "median_deviation", "pairwise_price_distance", "fixed_difference", "fixed_ratio", "repeated_price_tail", "staircase_quote", "accompaniment_structure")
ITEM_SIGNALS = ("item_price_correlation", "item_rank_consistency")
SIGNAL_SPECS = {
    "control_price_ratio": ("接近控制价的集中报价", 8), "median_deviation": ("相对项目中位数的极端偏离", 10),
    "pairwise_price_distance": ("投标人对报价距离过近", 15), "fixed_difference": ("多家报价呈近似固定差额", 14),
    "fixed_ratio": ("多家报价呈近似固定比例", 14), "repeated_price_tail": ("多家报价出现相同非常规尾数", 8),
    "staircase_quote": ("多家报价呈规则阶梯", 12), "accompaniment_structure": ("一低多高且高价集中", 15),
    "item_price_correlation": ("分项报价高度相关", 12), "item_rank_consistency": ("分项报价排序高度一致", 10),
}
_UNITS = {"": Decimal("1"), "cny": Decimal("1"), "rmb": Decimal("1"), "yuan": Decimal("1"), "元": Decimal("1"), "人民币": Decimal("1"), "万元": Decimal("10000"), "万": Decimal("10000"), "10k": Decimal("10000"), "千元": Decimal("1000"), "千": Decimal("1000"), "k": Decimal("1000")}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists(): return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _num(value: object) -> Decimal | None:
    text = normalize_text(value).replace(",", "")
    try: result = Decimal(text)
    except (InvalidOperation, ValueError): return None
    return result if text and result.is_finite() and result > 0 else None


def _amount(row: dict[str, Any], field: str = "amount") -> tuple[Decimal | None, str]:
    value = _num(row.get(field))
    if value is None: return None, "not_provided"
    unit = normalize_text(row.get("amount_unit")).lower().replace(" ", "")
    multiplier = _UNITS.get(unit)
    return (value * multiplier, "available") if multiplier is not None else (None, "excluded")


def _scope(row: dict[str, Any]) -> str:
    value = normalize_text(row.get("quote_scope")).lower().replace("_", "-")
    if value in {"item", "line-item", "unit", "分项", "分项报价", "清单"}: return "item"
    if value in {"total", "total-price", "overall", "总价", "总报价"}: return "total"
    return "item" if any((normalize_text(row.get("item_code")), normalize_text(row.get("item_name")), row.get("unit_price"))) else "total"


def _refs(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for row in rows:
        for ref in row.get("source_refs") or []:
            key = (str(ref.get("source_path", "")), str(ref.get("source_sha256", "")), int(ref.get("row_number") or 0))
            if key not in seen: seen.add(key); out.append(ref)
    return out


def _ids(rows: Iterable[dict[str, Any]]) -> list[str]:
    return sorted({normalize_text(row.get("record_id")) for row in rows if normalize_text(row.get("record_id"))})


def _fmt(value: Decimal | float | None) -> str:
    return "" if value is None else format(Decimal(str(value)).quantize(Decimal("0.01")), "f")


def _distance(left: float, right: float) -> float:
    return abs(left - right) / max((abs(left) + abs(right)) / 2, 1)


def _risk_level(score: int) -> str:
    return "high" if score >= 55 else "medium" if score >= 30 else "low" if score else "none"


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) < 3 or len(left) != len(right): return None
    a, b = sum(left) / len(left), sum(right) / len(right)
    numerator = sum((x-a)*(y-b) for x, y in zip(left, right))
    denom = math.sqrt(sum((x-a)**2 for x in left) * sum((y-b)**2 for y in right))
    return numerator / denom if denom else None


def _ranks(values: list[float]) -> list[float]:
    return [float(sorted(values).index(value) + 1) for value in values]


def _signal(bucket: dict[frozenset[str], list[dict[str, Any]]], bidders: Iterable[str], signal_type: str, rows: list[dict[str, Any]], detail: str, formula: str, inputs: dict[str, Any]) -> None:
    pair = frozenset(bidders)
    if len(pair) != 2: return
    label, weight = SIGNAL_SPECS[signal_type]
    bucket[pair].append({"signal_type": signal_type, "label": label, "weight": weight, "detail": detail, "formula": formula, "inputs": inputs, "source_record_ids": _ids(rows), "evidence_refs": _refs(rows)})


def _total_candidates(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    grouped: dict[tuple[str, str], list[tuple[Decimal, dict[str, Any]]]] = defaultdict(list)
    status: dict[str, str] = {}
    for row in rows:
        if row.get("record_type") not in {"quote", "bid"} or _scope(row) != "total": continue
        amount, state = _amount(row)
        bidder = normalize_text(row.get("bidder_id"))
        if amount is None: status[bidder] = "excluded" if state == "excluded" else status.get(bidder, state); continue
        grouped[(bidder, normalize_text(row.get("currency") or "CNY").upper())].append((amount, row))
    totals: dict[str, dict[str, Any]] = {}
    for bidder in {key[0] for key in grouped}:
        choices = []
        for (candidate, currency), candidates in grouped.items():
            if candidate != bidder: continue
            if len({amount for amount, _ in candidates}) != 1:
                status[bidder] = "excluded"; continue
            candidates.sort(key=lambda item: (0 if item[1].get("record_type") == "quote" else 1, normalize_text(item[1].get("record_id"))))
            choices.append((currency, candidates[0][0], [row for _, row in candidates]))
        if len(choices) == 1:
            currency, amount, source_rows = choices[0]
            totals[bidder] = {"currency": currency, "amount": amount, "rows": source_rows}
            status[bidder] = "available"
        else: status.setdefault(bidder, "excluded" if choices else "not_provided")
    return totals, status


def _item_values(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    values = {}
    for row in rows:
        if row.get("record_type") not in {"quote", "bid"} or _scope(row) != "item": continue
        amount, state = _amount(row, "unit_price") if row.get("unit_price") else _amount(row)
        key = normalize_text(row.get("item_code")) or normalize_text(row.get("item_name"))
        if amount is None or state != "available" or not key: continue
        if key in values and values[key]["amount"] != amount: values.pop(key, None)
        elif key not in values: values[key] = {"amount": amount, "row": row}
    return values


def _project_features(project_id: str, bidder_rows: dict[str, list[dict[str, Any]]], names: dict[str, str], controls: list[Decimal]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    bidders = sorted(bidder_rows); rows = [row for source in bidder_rows.values() for row in source]
    totals, total_status = _total_candidates(rows)
    items = {bidder: _item_values(bidder_rows[bidder]) for bidder in bidders}
    control = controls[0] if len(set(controls)) == 1 and controls else None
    control_state = "available" if control is not None else "excluded" if controls else "not_provided"
    signals: dict[frozenset[str], list[dict[str, Any]]] = defaultdict(list)
    currency_groups: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for bidder, entry in totals.items(): currency_groups[entry["currency"]].append((bidder, entry))

    for currency, group in currency_groups.items():
        group.sort(key=lambda item: (float(item[1]["amount"]), item[0])); values = [float(entry["amount"]) for _, entry in group]
        if len(group) >= 2:
            for (left_id, left), (right_id, right) in combinations(group, 2):
                source = left["rows"] + right["rows"]; distance = _distance(float(left["amount"]), float(right["amount"]))
                if distance <= .005: _signal(signals, (left_id, right_id), "pairwise_price_distance", source, "两家总价相对距离不超过 0.5%，需结合其他独立信号复核。", "abs(a-b)/((a+b)/2)", {"a": _fmt(left["amount"]), "b": _fmt(right["amount"]), "relative_distance": round(distance, 6)})
                if control:
                    ratios = (float(left["amount"] / control), float(right["amount"] / control))
                    if .95 <= ratios[0] <= 1 and .95 <= ratios[1] <= 1 and abs(ratios[0]-ratios[1]) <= .003: _signal(signals, (left_id, right_id), "control_price_ratio", source, "两家报价均接近控制价且比例高度接近，属于需交叉验证的弱信号。", "price/control_amount", {"left_ratio": round(ratios[0], 6), "right_ratio": round(ratios[1], 6), "control_amount": _fmt(control)})
        if len(group) >= 4:
            center, mad = median(values), median([abs(value-median(values)) for value in values])
            if mad:
                for bidder, entry in group:
                    z = .6745*(float(entry["amount"])-center)/mad
                    if abs(z) >= 3.5:
                        for other, other_entry in group:
                            if bidder != other: _signal(signals, (bidder, other), "median_deviation", entry["rows"]+other_entry["rows"], "单家总价相对项目中位数出现稳健统计极端偏离，需复核报价构成和资格条件。", "0.6745*(price-median)/MAD", {"price": _fmt(entry["amount"]), "median": _fmt(center), "mad": round(mad, 4), "robust_z": round(z, 4)})
        if len(group) >= 3:
            gaps = [values[i+1]-values[i] for i in range(len(values)-1)]; target = median(gaps)
            gap_match = target > 0 and sum(abs(gap-target)/max(target,1) <= .02 for gap in gaps) >= 2
            ratios = [values[i+1]/values[i] for i in range(len(values)-1) if values[i]]; target_ratio = median(ratios) if ratios else 0
            ratio_match = len(ratios) >= 2 and sum(abs(ratio-target_ratio) <= .003 for ratio in ratios) >= 2
            for (left_id, left), (right_id, right) in combinations(group, 2):
                source = left["rows"]+right["rows"]
                if gap_match:
                    inputs={"median_gap": round(target,4), "gap_count":len(gaps)}
                    _signal(signals,(left_id,right_id),"fixed_difference",source,"不少于三家报价的相邻差额近似一致，需结合报价清单与历史行为核验。","sorted_price[i+1]-sorted_price[i]",inputs)
                    _signal(signals,(left_id,right_id),"staircase_quote",source,"总价排序呈规则阶梯，属于待复核规律性差异。","adjacent_gap≈median_gap",inputs)
                if ratio_match: _signal(signals,(left_id,right_id),"fixed_ratio",source,"不少于三家报价的相邻比例近似一致，需结合其他独立证据复核。","sorted_price[i+1]/sorted_price[i]",{"median_ratio":round(target_ratio,6),"ratio_count":len(ratios)})
            tails: dict[str,list[tuple[str,dict[str,Any]]]] = defaultdict(list)
            for bidder, entry in group:
                tail=f"{int(entry['amount'].quantize(Decimal('1'))) % 10000:04d}"
                if tail not in {"0000","0100","1000"}: tails[tail].append((bidder,entry))
            for tail, matched in tails.items():
                if len(matched) >= 3:
                    for (left_id,left),(right_id,right) in combinations(matched,2): _signal(signals,(left_id,right_id),"repeated_price_tail",left["rows"]+right["rows"],"至少三家报价共享相同的四位非常规尾数；该信号单独不足以说明异常。","int(price) mod 10000",{"tail":tail,"matched_bidder_count":len(matched)})
            low_id, low = group[0]; highs=group[1:]
            if len(highs) >= 2:
                high_values=[float(entry["amount"]) for _,entry in highs]; high_center=median(high_values); spread=max(abs(value-high_center)/max(high_center,1) for value in high_values); low_ratio=float(low["amount"])/high_center
                if low_ratio <= .85 and spread <= .01:
                    for high_id, high in highs: _signal(signals,(low_id,high_id),"accompaniment_structure",low["rows"]+high["rows"],"存在一低多高且高价集中结构，仅作为待复核线索而非结论。","low/median(high) and max_deviation(high)",{"low_to_high_median":round(low_ratio,6),"high_spread":round(spread,6),"high_bidder_count":len(highs)})

    for left_id,right_id in combinations(bidders,2):
        shared=sorted(set(items[left_id]) & set(items[right_id]))
        if len(shared) < 3: continue
        left_values=[float(items[left_id][key]["amount"]) for key in shared]; right_values=[float(items[right_id][key]["amount"]) for key in shared]; source=[items[left_id][key]["row"] for key in shared]+[items[right_id][key]["row"] for key in shared]
        corr=_pearson(left_values,right_values)
        if corr is not None and corr >= .995: _signal(signals,(left_id,right_id),"item_price_correlation",source,"至少三个共同分项的报价相关系数极高，需排除公开清单、统一计价依据等正常原因。","Pearson(item_prices_a,item_prices_b)",{"shared_item_count":len(shared),"correlation":round(corr,6)})
        rank_corr=_pearson(_ranks(left_values),_ranks(right_values))
        if rank_corr is not None and rank_corr >= .995: _signal(signals,(left_id,right_id),"item_rank_consistency",source,"至少三个共同分项的报价排序高度一致，属于需人工复核的弱关联信号。","Pearson(rank(item_prices_a),rank(item_prices_b))",{"shared_item_count":len(shared),"rank_correlation":round(rank_corr,6)})

    project_name=next((normalize_text(row.get("project_name")) for row in rows if normalize_text(row.get("project_name"))),""); features=[]; edges=[]
    for left_id,right_id in combinations(bidders,2):
        left,right=totals.get(left_id),totals.get(right_id); pair=frozenset((left_id,right_id)); pair_signals=signals[pair]; same_currency=bool(left and right and left["currency"]==right["currency"])
        statuses={}
        for signal_type in TOTAL_SIGNALS:
            if signal_type == "control_price_ratio":
                if not left or not right:
                    statuses[signal_type] = "excluded" if "excluded" in {total_status.get(left_id), total_status.get(right_id)} else "not_provided"
                elif not same_currency:
                    statuses[signal_type] = "excluded"
                else:
                    statuses[signal_type] = "no_signal" if control else control_state
            elif not left or not right:
                statuses[signal_type]="excluded" if "excluded" in {total_status.get(left_id),total_status.get(right_id)} else "not_provided"
            else: statuses[signal_type]="no_signal" if same_currency else "excluded"
        shared_count=len(set(items[left_id])&set(items[right_id])); statuses.update({signal_type:"no_signal" if shared_count>=3 else "not_provided" for signal_type in ITEM_SIGNALS})
        for signal in pair_signals: statuses[signal["signal_type"]]="triggered"
        source=[]
        if left: source.extend(left["rows"])
        if right: source.extend(right["rows"])
        for key in set(items[left_id])&set(items[right_id]): source.extend([items[left_id][key]["row"],items[right_id][key]["row"]])
        contributions=[{"signal_type":signal["signal_type"],"label":signal["label"],"score":signal["weight"]} for signal in pair_signals]; score=min(100,sum(item["score"] for item in contributions))
        feature={"schema_version":QUOTE_FEATURE_SCHEMA_VERSION,"algorithm_version":QUOTE_ALGORITHM_VERSION,"feature_id":stable_identifier("quote_feature",f"{project_id}|{left_id}|{right_id}"),"project_id":project_id,"project_name":project_name,"bidder_a_id":left_id,"bidder_a_name":names.get(left_id,left_id),"bidder_b_id":right_id,"bidder_b_name":names.get(right_id,right_id),"currency":left["currency"] if same_currency else "","bidder_a_total_amount":_fmt(left["amount"]) if left else "","bidder_b_total_amount":_fmt(right["amount"]) if right else "","control_amount":_fmt(control),"signal_status":statuses,"signals":pair_signals,"risk_contributions":contributions,"risk_score":score,"risk_level":_risk_level(score),"review_status":"pending_review" if pair_signals else "not_triggered","source_record_ids":_ids(source),"evidence_refs":_refs(source),"interpretation":"报价模式仅用于待复核异常线索排序；单一价格接近、尾数或统计偏离不能直接认定围标、串标或违法违规。" if pair_signals else "已完成可用报价比较，未触发组合信号；缺失或排除字段不参与低风险判断。"}
        features.append(feature)
        if pair_signals: edges.append({"id":feature["feature_id"],"project_id":project_id,"source":f"{project_id}:{left_id}","target":f"{project_id}:{right_id}","type":"quote_pattern","risk_score":score,"risk_level":feature["risk_level"],"signal_types":[signal["signal_type"] for signal in pair_signals],"source_record_ids":feature["source_record_ids"],"evidence_refs":feature["evidence_refs"]})
    return features,edges


def analyze_quote_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    projects: dict[str,dict[str,list[dict[str,Any]]]]=defaultdict(lambda:defaultdict(list)); names={}; controls: dict[str,list[Decimal]]=defaultdict(list); quote_records=0; project_ids=set()
    for row in records:
        project=normalize_text(row.get("project_id")); bidder=normalize_text(row.get("bidder_id"));
        if project: project_ids.add(project)
        control,state=_amount(row,"control_amount")
        if project and control is not None and state=="available": controls[project].append(control)
        if row.get("record_type") not in {"quote","bid"}: continue
        quote_records+=1
        if not project or not bidder: continue
        projects[project][bidder].append(row); name=normalize_text(row.get("bidder_name"))
        if name: names[(project,bidder)]=name
    features=[]; nodes=[]; edges=[]
    for project,bidder_rows in sorted(projects.items()):
        for bidder in sorted(bidder_rows): nodes.append({"id":f"{project}:{bidder}","project_id":project,"bidder_id":bidder,"label":names.get((project,bidder),bidder),"type":"bidder"})
        project_features,project_edges=_project_features(project,bidder_rows,{bidder:names.get((project,bidder),bidder) for bidder in bidder_rows},controls[project]); features.extend(project_features); edges.extend(project_edges)
    status_counts=Counter(); signal_counts=Counter()
    for feature in features:
        for signal,status in feature["signal_status"].items(): status_counts[f"{signal}:{status}"]+=1
        for signal in feature["signals"]: signal_counts[signal["signal_type"]]+=1
    triggered_signals = [signal for feature in features for signal in feature["signals"]]
    evidence_trace_rate = round(sum(bool(signal["evidence_refs"]) for signal in triggered_signals) / len(triggered_signals), 4) if triggered_signals else 1.0
    all_triggered_traceable = all(bool(signal["evidence_refs"]) for signal in triggered_signals)
    summary={"schema_version":QUOTE_ANALYSIS_SCHEMA_VERSION,"algorithm_version":QUOTE_ALGORITHM_VERSION,"generated_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),"record_count":len(records),"quote_record_count":quote_records,"project_count":len(projects),"bidder_count":len({bidder for project in projects.values() for bidder in project}),"analyzed_project_count":sum(1 for project in projects.values() if len(project)>=2),"not_provided_project_count":len(project_ids-projects.keys()),"valid_total_quote_count":sum(1 for project in projects.values() for rows in project.values() for row in rows if _scope(row)=="total" and _amount(row)[0] is not None),"expected_pair_count":len(features),"completed_pair_count":len(features),"triggered_pair_count":sum(bool(feature["signals"]) for feature in features),"high_risk_pair_count":sum(feature["risk_level"]=="high" for feature in features),"medium_risk_pair_count":sum(feature["risk_level"]=="medium" for feature in features),"low_risk_pair_count":sum(feature["risk_level"]=="low" for feature in features),"available_signal_types":[signal for signal in TOTAL_SIGNALS+ITEM_SIGNALS if any(feature["signal_status"].get(signal)!="not_provided" for feature in features)],"signal_counts":dict(sorted(signal_counts.items())),"status_counts":dict(sorted(status_counts.items())),"evidence_trace_rate":evidence_trace_rate,"all_triggered_traceable":all_triggered_traceable,"notice":"报价模式分析采用项目内无监督稳健统计与结构规则。输出为待复核异常线索；单一弱价格信号不得直接认定围标、串标或违法违规。"}
    return {"summary":summary,"features":features,"graph":{"schema_version":QUOTE_ANALYSIS_SCHEMA_VERSION,"algorithm_version":QUOTE_ALGORITHM_VERSION,"nodes":nodes,"edges":edges,"projects":sorted(projects)}}


def build_quote_analysis(input_path: Path, output_dir: Path) -> dict[str, Any]:
    records=_read_jsonl(input_path/"audit_records.jsonl" if input_path.is_dir() else input_path); result=analyze_quote_records(records); output_dir.mkdir(parents=True,exist_ok=True); _write_jsonl(output_dir/"price_features.jsonl",result["features"]); _write_json(output_dir/"price_graph.json",result["graph"]); _write_json(output_dir/"price_analysis_summary.json",result["summary"]); return {"output_dir":str(output_dir),"feature_count":len(result["features"]),"edge_count":len(result["graph"]["edges"]),"summary":result["summary"]}


def load_quote_analysis(output_dir: Path) -> dict[str, Any]:
    summary_path,graph_path=output_dir/"price_analysis_summary.json",output_dir/"price_graph.json"
    summary=json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {"schema_version":QUOTE_ANALYSIS_SCHEMA_VERSION,"algorithm_version":QUOTE_ALGORITHM_VERSION,"record_count":0,"quote_record_count":0,"project_count":0,"bidder_count":0,"expected_pair_count":0,"completed_pair_count":0,"triggered_pair_count":0,"available_signal_types":[],"signal_counts":{},"evidence_trace_rate":1.0,"all_triggered_traceable":True,"notice":"尚未导入可用于报价模式分析的标准报价记录。"}
    graph=json.loads(graph_path.read_text(encoding="utf-8")) if graph_path.exists() else {"schema_version":QUOTE_ANALYSIS_SCHEMA_VERSION,"nodes":[],"edges":[],"projects":[]}
    return {"summary":summary,"features":_read_jsonl(output_dir/"price_features.jsonl"),"graph":graph}
