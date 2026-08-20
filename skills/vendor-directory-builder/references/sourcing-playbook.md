# Sourcing Playbook: mining a community for real providers

The goal of Phase 1 is to harvest a list of **names people actually vouch for**, with
enough context (who recommended them, for what) to prioritize. Everything here is about
getting high-signal leads cheaply; verification happens in Phase 2.

## 1. Mine the named community first

Community recommendations outrank ads and cold search results because they come from
people who paid for the work. Start with whatever source the user named (a Facebook
group, a subreddit, a local forum, a Slack/Discord, a Nextdoor, an email referral list).

### Facebook groups (the most common case)

A private group needs the user's **logged-in browser**: drive it with the browser tools;
don't try to fetch group URLs server-side (they're login-walled). Then:

1. **Use in-group search, not the feed.** The reliable pattern is the group's search URL:
   `https://www.facebook.com/groups/<group>/search/?q=<term>`
   Run it once per category term (for a flip crew: `contractor`, `general contractor`,
   `plumber`, `electrician`, `hvac`, `roofing`, `handyman`, `flooring`, `drywall`,
   `painter`, `foundation`, plus niche terms, see the taxonomy file). Searching beats
   scrolling: you get years of relevant posts in one view.
2. **Harvest two kinds of posts:**
   - **Self-promoters**: a provider advertising their own services. Contact info is
     usually right in the post text. Extraction of the results page text captures these
     cheaply.
   - **Recommendation-request threads**: "who's a good ___?" The answers live in the
     *comments*, which don't appear in page text. **Open those threads** (click the
     post/timestamp to open the permalink modal) and read the comments in a screenshot.
     These are the highest-value leads because they're peer referrals.
3. **The comments are the gold.** A "looking for a plumber" post with 12 comments will
   name several plumbers, often with a phone number and a one-line review ("used him on
   three flips, never late"). Prioritize threads with the most comments.
4. **Capture per lead:** name/company, any contact info, category/trade, **who
   recommended them**, and sentiment. Note business-page tags (often a verified badge);
   those are real companies and the highest-quality mentions.

### Other community types

- **Reddit / forums:** search the subreddit/forum for the category term; recommendation
  threads and "recs megathreads" are the analog of FB comment threads.
- **Nextdoor:** neighborhood recommendation posts, very local, good for home services.
- **Slack/Discord communities:** search channels; ask isn't always possible, so search
  history.
- **A referral list / spreadsheet the user already has:** treat it as pre-sourced leads
  and jump to Phase 2 verification.

## 2. The single strongest signal: cross-validation

When **two or more different people** independently name the same provider, that is the
best lead you can get. Flag it and recommend calling those first. One enthusiastic
self-post is weaker than two unrelated members saying "we use them."

## 3. Efficiency notes (so you don't burn the session)

- Text extraction of a search-results page is cheap and catches all the self-promoters
  and identifies which threads to open, so do that first for each term.
- Reserve the expensive screenshot-and-read step for the **highest-comment recommendation
  threads**. You don't need to read every thread; a couple of the richest per category
  usually surfaces the names that keep recurring.
- Specialty/niche categories (septic, termite, countertops, excavation) are often thin or
  absent in a general investor group. That's expected: note it and source those from
  public records in Phase 2. Don't grind the group for names that aren't there.

## 4. Cutting-edge tactics to keep a bench deep

Beyond the primary community, these keep a directory alive as the user scales. Offer them
in the Methodology tab so the list stays useful after you hand it over.

- **Sub off an active operator.** The people posting the most deals/jobs already have a
  vetted crew. "Who are your top three and can I use your name?" inherits their vetting
  for free. Fastest shortcut there is.
- **Ask suppliers who the pros are.** Supply-house pro desks (plumbing/electrical supply,
  flooring wholesalers, paint stores, lumberyards) see who buys volume, pays on time, and
  pulls permits. "Which contractors here do the most volume and always pay?"
- **Pull permit data.** County/city permit records list the licensed contractor on every
  job. Names that recur on permits in the target ZIPs are the busy, permit-pulling pros,
  and it proves they actually pull permits.
- **Mine adjacent communities.** Other local groups, associations (e.g., a local REIA,
  builders' association), and Nextdoor cover providers the first group missed.
- **The small-test-job filter.** For promising-but-unverifiable names, recommend one
  small low-risk job first. Speed, cleanliness, communication, and whether the bid held
  tell you everything before you trust them with a big scope.

## 5. What "done" looks like for Phase 1

A working list of leads across the use case's categories, each tagged with its source
signal (self-promo vs. recommended-by-N, and by whom), ready for verification. Gaps are
labeled, not hidden.
