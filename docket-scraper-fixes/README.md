# Docket Scraper fixes

Four modules in the "Scraper - Docket" scenario change. Paste each file into the module named below. No module IDs, mappings, or data structures change, so nothing downstream needs rework.

| File | Make module | Where |
|---|---|---|
| `module-38-score-and-gate.js` | 38, Execute Code | Replace the whole code box |
| `module-45-apply-litigation-research.js` | 45, Execute Code | Replace the whole code box |
| `module-30-entity-profile-prompt.txt` | 30, Gemini (entity profile) | Replace the prompt text |
| `module-43-litigation-research-prompt.txt` | 43, Gemini (litigation research) | Replace the prompt text |

## Why bad cases were getting through

1. **Unknown data scored as acceptable.** An unverified HQ and size each added 0 points and the cutoff was 0 or higher, so a company the model knew nothing about qualified. Module 30 told the model to return "unknown" for everything unless the name matched exactly, which produced many of these.
2. **Size was only a score penalty.** A large Illinois-headquartered company scored 4 + (-1) = 3 and ranked well. Only micro and small business were disqualified.
3. **Out-of-state HQ was only a penalty.** A mid-market company headquartered elsewhere with any Illinois operation scored 0 or better.
4. **Ownership never matched.** The code looked for the exact strings `government` and `small_business`, but the prompt never defined ownership values, so the model's free text rarely matched.
5. **Representation was weakly checked.** Counsel only disqualified a case when a found matter carried the same docket number as the new case. A firm found on the employer's older NLRB, EEOC, or court matters cost 2 points at most. A failed litigation search still allowed a "Yes".
6. **Top three always got "Yes".** The scenario ranks and takes the top three, so a weak week still produced three recommendations.

## What changed

Module 38:
- Hard gates. A case is disqualified, regardless of score, if it is government, an individual, publicly traded, micro, large, national, any size outside `ALLOWED_SIZES`, or headquartered outside Illinois.
- Unverified HQ or size now means "Insufficient profile" and cannot be recommended.
- Minimum score of 3 (`MIN_SCORE`) in addition to the gates.
- Ownership, state, and size are normalized from free text.

Module 45:
- Any counsel found for the exact or a related entity, any national-firm signal, or counsel on the new docket disqualifies the case, and the firm name goes into the flags.
- A failed or missing litigation search blocks "Yes".
- Same minimum score.

Module 30 prompt: fixed value lists for size and ownership, size and HQ taken from the ultimate parent, an acquisition check (for cases like Vitech), and the exact-name rule replaced with a rule that still rejects look-alike companies.

Module 43 prompt: searches the new docket for defense counsel, adds "represented by" and "outside counsel" searches, and defines when `counselObserved` is "yes". The old "if researchCandidate is false, do not search" branch is removed because the model returned it for real candidates with a blank docket number, which meant the counsel check never ran. Module 43 now researches every case. Do not put a filter on module 43: if no case passes it, module 47 and everything after it (including the sheet writes) does not run.

Module 45 treats a "Not researched" reply and a missing or blank-docket result as a failed search. Module 47 takes `docketNumber` from `{{39.value.docketNumber}}`, not from the model's reply.

Module 54 input `existingFlags` is `{{23.`22`}}` (with backticks) and module 27 column W is `{{54.result.flags}}`.

## Settings you may want to change

At the top of module 38: `REQUIRE_IL_HQ`, `ALLOWED_SIZES`, `MIN_SCORE`, `RESEARCH_LIMIT`, `OUTREACH_LIMIT`. The size bands (middle_market is 50 to 500 employees) are in the module 30 prompt and match the ranges already in your old code. Module 45 repeats `MIN_SCORE` and `OUTREACH_LIMIT`, so change both together.

## Not verified

I tested the two code modules against sample inputs in Node. I did not run the scenario in Make or call Gemini, so the prompt changes are untested. All four Gemini modules use `gemini-3.5-flash-lite`. A lighter model with search grounding is the likeliest reason counsel and size lookups come back thin. Moving modules 30 and 43 to a stronger model is worth trying, but I can't confirm which model names your Make connection offers.
