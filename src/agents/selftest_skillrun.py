"""Selftest: the model lineup and skill_report (src/model_registry.py, agents/skillrun.py, 2026-11-17).

Zero network: OpenRouter is a fake (skillrun.POST). The skills' own generators run for real on the fixtures,
so a change to a generator's input contract shows up here as a failed render."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

COMPS_CSV = "\n".join([
    "Address,City,Sold,Price,Beds,Baths,Sqft,$/sf,Miles,Bucket,Buyer (deed),Cash,Source",
    "10 Example St,Knoxville,2026-09-01,200000,2,1.0,1000,200,0.2,RENOVATED/RETAIL,A SAMPLE,,SiftMap deed",
    "12 Example St,Knoxville,2026-08-01,210000,2,1.0,1000,210,0.3,RENOVATED/RETAIL,B SAMPLE,,SiftMap deed",
    "14 Example St,Knoxville,2026-07-01,190000,2,1.0,1000,190,0.4,RENOVATED/RETAIL,C SAMPLE,,SiftMap deed",
    "20 Sample Ave,Knoxville,2026-09-15,80000,2,1.0,1000,80,0.5,DISTRESSED,SAMPLE HOLDINGS LLC,cash,SiftMap deed",
    "22 Sample Ave,Knoxville,2026-06-15,90000,2,1.0,1000,90,0.6,DISTRESSED,DEMO HOMES LLC,cash,SiftMap deed",
    "24 Sample Ave,Knoxville,2026-05-15,70000,2,1.0,1000,70,0.7,DISTRESSED,TEST PROPS LLC,cash,SiftMap deed",
]) + "\n"
SUBJECT = {"subject": {"street": "1 Test Way", "city": "Knoxville", "state": "TN", "zip": "37917", "county": "Knox",
                       "beds": 2, "baths": 1, "sqft": 1000, "year_built": 1950},
           "engine": {"tight_arv": 201000, "comps_used": 3, "basis": "3 same-bed sales"}, "months": 12}


def comp(addr, price, cond):
    return {"address": addr, "sale_date": "2026-09-01", "sale_price": price, "gla": 1000, "ppsf": price / 1000,
            "beds": 2, "baths": 1, "year_built": 1950, "condition": cond, "distance": 0.3, "total_adjustments": 0,
            "adjusted_value": price, "outlier_flag": False, "outlier_reason": ""}


def comps_report(bump: int = 0) -> dict:
    comps = [comp("10 Example St", 200000 + bump, "Renovated"), comp("12 Example St", 210000, "Renovated"),
             comp("14 Example St", 190000, "Renovated"), comp("20 Sample Ave", 80000, "Unrenovated"),
             comp("22 Sample Ave", 90000, "Unrenovated"), comp("24 Sample Ave", 70000, "Unrenovated")]
    return {
        "subject_property": {"address": "1 Test Way", "city": "Knoxville", "state": "TN", "zip": "37917",
                             "county": "Knox", "subdivision": "", "property_type": "Single family", "gla": 1000,
                             "lot_size": "0.2 ac", "beds": 2, "baths": 1, "year_built": 1950},
        "comps": comps,
        "outlier_screening": {"median_ppsf": 135, "std_dev_ppsf": 60, "threshold_low": 30, "threshold_high": 300,
                              "excluded_comps": [], "flagged_comps": []},
        "bucket_analysis": {"unrenovated": {"count": 3, "median_ppsf": 80.0, "comps": ["20 Sample Ave"]},
                            "renovated": {"count": 3, "median_ppsf": 200.0, "comps": ["10 Example St"]},
                            "market_premium_pct": 150.0, "spread_sanity": "past 30% (investor tier)"},
        "market_sentiment": {"dom_trend": "flat", "inventory_months": 3, "yoy_price_change_pct": 0,
                             "sale_to_list_ratio": 1.0, "classification": "Neutral", "sentiment_adjustment_pct": 0},
        "market_overview": {"market_phase": "steady", "median_price": 140000, "median_ppsf": 135, "avg_dom": 30,
                            "sale_to_list_ratio": 1.0, "active_count": 0, "pending_count": 0, "notes": ""},
        "arv_calculation": {"base_ppsf": 80.0, "market_premium_pct": 150.0, "renovated_ppsf": 200.0,
                            "subject_gla": 1000, "as_is_value": 80000, "base_arv": 200000,
                            "feature_adjustments": 0, "sentiment_adjustment_pct": 0,
                            "same_bed_median_sale_price": 200000, "bedroom_band_cap_applied": False,
                            "final_arv": 200000, "confidence_level": "Medium"},
        "adjustments_applied": [], "sources": ["County deed sales (SiftMap)"],
        "search_parameters": {"time_window": "12 months", "radius": "drawn area", "gla_range": "700-1350",
                              "additional_filters": "2 bed"},
        "recommendations": ["Walk it before offering \u2014 the roof is unknown."],
        "caveats": ["Three renovated sales only."]}


def rehab_report(bad_total: bool = False) -> dict:
    items = [{"item": "LVP flooring", "include": True, "qty": 1000, "unit": "sf", "unit_cost": 3.0,
              "total": 3000.0 if not bad_total else 3500.0, "notes": ""},
             {"item": "Interior paint", "include": True, "qty": 1000, "unit": "sf", "unit_cost": 2.0, "total": 2000.0,
              "notes": ""}]
    est = {"scope": "Full Rehab", "finish_tier": "Investor-Flip Grade",
           "categories": [{"category": "Interior", "items": items, "category_total": 5000.0}],
           "subtotal": 5000, "contingency_pct": 0.10, "contingency": 500, "grand_total": 5500, "cost_per_sf": 5.5}
    empty = {"scope": "Wholetail", "finish_tier": "Builder to Mid Grade", "categories": [], "subtotal": 0,
             "contingency_pct": 0.05, "contingency": 0, "grand_total": 0, "cost_per_sf": 0.0}
    return {
        "property": {"address": "1 Test Way", "city": "Knoxville", "state": "TN", "zip": "37917", "county": "Knox",
                     "gla": 1000, "beds": 2, "baths": 1.0, "year_built": 1950, "lot_size": "0.2 ac",
                     "property_type": "Single family", "condition_summary": "Dated, sound"},
        "comp_reference": {"arv": 200000, "bucket_a_ppsf": 80.0, "bucket_b_ppsf": 200.0,
                           "renovation_premium_pct": 150.0, "market_premium_dollars": 120000,
                           "comp_finish_notes": "LVP and paint", "purchase_price": 90000},
        "condition_assessment": [{"category": "Interior", "item": "Floors", "condition": "Poor", "notes": "",
                                  "photo_reference": "01"}],
        "local_pricing": {"city": "Knoxville", "state": "TN", "cost_index": 0.88, "labor_rate_multiplier": 0.88,
                          "material_rate_multiplier": 1.0, "notes": "", "sources": ["locked list"]},
        "rehab_estimate": est, "wholetail_estimate": empty,
        "deal_analysis": {"arv": 200000, "purchase_price": 90000, "rehab_cost": 5500, "wholetail_cost": 0,
                          "hold_time_months": 4,
                          "hold_time_breakdown": {"rehab_duration_weeks": 4, "rehab_duration_months": 1,
                                                  "comp_dom_days": None, "marketing_period_months": 2,
                                                  "closing_period_months": 1, "source": "calculated", "notes": ""},
                          "financing": {"loan_amount": 95500, "ltv_pct": 1.0, "interest_rate_annual": 0.12,
                                        "points_pct": 0, "monthly_interest": 955, "total_financing_cost": 3820,
                                        "notes": ""},
                          "holding_costs": {"property_taxes_monthly": 100, "insurance_monthly": 100, "hoa_monthly": 0,
                                            "gas_monthly": 0, "water_monthly": 50, "electric_monthly": 50,
                                            "other_monthly": 0, "total_monthly": 300, "total_holding": 1200},
                          "buying_costs": {"escrow_attorney": 900, "title_insurance": 693, "title_insurance_pct": 0.0077,
                                           "total_buying": 1593},
                          "selling_costs": {"realtor_pct": 0.05, "realtor_fees": 10000, "title_insurance": 1280,
                                            "title_insurance_pct": 0.0064, "escrow_attorney": 900,
                                            "recording_fees": 400, "transfer_tax": 240, "transfer_tax_pct": 0.0012,
                                            "home_warranty": 750, "total_selling": 13570},
                          "rehab_total_all_in": 115683, "wholetail_total_all_in": 0, "rehab_net_profit": 84317,
                          "wholetail_net_profit": 0, "rehab_roi_pct": 72.9, "wholetail_roi_pct": 0.0,
                          "mao_75_pct": 144500, "rehab_margin_vs_premium": 0.05},
        "material_specs": [], "recommendations": ["Flipper MAO is 70% of ARV minus the rehab."],
        "caveats": ["Roof not seen."]}


def reply(obj, cost=0.04):
    return 200, {"choices": [{"message": {"content": json.dumps(obj)}, "finish_reason": "stop"}],
                 "usage": {"cost": cost}, "provider": "FakeHost"}


def run(check, tmp: Path) -> None:
    import model_registry
    from . import config, sandbox, skillrun, skilltools, store, team
    from .research import open_research

    # ---- the lineup, as the code reads it on a box that overrides nothing
    env_names = [j["env"] for j in model_registry.JOBS.values()]
    keep_env = {k: os.environ.pop(k, None) for k in env_names + ["OPENROUTER_API_KEY"]}
    try:
        check(model_registry.model("employees") == "claude-haiku-5-5"
              and model_registry.model("text_replies") == "claude-haiku-5-5"
              and model_registry.model("file_skills") == "openai/gpt-6.1-terra"
              and model_registry.model("who_signs") == "openai/gpt-6.1-terra",
              "the lineup: Haiku for the team and the replies, Terra for the file skills and who signs")
        check(open_research.settings()["analysis"] == model_registry.default("who_signs")
              and open_research.settings()["referee"] == model_registry.default("research_referee"),
              "the open research engine reads its models from the model file")
        roster = team.load()
        check(all(e.model == config.MODEL for e in roster.employees.values()),
              "no employee names its own model: the roster defers to the model file")

        # ---- dashes and summary hygiene
        check(skillrun.undash("ARV $190,000\u2013$200,000 \u2014 tight") == "ARV $190,000-$200,000, tight",
              "a dash between numbers becomes a hyphen, any other em or en dash a comma")
        check(skillrun.clean_summary(["ARV $200,000", "No workbook or PDF was generated; not an appraisal."])
              == ["ARV $200,000"], "a summary line claiming no workbook was made is dropped")
        check(skillrun.clean_summary(["Rehab $53,708.40, MAO $128,991.60"]) == ["Rehab $53,708, MAO $128,992"],
              "a summary reads whole dollars; the workbook keeps the exact figures")
        msgs = skillrun.rehab_messages(None, "3/1 in Knoxville TN 37909", [], None, None, [])
        check("locked master material list" in msgs[1]["content"][0]["text"],
              "a rehab asked without a comps run still gets the locked list when the notes place it in Knox or Blount")

        # ---- comps through skill_report
        tid, _ = store.enqueue("sales", "ask", "", subject="comps", source="slack", force=True)
        task = store.task(tid)
        ex = sandbox.run_dir_for(task) / "exports"
        ex.mkdir(parents=True, exist_ok=True)
        (ex / "comps_1_Test_Way.csv").write_text(COMPS_CSV, encoding="utf-8")
        (ex / "subject_1_Test_Way.json").write_text(json.dumps(SUBJECT), encoding="utf-8")
        seen: list = []
        queue: list = []

        def fake(body, key):
            seen.append(body)
            return queue.pop(0)

        keep_post, keep_cap = skillrun.POST, config.DAILY_CAP_USD
        skillrun.POST = fake
        try:
            res = skillrun.run(task, "sales", {"skill": "real-estate-comping"})
            check(not res["ok"] and "OPENROUTER_API_KEY" in res["why"] and not seen,
                  "with no OpenRouter key the tool says so before any call, and the employee works the skill by hand")
            os.environ["OPENROUTER_API_KEY"] = "test-key"
            queue[:] = [reply({"report": comps_report(), "summary": ["ARV $200,000 \u2014 3 comps"]})]
            spent0 = store.spent(day=store.today())
            res = skillrun.run(task, "sales", {"skill": "real-estate-comping", "address": "1 Test Way"})
            check(res["ok"] and res["model"] == "openai/gpt-6.1-terra" and res["attempts"] == 1
                  and res["figures"]["arv"] == 200000 and res["figures"]["as_is"] == 80000,
                  "a right comps answer passes its checks on the first try: ARV and the investor-tier as-is")
            wb = sandbox.run_dir_for(task) / "files" / res["file"]["name"]
            check(wb.is_file() and wb.stat().st_size > 2000 and res["file"]["name"].startswith("Comps_1_Test_Way"),
                  "the skill's own generator built the workbook in the thread's files")
            body = seen[-1]
            prompt = json.dumps(body["messages"])
            check(body["provider"] == {"data_collection": "deny", "require_parameters": True}
                  and body["response_format"] == {"type": "json_object"} and "temperature" not in body,
                  "every call: no data kept by the host, JSON only, no temperature (reasoning hosts refuse it)")
            check("201000" not in prompt and "201,000" not in prompt and res["figures"].get("engine_arv") == 201000,
                  "the engine's ARV is withheld from the model and added beside its answer afterwards")
            check("As-is means what a cash investor pays" in prompt and "Two-Bucket" in prompt,
                  "the house as-is rule and the disclosure method ride in every comps prompt")
            check(res["summary"] == ["ARV $200,000, 3 comps"], "the summary comes back without dashes")
            check(abs(store.spent(day=store.today()) - spent0 - 0.04) < 1e-6, "the call's true cost is booked")

            queue[:] = [reply({"report": comps_report(bump=25000), "summary": ["x"]}),
                        reply({"report": comps_report(), "summary": ["ARV $200,000"]})]
            res = skillrun.run(task, "sales", {"skill": "real-estate-comping"})
            check(res["ok"] and res["attempts"] == 2 and not res["fallback_used"]
                  and "failed these checks" in json.dumps(seen[-1]["messages"][-1]),
                  "a changed sale price is caught and sent back once with the problem named")

            queue[:] = [reply({"report": comps_report(bump=25000)}), reply({"report": comps_report(bump=25000)}),
                        reply({"report": comps_report(), "summary": ["ok"]})]
            res = skillrun.run(task, "sales", {"skill": "real-estate-comping"})
            check(res["ok"] and res["fallback_used"] and seen[-1]["model"] == model_registry.model("file_skills_fallback"),
                  "two failures on the first model go once to the fallback model")

            queue[:] = [reply({"report": comps_report(bump=25000)})] * 3
            res = skillrun.run(task, "sales", {"skill": "real-estate-comping"})
            check(not res["ok"] and res["problems"] and "by hand" in res["why"],
                  "three failures come back as a refusal with the problems, never a wrong number")

            config.DAILY_CAP_USD = store.spent(day=store.today()) + 0.10
            queue[:] = [reply({"report": comps_report()})]
            res = skillrun.run(task, "sales", {"skill": "real-estate-comping"})
            check(not res["ok"] and "cap" in res["why"] and len(queue) == 1,
                  "no call is made when it could cross the day's cap")
            config.DAILY_CAP_USD = keep_cap

            # ---- rehab through skill_report
            p, n, d = skillrun.check_rehab(rehab_report(bad_total=True))
            check(any("is not the total" in x for x in p) and any("not the category total" in x for x in p),
                  "a rehab line that is not quantity times unit cost, and a category that does not add up, are caught")
            check(not skillrun.check_rehab(rehab_report())[0], "a rehab whose totals add up passes")
            from . import attachments
            inbox = (Path(config.WORKSPACES_DIR) / "sales" / attachments.INBOX
                     / attachments.thread_dir(task.get("slack_channel") or "", task.get("slack_thread") or ""))
            inbox.mkdir(parents=True, exist_ok=True)
            (inbox / "01-kitchen.png").write_bytes(
                bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                              "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"))
            queue[:] = [reply({"report": rehab_report(), "summary": ["Rehab $5,500"]}, cost=0.09)]
            res = skillrun.run(task, "sales", {"skill": "rehab-estimator", "notes": "Floors shot, paint everywhere",
                                               "arv": 200000, "purchase_price": 90000})
            body = seen[-1]
            parts = body["messages"][1]["content"]
            check(res["ok"] and res["figures"]["rehab"] == 5500 and res["file"]["name"].startswith("Rehab_"),
                  "a rehab answer whose totals add up renders with the rehab skill's own generator")
            check(isinstance(parts, list) and any(x.get("type") == "image_url" for x in parts)
                  and "locked master material list" in json.dumps(parts),
                  "the thread's walkthrough photos and, in Knox, the locked material list go with the rehab")
            res = skillrun.run(task, "sales", {"skill": "rehab-estimator", "photos": "none"})
            check(not res["ok"] and "photos" in res["why"], "a rehab with no photos and no notes is refused up front")
        finally:
            skillrun.POST, config.DAILY_CAP_USD = keep_post, keep_cap

        names = [t.name for t in skilltools.skill_tools(SimpleNamespace(employee=roster.employees["sales"],
                                                                         task={"id": 1}))]
        check(names == ["comps_data", "skill_report", "publish_sheet"], "Roma carries comps_data and skill_report")
    finally:
        for k, v in keep_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
