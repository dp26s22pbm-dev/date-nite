# Tracker notes check

New defendants are compared with employers the tracker already marks as firm clients (AD Client? = Yes) or whose Additional Notes say represented, client, out of state, outside, or no longer.

Parts:
1. `watchlist-formula.txt`: a Watchlist tab in the tracker workbook that lists those employers.
2. Two new Make modules between module 14 and module 15: a Google Sheets Search Rows on the Watchlist tab, then an array aggregator.
3. `module-42-with-notes.js`: module 42 (the conflict check) with the notes check added. A match goes down the existing conflict review route with the note as the reason.

`module-42-original.js` is module 42 before this change. `test-docket-report-notes.csv` is a 5 case test file.
