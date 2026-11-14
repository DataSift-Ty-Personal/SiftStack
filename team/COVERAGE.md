<!-- GENERATED from team/roster.yaml + docs/agents.json by `python src/agents/cli.py team`. Do not edit by hand. -->

# Org chart coverage

Every agent on docs/agents.json (the public org chart), the team workflow that runs it, and the employee who owns that workflow. 90 agents: 81 run by a workflow, 3 platform, 6 retired, 0 not yet assigned.

## Atlas (Analysis)

| Workflow | Status | Org-chart agents it runs |
|---|---|---|
| ftm_pull | by hand today | Scheduled FTM Runner (ftmrun), FTM Scheduler (ftmsched), Egress Proxy Resolver (proxy), Notice Scraper (scraper), Turnstile Gate Runner (gate), Notice Parser (parser), Foreclosure Filter (fcfilter), Address Standardizer (addr), Property Enricher (prop), Tax Enricher (tax), Obituary Enricher (obit), API Uploader (apiupload) |
| county_pull | by hand today | Knox FTM Puller (knoxftm), Lien Resolver (lien), Foreclosure Consolidator (consolidate), API Uploader (apiupload) |
| courthouse_import | by hand today | Courthouse Photo Importer (photo), PDF Importer (pdfimp), Dropbox Watcher (dropbox) |
| market_report | live | Market Finder Extractor (finder), County Market Report Generator (report) |
| list_pull | live | Doors Per Deal Ladder Pull (ladderpull), Agency Test Pools (aipools) |
| idle_records | live | Obituary Opportunity Ranker (obitrank), Dream 100 Ranker (dream100), D4D Call List (d4dcalls) |
| sold_cleanup | by hand today | SiftMap Sold Tagger (siftmap) |
| account_setup | live | Schema Setup (schema), Preset Manager (presets), Sequence Builder (sequences), Account Blueprint Export (bpexport), Account Blueprint Apply (bpapply), Upload Wizard Driver (wizard) |
| land_desk | live | Builder Land Desk (builderland), Buildable Lot Finder (vacantland) |
| ftm_assign | autopilot stage | none: not on the org chart yet |
| top1000_assign | autopilot stage | none: not on the org chart yet |
| ftm_auction | autopilot stage | none: not on the org chart yet |
| ftm_reactivate | autopilot stage | none: not on the org chart yet |
| ladder_check | autopilot stage | none: not on the org chart yet |
| conventions_check | autopilot stage | none: not on the org chart yet |
| hot_auction_tasks | autopilot stage | none: not on the org chart yet |
| second_chances | autopilot stage | none: not on the org chart yet |
| caller_book | autopilot stage | none: not on the org chart yet |
| probate_honest | autopilot stage | none: not on the org chart yet |
| top1000_refresh | autopilot stage | none: not on the org chart yet |

## Diggs (Research)

| Workflow | Status | Org-chart agents it runs |
|---|---|---|
| deep_prospect | live | Research Agent (researchagent), Single-Record Deep Prospect (dprecord), Dream 100 Deep Prospect (dream100dp), Deep Prospecting v5 (dp5) |
| skip_trace | live | Tracerfy Skip Trace (tracerfy) |
| phone_score | live | Phone Validator (phone) |
| entity_principals | live | Entity Researcher (entity) |
| probate_property | live | Probate Property Finder (probfind) |
| buyer_registry | live | Buyer Sweep (sweep), Dispo Buyer Registry (dispobuyers), Dispo Skiptrace (disposkip), Buyer Prospector (prospector) |
| vendor_directory | by hand today | Vendor Directory Builder (vendordir), Contractor Call Sheet (callsheet) |
| ftm_death_screen | autopilot stage | none: not on the org chart yet |
| top1000_death_screen | autopilot stage | none: not on the org chart yet |
| ftm_native_trace | autopilot stage | none: not on the org chart yet |
| top1000_native_trace | autopilot stage | none: not on the org chart yet |
| ftm_retrace | autopilot stage | Tracerfy Skip Trace (tracerfy) |
| top1000_retrace | autopilot stage | Tracerfy Skip Trace (tracerfy) |
| ftm_exhausted | autopilot stage | Tracerfy Skip Trace (tracerfy) |
| ftm_score | autopilot stage | Phone Validator (phone) |
| top1000_score | autopilot stage | Phone Validator (phone) |
| ftm_deep_prospect | autopilot stage | none: not on the org chart yet |
| ftm_return_mail | autopilot stage | none: not on the org chart yet |
| ftm_no_numbers | autopilot stage | none: not on the org chart yet |
| hottest_retrace | autopilot stage | Tracerfy Skip Trace (tracerfy) |
| hottest_score | autopilot stage | Phone Validator (phone) |
| ftm_refresh | autopilot stage | none: not on the org chart yet |
| top1000_trace_refresh | autopilot stage | none: not on the org chart yet |
| strong_native_trace | autopilot stage | none: not on the org chart yet |
| strong_score | autopilot stage | none: not on the org chart yet |
| bulk_native_trace | autopilot stage | none: not on the org chart yet |
| bulk_score | autopilot stage | none: not on the org chart yet |
| research_queue | autopilot stage | none: not on the org chart yet |
| obituary_lane | autopilot stage | Obituary Mail Campaign (obitcamp), Obituary Mail Export (obitmail) |

## Roma (Sales Assistant)

| Workflow | Status | Org-chart agents it runs |
|---|---|---|
| book_appointment | live | none: not on the org chart yet |
| quick_screen | live | Comp Package Engine (comp) |
| underwrite | live | Dual-Track ARV Engine (arv), Rehab Estimator (rehab), Master Material List (material), Exit Strategy Engine (exits), Deal Analyzer (dealanalyzer), Lot Split Underwriter (lotsplit) |
| offer_sheet | live | none: not on the org chart yet |
| deal_buyers | live | Deal Buyer Match (dealmatch) |
| dispo_blast | by hand today | Dispo Deal Blast (dispoblast), Dispo Buyer Phonebook (dispobook), Dispo Sequential Flow (dispoflow) |
| lender_package | live | Lender Analysis (lender), Lender Package Builder (lenderpkg), Lender Document Set (lenderdocs) |
| sms_replies | by hand today | Reply Classifier (classifier), Reply Drafter (responder), Smarter Contact Reply Agent (scinbound), Sender Pool Manager (sender), MMS Screenshot Sender (mms) |
| revocation | autopilot stage | none: not on the org chart yet |
| full_attempt | autopilot stage | none: not on the org chart yet |
| speed_to_lead | autopilot stage | none: not on the org chart yet |
| lead_nurture | autopilot stage | none: not on the org chart yet |
| lead_appointments | autopilot stage | none: not on the org chart yet |

## Quinn (Operations)

| Workflow | Status | Org-chart agents it runs |
|---|---|---|
| kpi_report | live | none: not on the org chart yet |
| qa_batch | live | none: not on the org chart yet |
| call_coaching | live | Transcription & Triage (transcribe), Cold Call Coach (coldcoach), Lead Manager Coach (lmcoach), Closer Coach (closercoach) |
| sop_writer | by hand today | Playbook Creator (playbookgen) |
| outreach_health | live | Deliverability Readout (deliver), Caller Reputation Monitor (callerrep) |
| run_script | live | none: not on the org chart yet |
| ftm_pipeline | autopilot stage | none: not on the org chart yet |
| ftm_attempts | autopilot stage | none: not on the org chart yet |
| top1000_attempts | autopilot stage | none: not on the org chart yet |
| owner_lanes | autopilot stage | none: not on the org chart yet |
| sequence_watchdog | autopilot stage | none: not on the org chart yet |
| touch_audit | autopilot stage | none: not on the org chart yet |
| daily_capture | autopilot stage | none: not on the org chart yet |
| lead_check | autopilot stage | none: not on the org chart yet |
| ftm_mail | autopilot stage | none: not on the org chart yet |
| top1000_mail | autopilot stage | none: not on the org chart yet |
| autopilot_digest | autopilot stage | none: not on the org chart yet |
| lead_clock | autopilot stage | none: not on the org chart yet |
| lead_drips | autopilot stage | none: not on the org chart yet |
| lead_tasks | autopilot stage | none: not on the org chart yet |
| lead_weekly | autopilot stage | none: not on the org chart yet |
| lead_exits | autopilot stage | none: not on the org chart yet |
| ftm_registry_scrub | autopilot stage | none: not on the org chart yet |
| top1000_registry_scrub | autopilot stage | none: not on the org chart yet |
| ftm_texts | autopilot stage | Smarter Contact Campaign Builder (sccampaign), Text Touch Builder (texttouch) |
| top1000_texts | autopilot stage | Smarter Contact Campaign Builder (sccampaign), Text Touch Builder (texttouch) |
| ftm_followup_texts | autopilot stage | Pre-Send Audit (scaudit) |
| top1000_followup_texts | autopilot stage | Pre-Send Audit (scaudit) |
| outreach_limits | autopilot stage | none: not on the org chart yet |
| strong_dial_file | autopilot stage | none: not on the org chart yet |
| bulk_dial_file | autopilot stage | none: not on the org chart yet |
| rehash_dial_file | autopilot stage | none: not on the org chart yet |
| kpi_alarms | autopilot stage | KPI Engine (kpi) |
| goal_sheet | autopilot stage | KPI Engine (kpi) |
| deal_sources | autopilot stage | none: not on the org chart yet |
| cadence_tuner | autopilot stage | none: not on the org chart yet |
| coaching_picks | autopilot stage | Call Puller (callpull) |
| research_results | autopilot stage | none: not on the org chart yet |
| heir_lane | autopilot stage | none: not on the org chart yet |
| paid_lookups | autopilot stage | none: not on the org chart yet |
| agency_export | autopilot stage | none: not on the org chart yet |

## Not workflows

| Agent | Why |
|---|---|
| SiftStack Core (core) | the shared spine every workflow imports |
| Operator (operator) | the person running the business |
| Claude Orchestrator (orchestrator) | Claude itself, directing the team |
| Proof-of-Source Capture (shot) | retired |
| County Owner Pull (soicounty) | retired |
| Enformion Resolver (soienf) | retired |
| Realtor Score Enrichment (soienrich) | retired |
| Contact Intake (soiintake) | retired |
| Owner Name Join (soimatch) | retired |
