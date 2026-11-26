# P0-B2 Pilot packet — technical appendix (engineering record)

Generator `clinical_review/tools/build_p0b2_ladder_package.py` (p0b2-ladder-review-package-v1); bank artifact digest `1c0c3a8607f24cbc71b3fd9aff2bf1b114a4e6c82b86278bb63ed7f72da42f40`. Founder decision: Option B envelope.

## Envelope candidate

- `minimum_node_ref`: `node1:f3be24e2680cfb214e498b877b7fd552`
- `maximum_node_ref`: `node1:afa9b5c06d9465c51a99126dcb251c22`
- `starting_node_ref`: `FROM BASELINE (section D) OR CLINICIAN`

## Chain (node identity and provenance)

| Step | node_ref | type | step# | rung_ref | workbook family → canonical | endpoint source |
|---|---|---|---|---|---|---|
| Step 1 | `node1:f3be24e2680cfb214e498b877b7fd552` | endpoint | 2 | `rung1:6c8e17d12c4ca2ab1d583e94635100d3` | expressive_three_words → expressive_first_words | workbook_final_bridge_row |
| Step 2 | `node1:cb508e3ea7253ee6f3e59e092fcb54f0` | bridge | 1 | `rung1:feb590cf2788978b383c11062ce67c1b` | expressive_vocabulary_growth → expressive_vocabulary_growth | — |
| Step 3 | `node1:1a3b74cec4d866fb196f47fb8d6302ab` | bridge | 2 | `rung1:feb590cf2788978b383c11062ce67c1b` | expressive_vocabulary_growth → expressive_vocabulary_growth | — |
| Step 4 | `node1:f8102ec520a2932f03112d9c4c6e0e3a` | bridge | 3 | `rung1:feb590cf2788978b383c11062ce67c1b` | expressive_two_word_phrase → two_word_phrases | — |
| Step 5 | `node1:afa9b5c06d9465c51a99126dcb251c22` | endpoint | 4 | `rung1:feb590cf2788978b383c11062ce67c1b` | expressive_two_word_phrase → two_word_phrases | workbook_final_bridge_row |

## Transitions

- `node1:f3be24e2680cfb21…` → `node1:cb508e3ea7253ee6…`: **TEXT_LINKED** — previous_bridge_step text matches exactly one endpoint at 18m
- `node1:cb508e3ea7253ee6…` → `node1:1a3b74cec4d866fb…`: **STRUCTURED** — same milestone, bridge_step_number order
- `node1:1a3b74cec4d866fb…` → `node1:f8102ec520a2932f…`: **STRUCTURED** — same milestone, bridge_step_number order
- `node1:f8102ec520a2932f…` → `node1:afa9b5c06d9465c5…`: **STRUCTURED** — same milestone, bridge_step_number order

## Card proposals

| activity_template_id | title | family | tier | allowed nodes | proposed |
|---|---|---|---|---|---|
| `atpl1:987acefca5dcf8001c218aba7a951022` | What's That? Book | expressive_vocabulary_growth | bucket_curated | Step 2, Step 3 | NEEDS CLINICIAN DECISION |
| `atpl1:aa5d7da84faba502ab7a4c6284f1347d` | Cleanup Request Game | expressive_vocabulary_growth | bucket_curated | Step 2, Step 3 | NEEDS CLINICIAN DECISION |
| `atpl1:13a4fd9b1e557d877381cc41fed5098c` | Paint Choice Words | expressive_vocabulary_growth | bucket_curated | Step 2, Step 3 | NEEDS CLINICIAN DECISION |
| `atpl1:adafba440db93934b88a2fa45a364f1d` | Family Photo Names | expressive_vocabulary_growth | bucket_curated | Step 2, Step 3 | NEEDS CLINICIAN DECISION |
| `atpl1:cb95176fdcc5cff557bdc409da186304` | Quick Naming Walk | expressive_vocabulary_growth | bucket_curated | Step 2, Step 3 | NEEDS CLINICIAN DECISION |
| `atpl1:80d40c4226b091fc36ba73aeb061651e` | What's Missing? Box | expressive_vocabulary_growth | bucket_curated | Step 2, Step 3 | NEEDS CLINICIAN DECISION |
| `atpl1:7e97ac6a60c3623f7a609c1c2b7e1995` | Snack Choice Words | expressive_vocabulary_growth | bucket_curated | Step 2, Step 3 | NEEDS CLINICIAN DECISION |
| `atpl1:f273cccd0afd3c96730215bc49ba8096` | Song Fill-In Game | expressive_vocabulary_growth | bucket_curated | Step 2, Step 3 | NEEDS CLINICIAN DECISION |
| `atpl1:2a9316b59bfcfef283cfeeb26a2034f6` | Pick Your Toy | expressive_vocabulary_growth | bucket_curated | Step 2, Step 3 | NEEDS CLINICIAN DECISION |
| `atpl1:ca49a944bb6b1181d845524845bdb023` | Give Me Two! Game | expressive_vocabulary_growth | bucket_curated | Step 2, Step 3 | NEEDS CLINICIAN DECISION |
| `atpl1:3b00aee6c8a322cddda1ec55543158a2` | More Please Routine | expressive_vocabulary_growth | bucket_curated | Step 2, Step 3 | NEEDS CLINICIAN DECISION |
| `atpl1:5e054d694dcf6b0e700951eb4c2ebe8d` | Two-Word Choice Game | expressive_vocabulary_growth | bucket_curated | Step 2, Step 3 | NEEDS CLINICIAN DECISION |
| `atpl1:5f44b9eba107589ca26c6610f2c6cb05` | Help Me | two_word_phrases | family_curated | Step 4, Step 5 | NEEDS CLINICIAN DECISION |
| `atpl1:1226e454bd64d869206574bb6ad9851f` | All Done and Stop | two_word_phrases | family_curated | Step 4, Step 5 | NEEDS CLINICIAN DECISION |
| `atpl1:166c4e5492dab00d41b9fb4d5162202b` | Want + Choice | two_word_phrases | family_curated | Step 4, Step 5 | NEEDS CLINICIAN DECISION |
| `atpl1:f7a99d017c398560058379637732ac58` | Action + Object | two_word_phrases | family_curated | Step 4, Step 5 | NEEDS CLINICIAN DECISION |
| `atpl1:041794bbb1acb1f86a27ca9460088c2d` | More Bubbles | two_word_phrases | family_curated | Step 4, Step 5 | NEEDS CLINICIAN DECISION |
| `atpl1:01cdd9f34e37a62bed4feda4300902a7` | My Turn | two_word_phrases | family_curated | Step 4, Step 5 | NEEDS CLINICIAN DECISION |
| `atpl1:79e480b2ccc99485f2d7ecb0630fd964` | Who's Here (candidate) | expressive_first_words | ledger: generated_candidate, reviewer: None | Step 1 | Step 1 |
| `atpl1:03857398879347566cacc0d6280d2517` | Peekaboo Names (candidate) | expressive_first_words | ledger: generated_candidate, reviewer: None | Step 1 | Step 1 |
| `atpl1:a12d0335f334b1ab0c320d4cec1aeb41` | Favorite Things (candidate) | expressive_first_words | ledger: generated_candidate, reviewer: None | Step 1 | Step 1 |
| `atpl1:1dc565ad660e3551e3707e2497f82d2c` | Snack Choices (candidate) | expressive_first_words | ledger: generated_candidate, reviewer: None | Step 1 | Step 1 |
| `atpl1:dc14cad0ff29afbf65f43725330f5abd` | Bath and Bedtime Words (candidate) | expressive_first_words | ledger: generated_candidate, reviewer: None | Step 1 | Step 1 |
| `atpl1:8ce3a7e5bb00817c5754654af54899db` | Magic Word Box (candidate) | expressive_first_words | ledger: generated_candidate, reviewer: None | Step 1 | Step 1 |
| `atpl1:b0eb9f7e0e847c69984900b04429ec79` | Match the Picture (candidate) | expressive_first_words | ledger: generated_candidate, reviewer: None | Step 1 | Step 1 |

## Coverage (eligible, before Hannah's split; approved now = 0 everywhere)

| Step | reviewed eligible | unreviewed candidates | themes |
|---|---|---|---|
| Step 1 | 0 | 7 | 7 |
| Step 2 | 12 | 0 | 11 |
| Step 3 | 12 | 0 | 11 |
| Step 4 | 6 | 0 | 6 |
| Step 5 | 6 | 0 | 6 |
