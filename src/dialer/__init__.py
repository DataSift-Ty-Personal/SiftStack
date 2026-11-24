"""The SiftStack dialer: a Twilio softphone, click-to-dial from DataSift, and a single-line power dialer.

What it replaces: the click-to-call and power dialing the team gets from Kixie and smrtPhone, built on
Twilio Programmable Voice with our own rules, our own write-back to DataSift, and the texting app
(Smarter Contact) told about every call.

The shape, in one paragraph. A caller signs in once and keeps one browser tab open (phone.html): a
Twilio Voice SDK Device, the record being called, the text thread with that number, and the
disposition buttons. Every call is its own Twilio conference: the caller's browser leg joins the
conference named for the call, and the server dials the homeowner into it through the Conference
Participants API. A conference per call is what makes voicemail drop (redirect the homeowner's leg to
a recording), hold, warm transfer to a closer, and a manager's listen or whisper possible with no
extra plumbing. The Chrome extension (extensions/sift-dialer) puts a call button beside every phone
number on app.reisift.io and, when the power dialer moves on, opens the next record in the DataSift
tab. It holds no keys and never talks to the server: it only passes messages between the DataSift tab
and the dialer tab.

    app.py          the web app: the caller's pages and API, and Twilio's webhooks (signature checked)
    calls.py        one call's life: prepare, the TwiML, the homeowner leg, callbacks, controls
    queue.py        the power dialer: a DataSift preset walked one record at a time, through the gate
    rules.py        may this number be dialed now: calling hours on both clocks, the gate, the daily limit
    dispositions.py what each outcome writes to DataSift and tells the texting app, as one table
    writeback.py    the worker that performs those writes off the call path, with retries and read-backs
    sift.py         DataSift over the Open API key: records, presets, phones, counters, notes
    twilio_voice.py tokens, TwiML, the REST calls, the webhook signature
    texting.py      the texting app: the thread for a number, a call's outcome, hand raises pushed in
    store.py        SQLite on the app's own volume: callers, calls, the queue, the write log

Three rules hold it together:
  * Nothing slow runs while a phone is ringing. A webhook answers from the database; every DataSift
    write goes through the write-back worker, so a slow CRM never delays a call or a disposition.
  * The same holds as everywhere else. A record the gate holds is never put in the power dialer, a
    number marked do-not-call is never dialed, and calls go out only inside the house calling hours on
    the business clock and on the homeowner's own (both, every candidate time zone).
  * It starts safe. DIALER_CALLS=test dials only the numbers in DIALER_TEST_NUMBERS, and
    DIALER_WRITEBACK=dry writes nothing to DataSift. Each is switched on by hand, one at a time.
"""
