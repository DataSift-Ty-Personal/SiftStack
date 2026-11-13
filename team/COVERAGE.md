<!-- GENERATED from team/roster.yaml + docs/agents.json by `python src/agents/cli.py team`. Do not edit by hand. -->

# Org chart coverage

Every agent on docs/agents.json (the public org chart), the team workflow that runs it, and the employee who owns that workflow. 90 agents: 81 run by a workflow, 3 platform, 6 retired, 0 not yet assigned.

## Atlas (Analysis)

| Workflow | Status | Org-chart agents it runs |
|---|---|---|
| ftm_pull | by hand today | Scheduled FTM Runner (ftmrun), FTM Scheduler (ftmsched), Egress Proxy Resolver (proxy), Notice Scraper (scraper), Turnstile Gate Runner (gate), Notice Parser (parser), Foreclosure Filter (fcfilter), Address Standardizer (addr), Property Enricher (prop), Tax Enricher (tax), Obituary Enricher (obit), API Uploader (apiupload) |
| county_pull | by hand today | Knox FTM Puller (knoxftm), Lien Resolver (lien), Foreclosure Consolidator (consolidate), API Uploader (apiupload) |
| courthouse_import | by hand today | Courthouse Photo Importer (photo), PDF Importer (pdfimp), Dropbox Watcher (dropbox) |
| market_report | by hand today | Market Finder Extractor (finder), County Market Report Generator (report) |
| list_pull | by hand today | Doors Per Deal Ladder Pull (ladderpull), Agency Test Pools (aipools) |
| idle_records | by hand today | Obituary Opportunity Ranker (obitrank), Dream 100 Ranker (dream100), D4D Call List (d4dcalls) |
| sold_cleanup | by hand today | SiftMap Sold Tagger (siftmap) |
| account_setup | by hand today | Schema Setup (schema), Preset Manager (presets), Sequence Builder (sequences), Account Blueprint Export (bpexport), Account Blueprint Apply (bpapply), Upload Wizard Driver (wizard) |
| land_desk | by hand today | Builder Land Desk (builderland), Buildable Lot Finder (vacantland) |
| ftm_assign | autopilot stage | none: not on the org chart yet |
| ftm_auction | autopilot stage | none: not on the org chart yet |
| ftm_reactivate | autopilot stage | none: not on the org chart yet |

## Diggs (Research)

| Workflow | Status | Org-chart agents it runs |
|---|---|---|
| deep_prospect | live | Research Agent (researchagent), Single-Record Deep Prospect (dprecord), Dream 100 Deep Prospect (dream100dp), Deep Prospecting v5 (dp5) |
| skip_trace | by hand today | Tracerfy Skip Trace (tracerfy) |
| phone_score | by hand today | Phone Validator (phone) |
| entity_principals | by hand today | Entity Researcher (entity) |
| probate_property | by hand today | Probate Property Finder (probfind) |
| buyer_registry | by hand today | Buyer Sweep (sweep), Dispo Buyer Registry (dispobuyers), Dispo Skiptrace (disposkip), Buyer Prospector (prospector) |
| vendor_directory | by hand today | Vendor Directory Builder (vendordir), Contractor Call Sheet (callsheet) |
| ftm_native_trace | autopilot stage | none: not on the org chart yet |
| ftm_retrace | autopilot stage | Tracerfy Skip Trace (tracerfy) |
| ftm_exhausted | autopilot stage | Tracerfy Skip Trace (tracerfy) |
| ftm_score | autopilot stage | Phone Validator (phone) |
| ftm_deep_prospect | autopilot stage | none: not on the org chart yet |
| ftm_return_mail | autopilot stage | none: not on the org chart yet |
| ftm_no_numbers | autopilot stage | none: not on the org chart yet |
| hottest_retrace | autopilot stage | Tracerfy Skip Trace (tracerfy) |
| hottest_score | autopilot stage | Phone Validator (phone) |

## Roma (Sales Assistant)

| Workflow | Status | Org-chart agents it runs |
|---|---|---|
| quick_screen | by hand today | Comp Package Engine (comp) |
| underwrite | by hand today | Dual-Track ARV Engine (arv), Rehab Estimator (rehab), Master Material List (material), Exit Strategy Engine (exits), Deal Analyzer (dealanalyzer), Lot Split Underwriter (lotsplit) |
| offer_sheet | by hand today | none: not on the org chart yet |
| deal_buyers | by hand today | Deal Buyer Match (dealmatch) |
| dispo_blast | by hand today | Dispo Deal Blast (dispoblast), Dispo Buyer Phonebook (dispobook), Dispo Sequential Flow (dispoflow) |
| lender_package | by hand today | Lender Analysis (lender), Lender Package Builder (lenderpkg), Lender Document Set (lenderdocs) |
| sms_replies | by hand today | Reply Classifier (classifier), Reply Drafter (responder), Smarter Contact Reply Agent (scinbound) |

## Quinn (Operations)

| Workflow | Status | Org-chart agents it runs |
|---|---|---|
| daily_digest | by hand today | none: not on the org chart yet |
| kpi_report | by hand today | KPI Engine (kpi) |
| qa_batch | by hand today | Pre-Send Audit (scaudit) |
| call_coaching | by hand today | Call Puller (callpull), Transcription & Triage (transcribe), Cold Call Coach (coldcoach), Lead Manager Coach (lmcoach), Closer Coach (closercoach) |
| sop_writer | by hand today | Playbook Creator (playbookgen) |
| outreach_health | by hand today | Deliverability Readout (deliver), Caller Reputation Monitor (callerrep) |
| sms_campaign | by hand today | Smarter Contact Campaign Builder (sccampaign), Text Touch Builder (texttouch), Sender Pool Manager (sender), MMS Screenshot Sender (mms) |
| obituary_mail | by hand today | Obituary Mail Campaign (obitcamp), Obituary Mail Export (obitmail) |
| ftm_mail | autopilot stage | none: not on the org chart yet |
| autopilot_digest | autopilot stage | none: not on the org chart yet |

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
