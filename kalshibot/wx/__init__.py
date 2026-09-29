"""Weather market taxonomy and extended modeling (packages WX-1 .. WX-7).

Research layer beside the live daily weather strategy. It discovers every
Kalshi weather market, reads each market's OWN rules for its settlement
source (Kalshi: "Always check the specific market's rules for its source"),
records forecasts point in time, calibrates sources, simulates multi-day
contracts, and scores all of it - without placing an order.

Nothing in this package can trade. It holds no trading client; everything it
reads from Kalshi goes through the unauthenticated, GET-only LiveBookReader.
Production execution stays locked by env_guard; WX-7 eligibility is a record,
not a switch.

Storage is its own SQLite file (wx.store), so the research load never
contends with the journal the trading workers write to.
"""
