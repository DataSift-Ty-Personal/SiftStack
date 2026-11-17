"""Acquisition playbook generator with SOPs, scripts, and daily checklists.

Generates custom playbooks based on investment blueprint (wholesale/flip/hold),
market (Knoxville/Blount), and team size (solo to full operation).

Output:
  - Markdown playbook document (comprehensive SOP)
  - Daily checklist (printable)
  - Script templates (per notice type × channel)

Usage:
  python src/main.py playbook --blueprint wholesale --market knoxville --team-size 1
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import config

logger = logging.getLogger(__name__)

# ── Blueprint definitions ─────────────────────────────────────────────

BLUEPRINTS = {
    "wholesale": {
        "name": "Wholesale",
        "description": "Assign contracts to cash buyers — no rehab, no ownership",
        "pipeline": ["Lead → ", "Comp → ", "Offer → ", "Contract → ", "Assign → ", "Close"],
        "timeline": "7-14 days per deal",
        "capital_needed": "$0 (earnest money only, refundable)",
        "risk_level": "Low",
        "profit_target": "$5,000-$15,000 per assignment",
    },
    "flip": {
        "name": "Fix-and-Flip",
        "description": "Purchase, renovate, sell at retail — highest profit, highest risk",
        "pipeline": ["Lead → ", "Comp → ", "Rehab Est → ", "Offer → ", "Close → ", "Rehab → ", "List → ", "Sell"],
        "timeline": "3-6 months per deal",
        "capital_needed": "$30,000-$100,000+ (purchase + rehab + holding)",
        "risk_level": "High",
        "profit_target": "$25,000-$75,000+ per flip",
    },
    "hold": {
        "name": "Buy-and-Hold",
        "description": "Purchase, stabilize, rent — build portfolio for cash flow",
        "pipeline": ["Lead → ", "Comp → ", "Cash Flow Analysis → ", "Offer → ", "Close → ", "Stabilize → ", "Tenant"],
        "timeline": "30-60 days to stabilize, then indefinite hold",
        "capital_needed": "$20,000-$50,000 (down payment + minor repairs)",
        "risk_level": "Medium",
        "profit_target": "$200-$500/mo cash flow + appreciation",
    },
    "hybrid": {
        "name": "Hybrid (Wholesale + Flip)",
        "description": "Wholesale most deals, cherry-pick best ones to flip",
        "pipeline": ["Lead → ", "Comp → ", "Triage → ", "Wholesale OR Flip"],
        "timeline": "Mixed — 7 days wholesale, 3-6 months flip",
        "capital_needed": "$30,000+ reserve for flip opportunities",
        "risk_level": "Medium",
        "profit_target": "Volume from wholesale, margin from flips",
    },
}

# ── Team role definitions ─────────────────────────────────────────────

TEAM_ROLES = {
    "acquisitions_manager": {
        "title": "Acquisitions Manager",
        "responsibilities": [
            "Review incoming leads and assign to team",
            "Run comps and rehab estimates on qualified leads",
            "Make offers and negotiate contracts",
            "Manage deal pipeline from lead to close",
        ],
    },
    "lead_manager": {
        "title": "Lead Manager / Cold Caller",
        "responsibilities": [
            "Execute niche sequential marketing (SMS, calls, mail)",
            "Qualify leads using 4 Pillars of Motivation",
            "Set appointments for acquisitions manager",
            "Update CRM with call dispositions",
        ],
    },
    "skip_tracer": {
        "title": "Skip Tracer / Researcher",
        "responsibilities": [
            "Run skip traces on new records",
            "Score phone numbers via Trestle",
            "Deep prospecting for deceased/complex records",
            "Entity research (LLC → person identification)",
        ],
    },
    "closer": {
        "title": "Closer / Disposition Manager",
        "responsibilities": [
            "Handle buyer relationships and negotiations",
            "Manage closing process and title coordination",
            "Calculate profit and track deal metrics",
            "Build and maintain cash buyer list",
        ],
    },
    "transaction_coordinator": {
        "title": "Transaction Coordinator",
        "responsibilities": [
            "Manage paperwork and deadlines",
            "Coordinate with title company, lender, attorney",
            "Track due diligence timeline",
            "Handle post-closing documentation",
        ],
    },
}

TEAM_CONFIGS = {
    1: {
        "name": "Solo Operator",
        "description": "One person handles everything. Focus on wholesale for speed.",
        "roles": {
            "You": list(TEAM_ROLES.keys()),  # All roles combined
        },
    },
    2: {
        "name": "Two-Person Team",
        "description": "Split: one handles leads/marketing, one handles deals/closing.",
        "roles": {
            "Person 1 (Lead Gen)": ["lead_manager", "skip_tracer"],
            "Person 2 (Acquisitions)": ["acquisitions_manager", "closer", "transaction_coordinator"],
        },
    },
    5: {
        "name": "Full Operation",
        "description": "Dedicated roles with specialization. Maximum throughput.",
        "roles": {
            "Acquisitions Manager": ["acquisitions_manager"],
            "Lead Manager": ["lead_manager"],
            "Skip Tracer": ["skip_tracer"],
            "Closer": ["closer"],
            "Transaction Coordinator": ["transaction_coordinator"],
        },
    },
}

# ── Script templates ──────────────────────────────────────────────────

CALL_SCRIPTS = {
    "foreclosure": {
        "day1": (
            "Hi, is this {name}? My name is [Your Name], and I'm a local investor here in "
            "{market}. I'm reaching out because I noticed there's a trustee sale notice "
            "filed on your property at {address}. I know that can be stressful, and I "
            "wanted to see if I could help. Are you looking to sell before the auction date? "
            "I can make a fair cash offer and close quickly if the timing works for you."
        ),
        "day2": (
            "Hi {name}, this is [Your Name] again — I called yesterday about your property "
            "at {address}. I know you've got a lot going on right now with the foreclosure "
            "process. I just wanted to let you know I can still make a cash offer and "
            "potentially close before the auction. Would you have a few minutes to talk?"
        ),
        "day3": (
            "Hey {name}, [Your Name] one last time. I want to make sure you know all your "
            "options for {address} before the sale date. Even if selling to me isn't the "
            "right fit, I'm happy to point you in the right direction. Give me a call back "
            "at [phone] — no pressure."
        ),
    },
    "probate": {
        "day1": (
            "Hi, is this {name}? My name is [Your Name], and I'm a local investor in "
            "{market}. I'm reaching out because I understand you may be managing a property "
            "at {address} as part of an estate. Handling inherited property can be "
            "overwhelming, especially on top of everything else. If you'd ever consider "
            "selling, I buy properties as-is for cash and can close on your timeline."
        ),
        "day2": (
            "Hi {name}, [Your Name] following up. I reached out about the property at "
            "{address}. I work with a lot of families going through probate and can handle "
            "everything from title to closing. No repairs needed, no agent commissions. "
            "Would that be helpful to talk about?"
        ),
        "day3": (
            "{name}, this is [Your Name] — last call about {address}. I know probate is a "
            "long process and the timing may not be right yet. When you're ready, I'm here. "
            "My number is [phone]. Wishing you and your family the best."
        ),
    },
    "tax_sale": {
        "day1": (
            "Hi {name}, this is [Your Name], a local real estate investor. I noticed your "
            "property at {address} may have some outstanding tax obligations. I work with "
            "property owners in similar situations and can offer a quick cash sale to help "
            "resolve things. Would you be open to hearing more?"
        ),
        "day2": (
            "{name}, [Your Name] again about {address}. I know tax situations can feel "
            "urgent. I can close in as little as 7-14 days with cash. No agent fees, no "
            "repairs needed. Would that be helpful?"
        ),
        "day3": (
            "Final call, {name} — [Your Name]. Just want to make sure you have my info "
            "for {address}. [phone]. Call anytime if you'd like to explore your options."
        ),
    },
    "eviction": {
        "day1": (
            "Hi, is this {name}? My name is [Your Name], a local investor here in "
            "{market}. I noticed you've been dealing with some tenant issues at your "
            "property on {address}. If managing that property has become more trouble "
            "than it's worth, I'd be happy to make a fair cash offer so you can move on."
        ),
        "day2": (
            "{name}, [Your Name] again about {address}. Dealing with problem tenants is "
            "exhausting — I get it. I buy properties as-is, even with tenants in place. "
            "Would that be worth discussing?"
        ),
        "day3": (
            "Hey {name}, [Your Name] one more time. Just checking in on {address}. "
            "My offer stands whenever you're ready. [phone] — no pressure."
        ),
    },
    "default": {
        "day1": (
            "Hi {name}, this is [Your Name], a local real estate investor in {market}. "
            "I'm reaching out about your property at {address}. I buy properties for "
            "cash and can close quickly. Would you be interested in hearing a fair offer?"
        ),
        "day2": (
            "{name}, [Your Name] following up on {address}. I can make a quick, hassle-free "
            "cash offer — no repairs, no commissions. Worth a quick chat?"
        ),
        "day3": (
            "Last call, {name} — [Your Name] about {address}. My number is [phone]. "
            "Call anytime if you'd like to explore selling."
        ),
    },
}

SMS_TEMPLATES = {
    1: "Hi {first_name}, I noticed your property at {address}. Are you or anyone in the "
       "family considering selling? I buy homes for cash and can close quickly. - [Your Name]",
    2: "Hey {first_name}, just following up on your property at {address}. If you're "
       "interested in a quick, fair cash offer, I'd love to chat. - [Your Name]",
    3: "Last message, {first_name} — I have a cash offer ready for {address}. If the "
       "timing isn't right, no worries at all. Let me know! - [Your Name]",
}

VOICEMAIL_SCRIPTS = {
    1: "Hi {name}, this is [Your Name], a local investor here in {market}. I'm calling "
       "about your property at {address}. I'd love to chat about making you a fair cash "
       "offer. Please call me back at [phone]. Thanks!",
    2: "{name}, [Your Name] again — calling back about {address}. I can make a cash offer "
       "with no repairs, no commissions, and close on your timeline. [phone]. Thanks!",
    3: "Hey {name}, [Your Name] one last time about {address}. I want to make sure you "
       "know your options. My number is [phone] — call anytime. Have a great day.",
}

# ── Daily checklist ───────────────────────────────────────────────────

DAILY_CHECKLIST = {
    "morning": [
        "STABM Check: Status → Tasks → Board → Messages (5 min)",
        "Review new leads from overnight scrape/import",
        "Check hot lead alerts and callback reminders",
        "Plan today's call/text targets (Dial First → Second → Third)",
    ],
    "midday": [
        "Execute niche sequential: Text Day 1/2/3 targets",
        "Cold call queue: work through call list by dial tier",
        "Log all dispositions in DataSift (interested/not interested/callback/etc.)",
        "Update lead statuses and tags for any responses",
    ],
    "afternoon": [
        "Run comps on any qualified leads",
        "Prepare offers for hot leads",
        "Process mail list — prepare direct mail pieces",
        "Deep prospecting on flagged records (30 min)",
    ],
    "evening": [
        "Update CRM: move leads through pipeline",
        "Review today's metrics (calls made, contacts, appointments)",
        "Plan tomorrow's priority list",
        "Check DataSift for overnight skip trace / enrichment results",
    ],
}


# ── Playbook generation ──────────────────────────────────────────────

def generate_playbook(blueprint: str = "wholesale", market: str = "knoxville",
                      team_size: int = 1, output_path: str = "") -> str:
    """Generate a comprehensive acquisition playbook as Markdown.

    Returns path to generated playbook file.
    """
    bp = BLUEPRINTS.get(blueprint, BLUEPRINTS["wholesale"])
    team = TEAM_CONFIGS.get(team_size, TEAM_CONFIGS[1])
    market_name = market.title()

    lines = []
    lines.append(f"# {bp['name']} Acquisition Playbook — {market_name}")
    lines.append(f"")
    lines.append(f"**Blueprint:** {bp['name']} — {bp['description']}")
    lines.append(f"**Market:** {market_name}, Tennessee")
    lines.append(f"**Team:** {team['name']} ({team_size} {'person' if team_size == 1 else 'people'})")
    lines.append(f"**Generated:** {datetime.now().strftime('%Y-%m-%d')}")
    lines.append(f"")

    # Overview
    lines.append(f"## Overview")
    lines.append(f"")
    lines.append(f"- **Pipeline:** {''.join(bp['pipeline'])}")
    lines.append(f"- **Timeline:** {bp['timeline']}")
    lines.append(f"- **Capital Needed:** {bp['capital_needed']}")
    lines.append(f"- **Risk Level:** {bp['risk_level']}")
    lines.append(f"- **Profit Target:** {bp['profit_target']}")
    lines.append(f"")

    # Team structure
    lines.append(f"## Team Structure — {team['name']}")
    lines.append(f"")
    lines.append(f"{team['description']}")
    lines.append(f"")
    for person, role_keys in team["roles"].items():
        lines.append(f"### {person}")
        for key in role_keys:
            role = TEAM_ROLES.get(key, {})
            if role:
                lines.append(f"**{role['title']}:**")
                for resp in role["responsibilities"]:
                    lines.append(f"  - {resp}")
        lines.append(f"")

    # Daily routine
    lines.append(f"## Daily Routine (STABM)")
    lines.append(f"")
    for period, tasks in DAILY_CHECKLIST.items():
        lines.append(f"### {period.title()}")
        for task in tasks:
            lines.append(f"- [ ] {task}")
        lines.append(f"")

    # Scripts
    lines.append(f"## Call Scripts")
    lines.append(f"")
    for notice_type, scripts in CALL_SCRIPTS.items():
        if notice_type == "default":
            continue
        lines.append(f"### {notice_type.replace('_', ' ').title()}")
        for day, script in scripts.items():
            lines.append(f"**{day.title()}:**")
            lines.append(f"> {script}")
            lines.append(f"")

    # SMS templates
    lines.append(f"## SMS Templates")
    lines.append(f"")
    for day, template in SMS_TEMPLATES.items():
        lines.append(f"**Day {day}:** {template}")
        lines.append(f"")

    # Voicemail scripts
    lines.append(f"## Voicemail Scripts")
    lines.append(f"")
    for day, script in VOICEMAIL_SCRIPTS.items():
        lines.append(f"**Day {day}:** {script}")
        lines.append(f"")

    # Niche sequential overview
    lines.append(f"## Niche Sequential Marketing Flow")
    lines.append(f"")
    lines.append(f"**Cost escalation:** SMS ($0.01) → Call ($0.03) → Mail ($1.75) → Deep Prospecting ($1.50-4.00)")
    lines.append(f"")
    lines.append(f"### 3-Day Cycle")
    lines.append(f"")
    lines.append(f"**Day 1:** Send text → Call all numbers → Leave voicemail → Trigger handwritten mailer ($1.75)")
    lines.append(f"**Day 2:** Call (different script) → New voicemail → Send 2nd text → Mailer in transit")
    lines.append(f"**Day 3:** Final call → Urgency voicemail → Final text → Mailer arrives (1-3 day delivery)")
    lines.append(f"")
    lines.append(f"### After Cycle Complete")
    lines.append(f"- No response → Deep Prospecting (Level 1-3)")
    lines.append(f"- Not interested → 90-day recycle (rotate mailer type)")
    lines.append(f"- Interested → Route to closer")
    lines.append(f"- Callback → Follow up at scheduled time")
    lines.append(f"")
    lines.append(f"**Key stat:** 20-30% of deals come from not-interested follow-ups.")
    lines.append(f"")

    # CLI reference
    lines.append(f"## Quick CLI Reference")
    lines.append(f"")
    lines.append(f"```bash")
    lines.append(f"# Daily scrape")
    lines.append(f"python src/main.py daily --counties Knox,Blount --upload-datasift")
    lines.append(f"")
    lines.append(f"# Lead qualification")
    lines.append(f"python src/main.py lead-manage --action qualify --csv-path output/latest.csv")
    lines.append(f"")
    lines.append(f"# Comp analysis")
    lines.append(f'python src/main.py comp --address "123 Main St, Knoxville, TN 37918"')
    lines.append(f"")
    lines.append(f"# Deal analysis")
    lines.append(f'python src/main.py analyze-deal --address "123 Main St" --purchase-price 150000')
    lines.append(f"")
    lines.append(f"# Niche sequential")
    lines.append(f'python src/main.py niche-sequential --list-name "Foreclosure" --channel sms --day 1')
    lines.append(f"")
    lines.append(f"# Deep prospecting")
    lines.append(f"python src/main.py deep-prospect --csv-path output/records.csv --depth 3")
    lines.append(f"")
    lines.append(f"# Market analysis")
    lines.append(f"python src/main.py market-analysis --counties Knox,Blount")
    lines.append(f"```")

    content = "\n".join(lines)

    if not output_path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = str(config.OUTPUT_DIR / f"playbook_{blueprint}_{market}_{timestamp}.md")

    Path(output_path).write_text(content, encoding="utf-8")
    logger.info("Playbook generated: %s", output_path)
    return output_path


# ── Main entry point ──────────────────────────────────────────────────

def run_playbook_generator(blueprint: str = "wholesale", market: str = "knoxville",
                           team_size: int = 1, output_path: str = "") -> dict:
    """Generate acquisition playbook.

    Returns dict with playbook path.
    """
    logger.info("Generating %s playbook for %s (team size %d)", blueprint, market, team_size)

    playbook_path = generate_playbook(blueprint, market, team_size, output_path)

    return {
        "playbook_path": playbook_path,
        "blueprint": blueprint,
        "market": market,
        "team_size": team_size,
    }
