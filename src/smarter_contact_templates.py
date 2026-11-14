#!/usr/bin/env python3
"""Empathy-first cold-text templates for Smarter Contact, plus the inbox kit.

Same four-touch shape as the text-touch-builder skill (identity check, drip,
soft ask, breakup), with one change Ty asked for on 2026-10-20: Touch 1 opens by
acknowledging how much outreach the owner is probably getting, and only then
asks whether we have the right person.

The line that makes this safe is the one the SMS agent playbook draws: we may
acknowledge the NOISE (lots of texts about the property), never the SITUATION
(why they are getting them). "I know you're probably getting blown up about
{addr}" is fine. "I know things are tough right now" names the list. Priority 1
in ty+2 is built from Absentee + Low Income, Low Credit + Free & Clear,
Absentee + Low Credit and investor score 90+, so money, credit, debt and equity
are as off limits here as foreclosure and probate are on the courthouse lists.

Every template is checked by the SAME rules that guard the live SMS agent and
the text-touch-builder skill, imported rather than copied, so the three can
never drift apart:

    python src/smarter_contact_templates.py --check         # refuse on any problem
    python src/smarter_contact_templates.py --preview 3     # render sample owners
    python src/smarter_contact_templates.py --export        # paste-ready files
    python src/smarter_contact_templates.py --tag-no-name   # dry; --commit writes

Placeholders here are neutral ({first} {addr} {city} {sender}); `--export`
rewrites them into Smarter Contact's merge tags (SC_TAGS below). Setup steps
and the reasoning behind each limit: docs/smarter-contact-setup.md.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from sms_agent import respond  # noqa: E402  (shared _BANNED / _LIST_WORDS)

_skill = importlib.util.spec_from_file_location(
    "build_text_touches",
    ROOT / "skills" / "text-touch-builder" / "scripts" / "build_text_touches.py")
_ttb = importlib.util.module_from_spec(_skill)
_skill.loader.exec_module(_ttb)

OUT_DIR = ROOT / "output" / "smarter_contact"

# Smarter Contact merge tags, as its app substitutes them: a literal regex on
# `<Label>` from its TemplateLabel enum (First Name, Last Name, Phone, Address,
# City, State, Zip), read out of the app bundle 2026-10-20 and matching the
# account's own test template. It capitalizes name values itself ("GEORGE" ->
# "George"), which is why only multi-token and empty first names need the
# no-name pools.
SC_TAGS = {
    "first": "<First Name>",
    "addr": "<Address>",
    "city": "<City>",
}
# The sign-off is a literal first name, not a merge tag: the person signing is
# the person who answers the callback (same rule as the SMS agent).
DEFAULT_SENDER = "Adriana"

# Smarter Contact limits (support.smartercontact.com, 2026).
SC_TEMPLATE_TITLE = 35
SC_QUICK_REPLY_NAME = 25
SC_WORKFLOW_DAYS = 365
# Opt-out text is appended to every template with optOut on. The app bundle's
# DEFAULT_OPT_OUT_MESSAGE is "Reply STOP to unsubscribe." (26 chars), and the
# account's test template measured exactly 27 extra characters with its
# separator. Every touch stays inside ONE segment with it attached.
OPT_OUT_RESERVE = 27
# Whether templates carry that footer. Off at Ty's request (2026-10-21); the
# account has allowOptOutDisable. Smarter Contact and 10DLC guidance expect
# opt-out wording in the FIRST text of a conversation, so if carrier blocks
# climb, turning this back on for Touch 1 is the first lever. STOP replies are
# honoured natively either way. The reserve above stays, so every touch still
# fits one segment if the footer comes back. Workflow messages have no such
# flag (their footer is an account-wide setting) and are left alone.
SC_OPT_OUT = False
SEGMENT = 160
TOUCH_CAP = SEGMENT - OPT_OUT_RESERVE
REPLY_CAP = 2 * SEGMENT

# ---------------------------------------------------------------------------
# The touches. One job each, never combined.
#
# Touch 1 is where the empathy lives. Touches 2-4 keep the thread (the ask,
# then owning the streak) so the sequence reads like one patient person rather
# than a template that forgot what it said.
#
# Never promise what the sequence will break. "I'll only ask once" followed by
# three more texts is a lie the owner can count.
TOUCH1 = [
    "Hi {first}, I know you're probably getting blown up about {addr}, so I'll be quick. Still the owner? {sender}",
    "Hey {first}, I'm sure your phone's been going off about {addr}. Sorry to add one more! Is it yours? {sender}",
    "Hi {first}, I bet you're getting a ton of texts about {addr}. Quick one, do I have the right person? {sender}",
    "Hey {first}, it's {sender}. You've probably heard from a lot of folks about {addr}. Am I texting the owner?",
    "Hi {first}, sorry for one more text about {addr}, I'm sure you're getting plenty. Is it still yours? {sender}",
    "Hey {first}, it's {sender}. Your phone's probably blowing up about {addr}, sorry! Do I have the right person?",
    "Hi {first}, I'll be brief since you're likely hearing from a lot of folks about {addr}. Still yours? {sender}",
    "Hey {first}, I know folks keep texting you about {addr}, so I'll be quick. Are you still the owner? {sender}",
    # The neutral flavor: acknowledges the noise without saying it is ABOUT the
    # property, which is the version least likely to read as "you're on a list".
    "Hi {first}, I know random texts are annoying, so I'll be quick. Are you still the owner of {addr}? {sender}",
    "Hey {first}, sorry for the random text, I know you get plenty. Is {addr} still yours? {sender}",
]
# The control arm: the consensus ownership check, no empathy line. Nobody
# publishes reply rates for the "getting blown up" opener (searched 2026-10-20),
# so it ships as a split test against this rather than as an assumption.
TOUCH1_CONTROL = [
    "Hi {first}, is this the right number for the owner of {addr}? {sender}",
    "Hey {first}, do you still own {addr}? Hoping I have the right person. {sender}",
    "Hi {first}, it's {sender}. Quick question, are you the owner of {addr}?",
    "Hey {first}, I hope your week's going well! Is {addr} yours? {sender}",
]
TOUCH1_CONTROL_NONAME = [
    "Hi, is this the right number for the owner of {addr}? {sender}",
    "Hey there, it's {sender}. Quick question, are you the owner of {addr}?",
]
TOUCH1_NONAME = [
    "Hi there, I'm sure the owner of {addr} gets a lot of texts, so sorry for one more. Is this them? {sender}",
    "Hey, it's {sender}. Whoever owns {addr} is probably getting blown up, so I'll be quick. Is that you?",
    "Hi, sorry for one more text about {addr}, I'm sure there have been plenty. Is this the owner? {sender}",
    # Smarter Contact requires one opener per 50 contacts in a campaign (capped
    # at 8); the live no-name group is 158 contacts, so it needs at least 4.
    "Hey there, it's {sender}. I bet the owner of {addr} hears from a lot of people. Is that you?",
    "Hi, I'll keep this quick since I'm sure {addr} gets plenty of texts. Is this the owner? {sender}",
    "Hi there, sorry to add to the pile about {addr}. Do I have the owner's number? {sender}",
]
# Touch 2 is the ask (Ty, 2026-10-20): a second "is it still yours?" the next
# day is the weakest text in the run, so day 1 gets to the point. Ownership
# stays conditional ("if it's yours") because many of these numbers belong to
# someone else, and asking a stranger whether they'd sell costs nothing.
TOUCH2 = [
    "Hi {first}, {sender} again. If {addr} is yours, would you ever think about selling it? No pressure at all.",
    "Hey {first}, following up on {addr}. If it's yours, is selling something you'd ever consider? {sender}",
    "Hi {first}, mine probably got buried, so I'll just ask. Would you ever sell {addr}? {sender}",
    "Hey {first}, me again! Totally fine if not, but have you ever thought about selling {addr}? {sender}",
    # Follow-up campaigns of a ~1,000-contact wave need 8 templates.
    "Hey {first}, quick question about {addr}. If it's yours, would you ever sell it? {sender}",
    "Hi {first}, {sender} here. Just curious, have you thought about selling {addr} at all?",
    "Hey {first}, hope your day's going well! If {addr} is yours, any chance you'd sell? {sender}",
    "Hi {first}, I'll just ask straight out. Would you ever think about selling {addr}? {sender}",
]
TOUCH2_NONAME = [
    "Hi, {sender} again. If you own {addr}, would you ever think about selling it? No pressure at all.",
    "Hey there, following up on {addr}. If it's yours, is selling something you'd consider? {sender}",
    "Hi, {sender} again. If you own {addr}, is selling ever on the table?",
    "Hey there, quick question about {addr}. If it's yours, would you ever sell it? {sender}",
]
# Touch 3 is the pitch (Ty, 2026-10-21: the "I'll leave you be" easy out read
# oddly). By day 2 they have had the empathy opener and the ask, so this one
# gives a reason to answer: what selling to a local buyer actually looks like.
# Only claims true of every deal (as-is, no repairs, no showings, no agent
# fees). Never a price, never a timeline promise, never "offer" or "free"
# (Smarter Contact blocks both).
TOUCH3 = [
    "Hi {first}, here's my simple pitch on {addr}: as-is, no repairs, no agent fees. Worth a quick call? {sender}",
    "Hey {first}, I buy as-is, so {addr} wouldn't need repairs, showings or agent fees. Open to a call? {sender}",
    "Hi {first}, I'm a local buyer. {addr} could sell as-is, no repairs or agent fees. Worth a quick call? {sender}",
    "Hey {first}, I work around your timing on {addr}, no repairs or showings needed. Worth a quick call? {sender}",
    "Hi {first}, {sender} here. If you sold {addr} to me, you'd skip repairs and agent fees. Worth a quick call?",
    "Hey {first}, selling {addr} as-is means no repairs and no showings. Open to a quick call? {sender}",
    "Hi {first}, I keep it simple on {addr}: you sell as-is, no agent fees. Worth a quick call? {sender}",
    "Hey {first}, {sender} here. With me, {addr} could sell as-is, nothing to fix first. Open to a quick call?",
]
TOUCH3_NONAME = [
    "Hey there, here's my simple pitch on {addr}: as-is, no repairs, no agent fees. Worth a quick call? {sender}",
    "Hi, {sender} here. I'm a local buyer, so {addr} could sell as-is with no repairs or agent fees. Worth a quick call?",
    "Hi, {sender} here. {addr} could sell as-is, no repairs, no showings. Worth a quick call?",
    "Hey there, simple pitch on {addr}: sell as-is, no agent fees. Open to a quick call? {sender}",
]
# Touch 5 follows weeks later, so the breakup says "for a while", never "last
# one ever" or "I promise". (Not "for now" either: Smarter Contact's template
# filter blocks the word "now".)
TOUCH4 = [
    "Hi {first}, I'll get out of your inbox for a while. Did you decide to keep {addr}? All the best! {sender}",
    "Hey {first}, last one from me for a while. If {addr} is ever for sale, can I be your first call? {sender}",
    "Hi {first}, {sender} again. If this is the wrong number, sorry! If not, should I close my file on {addr}?",
    "Hey {first}, I'll give you some space after this. Safe to say you're keeping {addr} this year? {sender}",
]
TOUCH4_NONAME = [
    "Hi, {sender} again about {addr}. If there's a better person to talk to, could you point me their way?",
    "Hey there, last text from me for a while about {addr}. Should I close my file on it? {sender}",
]
# Touch 5 is the long tail, months later, for owners who never answered. It is
# the only touch that lands after "last one from me", so it never pretends to
# be a continuation of that promise: it is a fresh check-in.
TOUCH5 = [
    "Hi {first}, it's {sender}. It's been a while since I texted about {addr}. Anything changed?",
    "Hey {first}, {sender} checking back in on {addr}. Would you think about selling it this year?",
    "Hi {first}, hope the last couple months have been good to you! Still holding onto {addr}? {sender}",
]
TOUCH5_NONAME = [
    "Hi, {sender} checking back in about {addr}. It's been a while. Is it still yours?",
    "Hey there, it's been a little while since I texted about {addr}. Anything changed with it? {sender}",
]
# (job, named pool, no-name pool, send day). Ty, 2026-10-20: open with
# consecutive days, not a spread-out drip. Three in a row is Smarter Contact's
# ceiling: a contact gets at most 3 texts per 7 days, then a 14-day cooldown,
# and a drip will not send message #3 before day 3 (silently raised). So touches
# 2 and 3 go out as FOLLOW-UP campaigns to non-responders, which are exempt from
# the drip floors but not from the cap. Day 21 clears the cooldown whether it
# starts at the third text or at the end of the 7-day window (support pages do
# not say which).
TOUCHES = {
    1: ("Empathy identity check", TOUCH1, TOUCH1_NONAME, 0),
    2: ("Would you ever sell?", TOUCH2, TOUCH2_NONAME, 1),
    3: ("The pitch", TOUCH3, TOUCH3_NONAME, 2),
    4: ("Breakup", TOUCH4, TOUCH4_NONAME, 21),
    5: ("Long-tail check-in", TOUCH5, TOUCH5_NONAME, 60),
}
SC_CAP, SC_CAP_WINDOW, SC_COOLDOWN = 3, 7, 14
SEND_DAYS = [day for *_, day in TOUCHES.values()]


# ---------------------------------------------------------------------------
# Land (Ty, 2026-11-10): the builder-fit lots and entitlement tracts from
# src/land_market.py. Same touch shape, same send days and the same gates as
# Priority 1, with three deliberate differences:
#   * The parcel is named by its parcel number through Smarter Contact's <APN>
#     merge tag. 405 of the 1,119 Knox pilot lots carry no house number, and
#     "the lot on Rutledge Pike" is not something an owner recognizes; the
#     number on their tax bill is.
#   * "vacant" is allowed. On a land list it names the asset. Priority 1 bans it
#     because that tier is built partly from a Vacant segment, where the word
#     tells the owner which list they are on.
#   * Each lane gets its true pitch. Lots: we are looking to buy the lot, as-is,
#     no listing, no agent fees (Ty, 2026-11-10: never claim we work with
#     builders, even though the lot desk only markets builder-fit lots). Tracts: as-is, the county and survey work at our cost,
#     a closing date the owner picks (the entitle-and-sell model). Tracts never
#     say "vacant", because some carry a farmhouse.
# The APN, acres and county fields only hold the right values after
# src/smarter_contact_land.py patches the pushed contacts: DataSift's
# integration writes its RECORD UUID into APN and leaves acres and county empty.
LAND_TAGS = {"apn": "<APN>", "acres": "<Parcel acres>", "county": "<Parcel county>"}
# Worst case from the mailable land (8,157 rows, 2026-11-09), at the 99th
# percentile like Priority 1's: addresses 25 characters, parcel numbers 13. The
# top 1% (to 33 and 17) bills as two segments rather than shortening every text.
LAND_WORST = {"first": "Christopher", "addr": "1234 Example Meadows Pike", "city": "Knoxville",
              "sender": "Adriana", "apn": "000X-X-000.00", "acres": "123.45", "county": "Jefferson"}
_LAND_WORDS = re.compile(
    r"\b(credit|income|debt|owe\w*|equity|paid off|free and clear|absentee|out of state|"
    r"inherit\w*|landlocked|flood\w*|steep|situation|going through|hard time|struggl\w*|"
    r"cash offer|we buy (land|houses)|any condition)\b", re.I)

LAND_LOT = {
    # Touch 1 and touch 2 #1-2 are already live in Smarter Contact (accepted 2026-11-10); change them only
    # together with an update there.
    1: ([
        "Hi {first}, quick question. Do you still own the vacant lot at {addr}, parcel {apn}? {sender}",
        "Hey {first}, it's {sender}. Is the vacant lot at {addr} (parcel {apn}) still yours?",
        "Hi {first}, I have you down as the owner of the lot at {addr}, parcel {apn}. Is that right? {sender}",
        "Hey {first}, sorry for the random text. Are you the owner of the lot at {addr} (parcel {apn})? {sender}",
        "Hi {first}, it's {sender}. Do I have the right person for the vacant lot at {addr}, parcel {apn}?",
        "Hey {first}, hope your week's going well! Is the lot at {addr} (parcel {apn}) yours? {sender}",
        "Hi {first}, I'll be quick. Are you still the owner of the lot at {addr}, parcel {apn}? {sender}",
        "Hey {first}, it's {sender}. Quick one about parcel {apn}, the vacant lot at {addr}. Is it still yours?",
    ], [
        "Hi, is this the owner of the vacant lot at {addr}, parcel {apn}? {sender}",
        "Hey there, it's {sender}. Do I have the right number for the owner of parcel {apn} at {addr}?",
        "Hi, sorry for the random text. Is this the owner of the lot at {addr} (parcel {apn})? {sender}",
        "Hey there, quick question. Are you the owner of the vacant lot at {addr}, parcel {apn}? {sender}",
    ]),
    2: ([
        "Hi {first}, {sender} again. If the lot at {addr} is yours, would you ever think about selling it?",
        "Hey {first}, following up on parcel {apn}. If it's yours, is selling the lot something you'd consider? {sender}",
        "Hi {first}, back with one question about the vacant lot at {addr}. Is it something you'd part with? {sender}",
        "Hey {first}, is the lot at {addr} one you plan to keep, or would you consider letting it go? {sender}",
        "Hi {first}, {sender} here. Does selling parcel {apn} make any sense for you this year?",
        "Hey {first}, no pressure here. Would you sell the vacant lot at {addr} for the right number? {sender}",
        "Hi {first}, I'm still interested in the lot at {addr}. Any chance it's for sale? {sender}",
        "Hey {first}, checking in once more on parcel {apn}. Open to selling that lot? {sender}",
    ], [
        "Hi, back with one question about the lot at {addr}. Would the owner ever part with it? {sender}",
        "Hey there, {sender} here. Is parcel {apn} something you'd sell this year?",
        "Hi, still interested in the vacant lot at {addr}. Any chance it's for sale? {sender}",
        "Hey there, circling back on parcel {apn}. Would you consider selling that lot? {sender}",
    ]),
    3: ([
        "Hi {first}, I'm looking to buy lots around {addr}. Would you be open to a quick call? {sender}",
        "Hey {first}, I'd like to buy the lot at {addr} as-is, no agent fees. Open to a quick call? {sender}",
        "Hi {first}, I'm a local buyer looking for land like your lot at {addr}. Worth a quick call? {sender}",
        "Hey {first}, simple pitch on the lot at {addr}: no listing, no agent fees, your timing. Worth a call? {sender}",
        "Hi {first}, {sender} here. I'm buying vacant lots near you, and yours at {addr} caught my eye. Open to a call?",
        "Hey {first}, you could sell parcel {apn} without listing it or paying an agent. Want to talk it through? {sender}",
        "Hi {first}, I keep it simple on lots like {addr}: no fees, I handle the paperwork. Worth a quick call? {sender}",
        "Hey {first}, {sender} here. I close on your schedule and do the paperwork on the lot at {addr}. Can we talk?",
    ], [
        "Hey there, local buyer here, hoping to purchase the vacant lot at {addr}. Could we talk by phone? {sender}",
        "Hi, {sender} here. I'd like to buy the lot at {addr} as-is, no listing needed. Worth a quick call?",
        "Hi, {sender} here. Selling the lot at {addr} to me means no agent fees. Open to a quick call?",
        "Hey there, if you'd sell parcel {apn}, I can skip the listing and the agent fees. Want to talk? {sender}",
    ]),
    4: ([
        "Hi {first}, I'll stop texting about the lot at {addr} for a while. If you ever want to sell, I'm here. {sender}",
        "Hey {first}, last one from me for a while. If parcel {apn} ever goes up for sale, can I get first look? {sender}",
        "Hi {first}, {sender} here. I'll leave you be for a while. Keeping the lot at {addr} is fine by me!",
        "Hey {first}, I'll give you some room after this. Sounds like you're holding the lot at {addr}? {sender}",
    ], [
        "Hi, {sender} again about the vacant lot at {addr}. If someone else owns it, could you point me their way?",
        "Hey there, giving parcel {apn} a rest for a while. Should I close my file on the lot? {sender}",
    ]),
    5: ([
        "Hi {first}, it's {sender}. Been a while since I asked about the vacant lot at {addr}. Any change?",
        "Hey {first}, {sender} checking in on parcel {apn} again. Would you sell the lot this year?",
        "Hi {first}, hope things are good! Do you still own the lot at {addr}? {sender}",
    ], [
        "Hi, {sender} here, checking back on the lot at {addr}. Still in your name?",
        "Hey there, a few months since I texted about parcel {apn}. Has anything changed with the lot? {sender}",
    ]),
}
LAND_TRACT = {
    1: ([
        "Hi {first}, do you still own the {acres} acres at {addr} in {county} County, parcel {apn}? {sender}",
        "Hey {first}, {sender} here. Are the {acres} acres on parcel {apn} in {county} County still in your name?",
        "Hi {first}, trying to reach the owner of parcel {apn}, {acres} acres at {addr}. Is that you? {sender}",
        "Hey {first}, the county has you down for {acres} acres at {addr}. Do you still own it? {sender}",
        "Hi {first}, it's {sender}. Is {addr} in {county} County, parcel {apn}, still your land?",
        "Hey {first}, hoping I have the right number for the owner of parcel {apn} ({acres} acres). Is that you? {sender}",
        "Hi {first}, are you still the owner of the acreage at {addr}, parcel {apn}? {sender}",
        "Hey {first}, it's {sender}. Do you still hold the {acres} acre tract at {addr}?",
    ], [
        "Hi there, I'm trying to reach the owner of parcel {apn} in {county} County. Is that you? {sender}",
        "Hey, it's {sender}. Do I have the right number for whoever owns the {acres} acres at {addr}?",
        "Hi, sorry to text out of the blue. Do you own the land at {addr} (parcel {apn})? {sender}",
        "Hey there, quick one. Does the {acres} acre tract at {addr} still belong to you? {sender}",
    ]),
    2: ([
        "Hi {first}, {sender} again. Would you ever sell some or all of the {acres} acres at {addr}?",
        "Hey {first}, one more question about parcel {apn}. Is any of that land for sale, even part of it? {sender}",
        "Hi {first}, do you see yourself keeping parcel {apn} long term, or would you sell? {sender}",
        "Hey {first}, {sender} here. Would selling the acreage at {addr} be worth a conversation?",
        "Hi {first}, still hoping to talk about the land at {addr}. Would you consider selling part of it? {sender}",
        "Hey {first}, circling back on parcel {apn} in {county} County. Open to selling? {sender}",
        "Hi {first}, no pressure at all. Would you sell parcel {apn} if the terms worked for you? {sender}",
        "Hey {first}, I'd still like to buy the land at {addr}. Is it for sale at all? {sender}",
    ], [
        "Hi, {sender} again. Would the owner of the {acres} acres at {addr} sell some or all of it?",
        "Hey there, one more question about parcel {apn}. Is any of that land for sale? {sender}",
        "Hi, circling back on the acreage at {addr} in {county} County. Open to selling? {sender}",
        "Hey there, still hoping to buy the land at {addr}. Would you consider it? {sender}",
    ]),
    3: ([
        "Hi {first}, I buy land as-is and do the survey and county work at my cost. Worth a call about {addr}? {sender}",
        "Hey {first}, the land at {addr} can sell to me as-is, no agent fees, your date. Worth a call? {sender}",
        "Hi {first}, I'm a local buyer. I pay for the survey and zoning work on land like {addr}. Worth a call? {sender}",
        "Hey {first}, on parcel {apn}: as-is, no commission, and you set the closing. Open to a call? {sender}",
        "Hi {first}, {sender} here. I work around your timing on land like {addr}, even a longer close. Worth a call?",
        "Hey {first}, there's no listing or agent commission if you sell parcel {apn} to me. Could we talk? {sender}",
        "Hi {first}, I keep it simple on {addr}: as-is, I handle the county work, your date. Worth a call? {sender}",
        "Hey {first}, {sender} here. Sell the land at {addr} to me and skip agent fees and survey costs. Worth a call?",
    ], [
        "Hey there, I buy acreage as-is and cover the survey and county work. Could we talk about {addr}? {sender}",
        "Hi, {sender} here. The land at {addr} could sell as-is, no agent fees, on your timing. Worth a quick call?",
        "Hi, {sender} here. You'd skip the listing and the commission selling parcel {apn} to me. Open to a call?",
        "Hey there, on the {acres} acres at {addr}, I can close as-is when it suits you. Can we talk? {sender}",
    ]),
    4: ([
        "Hi {first}, I'll back off on parcel {apn} for a while. If selling ever makes sense, text me. {sender}",
        "Hey {first}, taking a break from texting about parcel {apn}. Mind if I check back next season? {sender}",
        "Hi {first}, {sender} here. If this number's wrong, my apologies. If not, keeping the land at {addr}?",
        "Hey {first}, giving you some space after this one. Safe to say the acreage at {addr} isn't for sale? {sender}",
    ], [
        "Hi, {sender} again. If someone else handles the land at {addr}, could you point me their way?",
        "Hey there, stepping back from parcel {apn} for a while. Should I close my file on the acreage? {sender}",
    ]),
    5: ([
        "Hi {first}, it's {sender}, checking back on the {acres} acres at {addr}. Anything new with it?",
        "Hey {first}, {sender} again after a while. Would you sell parcel {apn} next year?",
        "Hi {first}, hope the season's treating you well! Still holding the acreage at {addr}? {sender}",
    ], [
        "Hi, {sender} checking back on the land at {addr} after a few months. Is it still yours?",
        "Hey there, it's been a while since I asked about parcel {apn}. Any plans for that land? {sender}",
    ]),
}
# Every touch is lane-specific: Smarter Contact refuses a template too close to one already in the account,
# so a shared touch 4 and 5 would be created once and refused the second time.
LAND_LATE = {}

LAND_LANES = {"lot": ("Lot", LAND_LOT), "tract": ("Tract", LAND_TRACT)}


def land_arms():
    """(lane, touch number, named pool, no-name pool) for every land touch set."""
    for lane, (_, early) in LAND_LANES.items():
        for n in sorted({**early, **LAND_LATE}):
            named, noname = early.get(n) or LAND_LATE[n]
            yield lane, n, named, noname


def land_title(lane: str, n: int, aud: str, i: int) -> str:
    t = f"Land {LAND_LANES[lane][0]} T{n} {'No Name' if aud == 'noname' else 'Named'} {i:02d}"
    assert len(t) <= SC_TEMPLATE_TITLE, t
    return t


SC_SIMILAR = 0.90   # Smarter Contact refused a template at 0.95 similarity to one in the account, accepted 0.88


def _similar(a: str, b: str) -> float:
    import difflib
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def land_audit() -> list[str]:
    found, seen = [], {}
    p1 = [to_sc(t, DEFAULT_SENDER) for _, _, nm, nn in _arms() for t in nm + nn]
    land = [(f"land {lane} T{n} {aud}[{i}]", to_sc(t, DEFAULT_SENDER))
            for lane, n, nm, nn in land_arms() for aud, pool in (("named", nm), ("noname", nn))
            for i, t in enumerate(pool)]
    for k, (where, text) in enumerate(land):
        close = max([_similar(text, x) for x in p1] + [_similar(text, x) for j, (_, x) in enumerate(land) if j != k])
        if close >= SC_SIMILAR:
            found.append(f"{where}: {close:.2f} similar to another template (Smarter Contact refuses near-duplicates)")
    for lane, n, named, noname in land_arms():
        for label, pool in (("named", named), ("noname", noname)):
            for i, t in enumerate(pool):
                where = f"land {lane} T{n} {label}[{i}]"
                if t in seen and seen[t] != (lane, n, label) and n <= 3:
                    found.append(f"{where}: duplicate of {seen[t]}")
                seen.setdefault(t, (lane, n, label))
                if label == "noname" and "{first}" in t:
                    found.append(f"{where}: uses {{first}}")
                if lane == "tract" and re.search(r"\bvacant\b", t, re.I):
                    found.append(f"{where}: says vacant, and some tracts carry a house")
                found += [f"{where}: {p}" for p in problems(t, kind="touch", key=str(n), profile="land")]
        if n <= 3 and len(named) < 8:
            found.append(f"land {lane} T{n}: needs 8+ named variations (one per 50 contacts)")
        if n <= 3 and len(noname) < 4:
            found.append(f"land {lane} T{n}: needs 4+ no-name variations")
    return found

# ---------------------------------------------------------------------------
# Labels, matched to the account's own scheme (read live 2026-10-20) so nothing
# is duplicated. Lead, Not interested and Wrong Number are Smarter Contact system
# labels; Nurture, Correct DNC and Unknown DNC already existed; the rest are
# created. Status values are the DataSift integration's own option values
# (property statuses lowercase, phone statuses uppercase, "DNC" = Unknown Owner
# DNC). Lead matters most: a ty+2 sequence on New Lead assigns the record to
# whoever owns new leads, so a hot reply reaches a caller with no extra wiring.
# The account's existing Do Not Call -> WRONG_DNC row is left exactly as set.
#   (label, use it when, property status, phone status, also a phone tag)
LABELS = [
    ("Lead", "open to selling, asked for a call, named a price, or said maybe",
     "new_lead", "", False),
    ("Nurture", "asked to talk later or gave a timeframe", "follow_up", "", False),
    ("Not interested", "confirmed owner, not selling", "not_interested", "", False),
    ("Property Listed", "listed with an agent", "listed", "", False),
    ("Property Sold", "already sold it", "sold", "", False),
    ("Owner Deceased", "family member says the owner passed", "", "", True),
    ("Wrong Number", "not the owner's number", "", "WRONG", False),
    ("Tenant", "a renter answered", "", "WRONG", True),
    ("Correct DNC", "the confirmed owner asked us to stop", "", "CORRECT_DNC", False),
    ("Unknown DNC", "STOP or hostile, ownership never confirmed", "", "DNC", False),
    ("Spanish Speaking", "replied in Spanish", "", "", True),
]

# Inbox quick replies. Keyed by what the OWNER said, each naming the label to
# set so the reply and the CRM state move together. Copy follows the SMS agent
# playbook (src/sms_agent/knowledge/playbook.md): never a price, never an
# offer, never argue, get them on the phone.
#
# A reply that may come from someone OTHER than the owner (tenant, family of a
# deceased owner) never uses {first}: Smarter Contact's first name is the
# owner's, so "Hi Mary" to Mary's grieving son greets the person who died.
#
# STOP and hostility deliberately have NO quick reply. They get no answer at
# all, only the DNC label.
#   (key, quick reply name <= 25 chars, when the owner says, label, body)
QUICK_REPLIES = [
    ("yes_owner", "Yes it's mine", "Yes, it's mine", "Lead",
     "Thanks for getting back to me, {first}! Would you ever consider selling it if the offer made sense? Happy to hop on a quick call."),
    ("who_is_this", "Who is this", "Who is this?", "",
     "Sorry, I should have led with that! I'm {sender}, a local buyer here in {city}. {addr} caught my eye. Have you ever thought about selling it?"),
    ("how_number", "How'd you get my number", "How did you get my number?", "",
     "Fair question. I research property records for homes I'm interested in. Totally fine if it's a bad time. Is {addr} yours?"),
    ("how_much", "How much", "How much will you pay? / Make me an offer", "Lead",
     "Totally fair question. I'd hate to throw out a number without knowing the house. Do you have 5 minutes for a quick call today or tomorrow?"),
    ("gave_price", "They named a price", "They named a price", "Lead",
     "Thanks for sharing that, it helps. Let me get you on the phone so I understand the house. What time works best for a quick call?"),
    ("call_me", "Call me", "Call me", "Lead",
     "Perfect, I'll give you a call shortly. Is now good, or is later today better?"),
    ("maybe", "Maybe depends on price", "Maybe / depends on the price", "Lead",
     "That's fair, it always depends on the number. A quick call is the easiest way to see if we're even close. When's a good time?"),
    ("later", "Check back later", "Not now, maybe later", "Nurture",
     "No problem at all. When would be a better time for me to check back in, a month or two?"),
    ("not_selling", "Not selling", "Not selling", "Not interested",
     "Totally understand. Just so I don't bug you again, is it that you'd never sell, or just not right now?"),
    ("not_interested_soft", "Not interested polite", "Not interested (polite)", "Not interested",
     "No problem at all, thanks for letting me know. If anything changes down the road, would it be okay if I checked back in a few months?"),
    ("listed", "Listed with agent", "It's listed with an agent", "Property Listed",
     "Thanks for letting me know! I'll leave you and your agent to it. If it doesn't sell, would you mind if I checked back in a couple months?"),
    ("already_sold", "Already sold", "Already sold it", "Property Sold",
     "Thanks for letting me know, I'll update my notes. Do you happen to own any other houses you'd think about selling?"),
    ("tenant", "Tenant answered", "I rent here / I'm the tenant", "Tenant",
     "Thanks for letting me know, and sorry to bother you! Do you happen to know the best way to get in touch with the owner?"),
    ("owner_passed", "Owner passed away", "The owner passed away", "Owner Deceased",
     "I'm so sorry for your loss, and I'm sorry to bother you. Are you the one looking after the house now, or is there someone better to talk to?"),
    ("wrong_number", "Wrong number", "Wrong number", "Wrong Number",
     "So sorry about that, I'll take your number off my list. Have a great day!"),
    ("spanish", "Spanish", "Replies in Spanish", "Spanish Speaking",
     "Gracias por responder! Prefiere que le llame alguien que hable espanol? Es sobre {addr}."),
    ("real_person", "Real person", "Is this a real person / a bot?", "",
     "Real person here, it's {sender}! Happy to hop on a quick call if that's easier."),
]
_NOT_OWNER_REPLIES = {"tenant", "owner_passed", "wrong_number"}

# Contact Workflows (Elite). Added by hand from a conversation, one body per
# step, and any reply pulls the contact out, so these only ever reach people
# who went quiet after an outcome. Step day = days after the contact is added.
#   name: (start label, no_first, [(day, body), ...])
WORKFLOWS = {
    "Soft No Check-Back": ("Not interested", False, [
        (45, "Hi {first}, it's {sender}. We texted a while back about {addr}. Just checking in, has anything changed?"),
        (120, "Hey {first}, hope you're well! Still holding onto {addr}, or would you think about selling this year? {sender}"),
        (240, "Hi {first}, {sender} here. Still interested in {addr} whenever the timing's right. Would a quick call work?"),
    ]),
    "Listed Check-Back": ("Property Listed", False, [
        (45, "Hi {first}, {sender} here. Is {addr} still on the market? If it stalls, I'd be glad to be your backup."),
        (90, "Hey {first}, checking back on {addr}. Did it end up selling? If not, I'm still interested. {sender}"),
    ]),
    "Asked For Later": ("Nurture", False, [
        (30, "Hi {first}, it's {sender}. You mentioned checking back about {addr}, so here I am! Is this a better time?"),
        (60, "Hey {first}, {sender} again about {addr}. Would a quick call this week work, or should I check back later?"),
    ]),
    # The person in this thread is family, never the owner: no {first}.
    "Family After A Loss": ("Owner Deceased", True, [
        (30, "Hi, it's {sender}. I hope you and your family are doing okay. Whenever you're ready to talk about {addr}, I'm here."),
        (90, "Hi, {sender} checking in. Has the family decided what to do with {addr} yet? No rush at all."),
    ]),
}

# Replies that get "label DNC now", no answer. (Smarter Contact's own "Response
# removal" turned out to be a list of phone numbers, not keywords, as of
# 2026-10-20, so this is the inbox's trigger list, not an account setting.)
# "sue" is left out on purpose (a seller named Sue signs her texts), and plain
# STOP needs no entry because Smarter Contact handles it natively.
#
# Since April 2025 the FCC requires honoring seven words (stop, quit, revoke,
# opt out, cancel, unsubscribe, end). "quit" and "end" are NOT here because a
# substring match on "end" suppresses everyone who mentions a weekend; add them
# only once the setup confirms Smarter Contact matches whole words.
RESPONSE_REMOVAL = [
    "stop texting", "remove me", "take me off", "unsubscribe", "opt out",
    "revoke", "cancel", "do not text", "dont text", "don't text", "attorney",
    "lawyer", "lawsuit", "harass", "harassing", "harassment", "tcpa",
    "cease and desist", "report you",
]

# Words that get a FIRST text filtered or read as a pitch (BatchLeads' list, plus
# REsimpli's spam words). Touch 1 asks who owns the house and nothing else.
_FIRST_TEXT_WORDS = re.compile(
    r"\b(interested|sell\w*|offer\w*|property|cash|investor|purchase|buy\w*|"
    r"looking|mortgage|loan|price|deal|investment)\b", re.I)

# Words Priority 1 makes radioactive on top of the shared list vocabulary. The
# segments are built from credit and income data, so naming money in any form
# confirms we know something about their finances.
_P1_WORDS = re.compile(
    r"\b(credit|income|debt|owe\w*|equity|paid off|free and clear|absentee|"
    r"vacant|landlord|tenants?|situation|going through|hard time|struggl\w*|"
    r"cash offer|we buy houses|any condition)\b", re.I)
# Smarter Contact's own template filter, read as text from its app bundle
# (templatesBannedWords, 2026-10-20). A template containing one of these is
# refused at create time ("Please remove "now" to proceed"), so it is checked
# here first. Brackets in the source mark optional suffixes ("offer[s]").
# Quick replies are not filtered (typed in a live thread), so this applies to
# touches and workflow steps only.
_SC_BANNED = re.compile(
    r"\b(irs|abuser?s?|harass(es)?|harassments?|harassings?|gangs?|now|free|"
    r"white-?lists?|loans?|lenders?|trump|putin|maga|biden|kamala|tim walz|jd vance|"
    r"jews?|jewish|offers?|winners?|limited access|911|grass|smoke|crack|weed|church)\b",
    re.I)
# A promise the sequence breaks. Touch 5 exists, so nothing may claim finality.
_BROKEN_PROMISE = re.compile(
    r"\b(I promise|never (text|bother)|last (one|text) (ever|from me)(?! for)|"
    r"one last time|stop texting after)\b", re.I)

# GSM-7 basic set. One character outside it (a curly apostrophe pasted from a
# doc is the usual culprit) re-encodes the WHOLE message as UCS-2, which drops a
# segment from 160 characters to 70 and roughly doubles the bill.
_GSM7 = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
            "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")

# Worst case a real Priority 1 record produces, so length checks are honest.
# Measured on ty+2 Priority 1 (2026-10-20): first name + street runs 22 chars
# at the median and 34 at p99; this pair is 35.
WORST = {"first": "Christopher", "addr": "12345 Old Maryville Pike",
         "city": "Knoxville", "sender": "Adriana"}
PLACEHOLDER_RX = re.compile(r"\{(\w+)\}")


def problems(text: str, *, kind: str, key: str = "", profile: str = "p1") -> list[str]:
    """Every reason this template must not ship. profile "land" swaps Priority 1's sensitive words for the
    land list's (so "vacant" is allowed) and checks length against land's worst-case address and parcel."""
    out = list(_ttb.human_voice_problems(text))
    if respond._BANNED.search(text):
        out.append(f"banned phrase: {respond._BANNED.search(text).group(0)}")
    if respond._LIST_WORDS.search(text):
        out.append(f"names the list: {respond._LIST_WORDS.search(text).group(0)}")
    sensitive, worst = (_LAND_WORDS, LAND_WORST) if profile == "land" else (_P1_WORDS, WORST)
    for m in sensitive.finditer(text):
        out.append(f"{'land' if profile == 'land' else 'Priority 1'} sensitive word: {m.group(0)}")
    unknown = set(PLACEHOLDER_RX.findall(text)) - set(worst)
    if unknown:
        out.append(f"unknown placeholder {sorted(unknown)}")
        return out
    rendered = text.format(**worst)
    bad = sorted({c for c in rendered if c not in _GSM7})
    if bad:
        out.append(f"non-GSM-7 characters {bad} (message bills as UCS-2)")
    cap = REPLY_CAP if kind == "reply" else TOUCH_CAP
    if len(rendered) > cap:
        out.append(f"{len(rendered)} chars at the worst case (cap {cap})")
    if kind != "reply" and _SC_BANNED.search(text):
        out.append(f"Smarter Contact blocks the word: {_SC_BANNED.search(text).group(0)}")
    if kind == "touch" and _BROKEN_PROMISE.search(text):
        out.append(f"promises an ending the sequence breaks: {_BROKEN_PROMISE.search(text).group(0)}")
    if kind == "touch" and key == "1" and _FIRST_TEXT_WORDS.search(text):
        out.append(f"pitch word in a first text: {_FIRST_TEXT_WORDS.search(text).group(0)}")
    q = text.count("?")
    if kind == "touch" and key in ("1", "2", "3") and q != 1:
        out.append(f"{q} questions (touches 1-3 end on exactly one easy question)")
    if q > 1:
        out.append(f"{q} questions (one per message)")
    return out


def _arms():
    """(touch number, arm name, named pool, no-name pool) for every touch set."""
    for n, (_, named, noname, _) in TOUCHES.items():
        yield n, "", named, noname
    yield 1, "control", TOUCH1_CONTROL, TOUCH1_CONTROL_NONAME


def audit() -> list[str]:
    found, seen = [], set()
    for n, arm, named, noname in _arms():
        for label, pool in ((f"{arm} named".strip(), named), (f"{arm} noname".strip(), noname)):
            for i, t in enumerate(pool):
                if t in seen:
                    found.append(f"T{n} {label}[{i}]: duplicate variant")
                seen.add(t)
                if label.endswith("noname") and "{first}" in t:
                    found.append(f"T{n} noname[{i}]: uses {{first}}")
                found += [f"T{n} {label}[{i}]: {p}" for p in problems(t, kind="touch", key=str(n))]
    labels = {lab for lab, *_ in LABELS}
    for key, name, _, label, body in QUICK_REPLIES:
        if len(name) > SC_QUICK_REPLY_NAME:
            found.append(f"reply {key}: name over {SC_QUICK_REPLY_NAME} chars")
        if label and label not in labels:
            found.append(f"reply {key}: label {label!r} is not in LABELS")
        if key in _NOT_OWNER_REPLIES and "{first}" in body:
            found.append(f"reply {key}: greets the owner by name, but the owner did not write it")
        found += [f"reply {key}: {p}" for p in problems(body, kind="reply", key=key)]
    for name, (start, no_first, steps) in WORKFLOWS.items():
        if start not in labels:
            found.append(f"workflow {name}: start label {start!r} is not in LABELS")
        if [d for d, _ in steps] != sorted(d for d, _ in steps) or steps[-1][0] > SC_WORKFLOW_DAYS:
            found.append(f"workflow {name}: step days out of order or past {SC_WORKFLOW_DAYS}")
        for d, body in steps:
            if no_first and "{first}" in body:
                found.append(f"workflow {name} day {d}: uses {{first}} in a thread the owner is not in")
            found += [f"workflow {name} day {d}: {p}" for p in problems(body, kind="workflow")]
    if len(TOUCH1) < 8:
        found.append("Touch 1 needs 8+ variations (Smarter Contact's recommended floor)")
    # Smarter Contact's cap, read the strict way: never more than 3 texts in a
    # 7-day window, and once 3 land in one, nothing until the window ends PLUS
    # the cooldown. A text scheduled into the cap is not sent, it just vanishes.
    for i, start in enumerate(SEND_DAYS):
        window = [d for d in SEND_DAYS[i:] if d < start + SC_CAP_WINDOW]
        if len(window) > SC_CAP:
            found.append(f"send days {SEND_DAYS}: {len(window)} texts within {SC_CAP_WINDOW} days of day {start}")
        if len(window) == SC_CAP:
            after = [d for d in SEND_DAYS if d > window[-1]]
            if after and after[0] < start + SC_CAP_WINDOW + SC_COOLDOWN:
                found.append(f"send days {SEND_DAYS}: day {after[0]} lands inside the "
                             f"{SC_COOLDOWN}-day cooldown (earliest day {start + SC_CAP_WINDOW + SC_COOLDOWN})")
    return found


def title(n: int, arm: str, aud: str, i: int) -> str:
    """Template title, unique and under Smarter Contact's 35-character limit."""
    t = f"P1 T{n} {arm + ' ' if arm else ''}{'No Name' if aud == 'noname' else 'Named'} {i:02d}"
    assert len(t) <= SC_TEMPLATE_TITLE, t
    return t


# ---------------------------------------------------------------------------
# The no-name split. A Smarter Contact template cannot branch on whether a
# merge field is empty, and it renders DataSift's RAW first-name field, not a
# cleaned one. Measured on ty+2 Priority 1 (5,349 records, 2026-10-20): 153
# trusts/LLCs with no first name ("Hi ,"), 42 initials ("Hi F,"), 5 entity-word
# names, 2 blanks, and 99 multi-token first names ("Hi Maria G Fuentes,").
# Those 301 are tagged so they can be pushed to their own group and get the
# no-name pools.
NO_NAME_TAG = "SC No First Name"
PRIORITY_TAG = "Priority 1"


def no_name_reason(owner: dict) -> str:
    """Why this owner cannot be greeted by <First Name>, or '' when they can."""
    from sms_agent.knowledge import touches
    fn = (owner.get("first_name") or "").strip()
    ln = (owner.get("last_name") or "").strip()
    co = (owner.get("company") or "").strip()
    full = " ".join(p for p in (fn, ln) if p) or co
    if not fn:
        return "no first name (company or trust)" if co else "no first name"
    if touches.is_entity(full):
        return "entity words in the name"
    if not touches.clean_first(f"{fn} {ln}".strip()):
        return "initials only"
    if " " in fn:
        return "first-name field holds several words"
    if fn.isupper() and len(fn) > 1:
        return "first name in all caps"
    return ""


# The split test's control arm. Chosen by a hash of the record uuid, so a rerun
# after new Priority 1 records land extends the SAME split instead of reshuffling
# who already got which opener.
CONTROL_TAG = "SC Control"


def _priority_rows(rd) -> list[dict]:
    tags = rd._call("/api/internal/tag/?limit=10000")
    tags = tags.get("results", tags) if isinstance(tags, dict) else tags
    p1 = next(t["uuid"] for t in tags if (t.get("title") or "") == PRIORITY_TAG)
    rows, off = [], 0
    while True:
        r = rd._call("/api/internal/property/", method="POST", override="GET",
                     body={"limit": 250, "offset": off, "query": {"must": {"any_tags": [p1]}}})
        page = r.get("results") or r.get("data") or []
        rows += page
        off += 250
        if not page or off >= (r.get("count") or 0):
            return rows


def _apply_tag(rd, uuids: list[str], tag: str) -> int:
    """Per-record read-modify-write PATCH of the full tag set, the contract
    crm_standalone.add_tags verified. POST /property/add-tags/ is NOT used: it
    has applied tags account-wide. Idempotent."""
    added = already = failed = 0
    for i, uuid in enumerate(uuids):
        try:
            cur = list(rd._call(f"/api/internal/property/{uuid}/").get("tags") or [])
            if tag in cur:
                already += 1
                continue
            rd._call(f"/api/internal/property/{uuid}/", method="PATCH",
                     body={"tags": cur + [tag]})
            if i == 0 or i % 50 == 0:  # read back early and periodically
                back = rd._call(f"/api/internal/property/{uuid}/").get("tags") or []
                if tag not in back or not set(cur) <= set(back):
                    raise RuntimeError(f"read-back mismatch on {uuid}: {back}")
            added += 1
        except RuntimeError as e:
            failed += 1
            print(f"  failed {uuid}: {str(e)[:120]}")
            if i == 0:
                return 1  # the first write did not verify, stop before the rest
    print(f"tagged {added} '{tag}', already tagged {already}, failed {failed}")
    return 1 if failed else 0


def tag_no_name(commit: bool) -> int:
    """Tag every Priority 1 record Smarter Contact cannot greet by name."""
    from obituary_opportunity import Reader
    rd = Reader("datasift-apikey", gap=0.35)
    rows = _priority_rows(rd)
    todo = [(x["uuid"], no_name_reason(x.get("owner") or {})) for x in rows]
    todo = [(u, why) for u, why in todo if why]
    by_why: dict[str, int] = {}
    for _, why in todo:
        by_why[why] = by_why.get(why, 0) + 1
    print(f"{PRIORITY_TAG}: {len(rows)} records, {len(todo)} need the no-name templates")
    for why, n in sorted(by_why.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5}  {why}")
    if not commit:
        print(f"DRY RUN: would tag {len(todo)} records '{NO_NAME_TAG}'. Rerun with --commit.")
        return 0
    return _apply_tag(rd, [u for u, _ in todo], NO_NAME_TAG)


def tag_control(fraction: float, commit: bool) -> int:
    """Tag a stable `fraction` of the NAMED Priority 1 records as the control arm."""
    import hashlib
    from obituary_opportunity import Reader
    rd = Reader("datasift-apikey", gap=0.35)
    named = [x["uuid"] for x in _priority_rows(rd) if not no_name_reason(x.get("owner") or {})]
    pick = [u for u in named
            if int(hashlib.sha256(f"sc-control|{u}".encode()).hexdigest(), 16) % 10000
            < fraction * 10000]
    print(f"{len(named)} named {PRIORITY_TAG} records, {len(pick)} in the control arm "
          f"({fraction:.0%}), {len(named) - len(pick)} on the empathy opener")
    if not commit:
        print(f"DRY RUN: would tag {len(pick)} records '{CONTROL_TAG}'. Rerun with --commit.")
        return 0
    return _apply_tag(rd, pick, CONTROL_TAG)


def to_sc(text: str, sender: str) -> str:
    """Neutral placeholders -> Smarter Contact merge tags, sender as a literal."""
    tags = {**SC_TAGS, **LAND_TAGS}
    return PLACEHOLDER_RX.sub(
        lambda m: sender if m.group(1) == "sender" else tags[m.group(1)], text)


def export_land(sender: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "sc_land_templates.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["title", "lane", "type", "touch", "job", "send day", "template",
                    "worst-case chars before opt-out"])
        for lane, n, named, noname in land_arms():
            job, day = TOUCHES[n][0], TOUCHES[n][3]
            for aud, pool in (("named", named), ("noname", noname)):
                for i, t in enumerate(pool, 1):
                    w.writerow([land_title(lane, n, aud, i), lane, "Initial" if n == 1 else "Follow-up", n, job,
                                day, to_sc(t, sender), len(t.format(**dict(LAND_WORST, sender=sender)))])
    return path


def export(sender: str) -> list[Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    paths = {k: OUT_DIR / f"sc_{k}.csv" for k in
             ("templates", "quick_replies", "labels", "workflows", "response_removal")}
    wl = lambda t: len(t.format(**dict(WORST, sender=sender)))  # noqa: E731
    with open(paths["templates"], "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["title", "type", "touch", "job", "send day", "send as",
                    "template", "worst-case chars before opt-out"])
        for n, arm, named, noname in _arms():
            job, day = TOUCHES[n][0], TOUCHES[n][3]
            for aud, pool in (("named", named), ("noname", noname)):
                camp = ("P1 Control" if arm else "P1 Empathy") + (" No Name" if aud == "noname" else "")
                for i, t in enumerate(pool, 1):
                    w.writerow([title(n, arm.title(), aud, i),
                                "Initial" if n == 1 else "Follow-up", n,
                                "Control identity check" if arm else job, day,
                                f"initial campaign {camp}" if n == 1 else
                                f"follow-up campaign to non-responders, day {day}"
                                + (" (No Name group only)" if aud == "noname" else ""),
                                to_sc(t, sender), wl(t)])
    with open(paths["quick_replies"], "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "when the owner says", "then set label", "quick reply"])
        for _, name, when, label, body in QUICK_REPLIES:
            w.writerow([name, when, label or "(leave as is)", to_sc(body, sender)])
    with open(paths["labels"], "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["label", "use it when", "DataSift property status",
                    "DataSift phone status", "also a DataSift phone tag"])
        w.writerows([lab, when, ps, ph, "yes" if tag else ""] for lab, when, ps, ph, tag in LABELS)
    with open(paths["workflows"], "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["workflow", "add contacts labeled", "step", "day", "message"])
        for name, (start, _, steps) in WORKFLOWS.items():
            for i, (day, body) in enumerate(steps, 1):
                w.writerow([name, start, i, day, to_sc(body, sender)])
    with open(paths["response_removal"], "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["keyword"])
        w.writerows([k] for k in RESPONSE_REMOVAL)
    return list(paths.values())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="audit every template and exit")
    ap.add_argument("--preview", type=int, default=0, help="print N rendered samples per touch")
    ap.add_argument("--export", action="store_true", help="write paste-ready CSVs")
    ap.add_argument("--sender", default=DEFAULT_SENDER, help="first name that signs the texts")
    ap.add_argument("--tag-no-name", action="store_true",
                    help="tag Priority 1 records that need the no-name templates (dry unless --commit)")
    ap.add_argument("--tag-control", type=float, default=0.0, metavar="FRACTION",
                    help="tag this share of named Priority 1 records as the control arm "
                         "(dry unless --commit)")
    ap.add_argument("--commit", action="store_true", help="write the tags")
    a = ap.parse_args()
    if a.tag_no_name:
        return tag_no_name(a.commit)
    if a.tag_control:
        return tag_control(a.tag_control, a.commit)

    found = audit() + land_audit()
    if found:
        print("TEMPLATE PROBLEMS (nothing ships until these are fixed):")
        for p in found:
            print("  " + p)
        return 1
    n_t = sum(len(p) + len(q) for _, _, p, q in _arms())
    n_w = sum(len(s) for _, _, s in WORKFLOWS.values())
    print(f"OK: {n_t} touch variants, {len(QUICK_REPLIES)} quick replies and {n_w} "
          "workflow steps pass the human-voice, list-word, Priority 1, GSM-7, "
          f"length (touches <= {TOUCH_CAP} before opt-out) and question checks.")
    n_l = sum(len(p) + len(q) for _, _, p, q in land_arms())
    print(f"OK: {n_l} land touch variants (lots and tracts) pass the same checks on the land profile.")
    if a.preview:
        sample = dict(WORST, sender=a.sender)
        for n, (job, named, _, day) in TOUCHES.items():
            print(f"\nTouch {n}, day {day}: {job}")
            for t in named[: a.preview]:
                r = t.format(**sample)
                print(f"  ({len(r)}c) {r}")
    if a.preview:
        sample = dict(LAND_WORST, sender=a.sender)
        for lane, n, named, _ in land_arms():
            print(f"\nLand {lane} touch {n}")
            for t in named[: a.preview]:
                r = t.format(**sample)
                print(f"  ({len(r)}c) {r}")
    if a.export:
        for path in export(a.sender) + [export_land(a.sender)]:
            print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
