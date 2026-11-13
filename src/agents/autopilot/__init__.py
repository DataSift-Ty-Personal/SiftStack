"""The autopilot: the prospecting half of the business, run stage by stage (build 1.0.64, 2026-11-11).

The ty+2 account already describes the whole lifecycle as saved presets (FTM - 00 Needs Skipped,
01 Skipped No Numbers, 02 Ready to Call, Call Attempt 1 to 3, Mail 01 to 06, Exhausted Call -> DP,
the Not Interested windows). What nothing did was MOVE records between them: a person had to assign
each new record, re-trace the ones with no numbers, score the phones, drop the mail, tag the
auctions. The autopilot does those moves.

Three rules hold the design together:
  * The presets are the state. A stage reads its presets live, by title, every run. When the owner
    edits a preset in DataSift, the autopilot follows. Progression rides DataSift's own counters
    (the dialer's call attempts, the mail attempts), never a private flow tag.
  * A stage starts in plan: it runs on its schedule, works out exactly what it would do and what
    it would cost, posts that, and writes nothing. The owner flips one stage at a time to live.
  * Money moves inside caps, outreach to homeowners is staged for a human unless its stage says
    auto, and every action is written to the ledger so a re-run never pays or mails twice.

The playbook is team/autopilot.yaml. Each stage there is also a workflow on the team roster, owned
by the employee who answers for it (Atlas assigns and watches auctions, Diggs traces and scores,
Quinn mails and reports).
"""
