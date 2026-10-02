# TASKS.md

**Goal.** Put the structure of every university into Wikidata: university, then school,
then department, with faculty linked to their departments. Every fact backed by a web
page that a person has checked.

This file is the plan. It is written for two students, Anya and Shuo, who direct a coding
agent rather than write most of the code. Read it top to bottom once. Come back to your
track every week. If a word is unfamiliar, see the glossary at the end.

Last updated: 2026-10-02

---

## 1. What exists today

The code can do one thing well: **given a university, find its schools and colleges.**

```
python -m wikidata_discover.scripts.wikidata_division_discover discover Q49210
```

That command (Q49210 is NYU):

1. Looks up the university on Wikidata: name, website, and the schools already linked to it.
2. Asks an LLM to list the university's schools. It tries OpenAI first (with web search).
   If that fails, it tries Anthropic, then Gemini (without web search).
3. Checks each school the LLM named against what Wikidata already has. First by comparing
   names, then by asking an LLM when names are close but not identical.
4. Labels each school: already linked, exists but not linked (an "orphan"), or missing.
5. Writes the results to `wikidata_discover/results/`: a CSV, a QuickStatements file that
   could create the missing schools, and a small JSON report.

**How good is it?** We built the true list of schools for 12 universities by hand
(`wikidata_discover/eval/ground_truth.py`) and measured. The best setup, Anthropic judging
a merged list from OpenAI and Gemini, gets about 95% precision and 95% recall. The numbers
are in `wikidata_discover/eval/results_summary.csv`. Rerun with:

```
python -m wikidata_discover.eval.run_eval
```

**What does not exist yet:**

- The `discover` command uses one LLM at a time. The better setup with a judge exists
  only in the evaluation code. (Anya, week 5.)
- Web search is only on for OpenAI. The other two answer from memory, so the URLs they
  cite may not exist. (Anya, week 5.)
- Runs are only partly recorded. The LLM's cleaned-up answer is cached in
  `wikidata_discover/results/cache/` so a rerun is free, but the raw response, the prompt,
  the pages consulted, and which run produced what are not kept. (Anya, week 2.)
- Departments. The code only goes one level down. (Anya, weeks 3 to 4.)
- Any check that a cited web page really says what the LLM claims. (Shuo, weeks 3 to 4.)
- Human review. (Shuo, weeks 6 to 8.)
- Nothing has been uploaded to Wikidata. (Both, week 9.)

**Collecting many universities at once.** `discover` handles one university. For the whole
list there is a batch runner that resumes where it stopped and uploads every output to the
`academiabot` bucket under `runs/<run_id>/`. It runs from a terminal
(`python -m wikidata_discover.scripts.batch_collect <run_id> <QID> ...`) or as a Cloud
Function called every 30 minutes by Cloud Scheduler, so that collection does not depend on anyone's
laptop. The first batch of 25 universities is in the bucket as `2026-10-02-batch01`. How
to deploy, start, stop, and watch a cloud run is in AGENTS.md ("Running collection in the
cloud"). You check a run by reading `runs/<run_id>/log.jsonl` in the bucket: one line per
university, with `status` and `uploaded`. Only Panos starts a cloud run, because it spends
LLM credit. Anya's week 2 (the run log) builds on this: the batch runner already records
what ran, with which models and code version, and the run log tables are where that
record moves next.

Where the project came from and why we model things the way we do: `docs/BACKGROUND.md`.
Read it once. It is short.

---

## 2. How we work

You direct a coding agent (Claude Code or similar). The agent reads `AGENTS.md` for the
rules of this codebase and reads this file for what to do. You do the part the agent
cannot do:

- **Say exactly what to build.** Name your track and the week. Give the agent the
  "you check it by" line from your table. That is the test it must pass.
- **Check the result on data you chose.** Run the command. Open the file. Count the rows.
  Never accept "done" without seeing it work.
- **Judge the data.** Is this really a department? Is this URL really the right page? The
  agent guesses. You know, or you find out.
- **Own what goes into Wikidata.** The agent never uploads. A person does, after review.

Rules that keep things manageable:

1. One week of work per branch and pull request. Small steps.
2. Ask the agent for a short plan before it writes code. Read it. Push back if it is
   doing more than the week asks.
3. If the agent says tests pass, run them yourself: `python -m pytest tests -q`.
4. At the end of each session, have the agent tick your boxes in this file, add anything
   it learned to `AGENTS.md`, and summarize the change in the pull request.
5. Keep a short log for Panos: date, what you tried, what worked, what you decided.
6. Weekly meeting: bring the deliverable in the right-hand column of your table.

---

## 3. Week 1 for both of you (Oct 2 to 8): get set up

- [ ] Ask the agent to install dependencies and run the tests. All should pass (33 today).
- [ ] Get API keys working. Today the code reads keys only from `.env`: copy `env.example`
      to `.env` and paste at least one key (ask Panos). Never commit `.env`. After Anya's
      week 2 lands, the keys will come from Secret Manager and `.env` becomes optional.
- [ ] Run discovery on NYU (Q49210). Open `wikidata_discover/results/reports/Q49210_report.json`.
      (NYU's schools are all linked already, so no CSV is written for it. A university with
      missing schools also gets a CSV and a QuickStatements file.)
- [ ] Run it on a university you know well. Is the list of schools right? Note what is wrong.
- [ ] Ask the agent to walk you through the pipeline using `discovery.py` as the guide.
      Then ask it the question that confused you most.
- [ ] Run the evaluation and compare its numbers to `wikidata_discover/eval/results_summary.csv`.
- [ ] Read `docs/BACKGROUND.md`.
- [ ] Write one paragraph for Panos: what the pipeline does, and one thing you would change.

---

## 4. Anya's track: build the pipeline

Your question for the semester: **can the code find every department of a university,
and remember how it found each one?**

| Week | Dates | You ask the agent to build | You check it by | You do yourself | Hand to Panos |
|---|---|---|---|---|---|
| 2 | Oct 9 to 15 | **Record everything.** A run log in BigQuery with four tables: `runs` (one row per command run), `llm_calls` (every prompt and raw response), `evidence` (every web page fetched), `candidates` (every unit proposed, with links to the calls and pages that produced it). Big text goes to Cloud Storage; the row keeps the path. Local JSON files when GCP is unreachable. Also: `config.py` reads API keys from Secret Manager when `.env` has none. | Running discovery on NYU twice. Both runs appear in BigQuery. Open one raw LLM response in Cloud Storage and find a school name in it. Delete `.env`, run again: it still works. Then make the cloud writer fail (for example, point it at a bucket that does not exist) and run again: the run, calls, evidence, and candidates must all be in `wikidata_discover/results/runs/`. | Decide what one `candidates` row must contain so that Shuo can review it later without asking you. Write that list down and give it to Shuo. | The table schema, one page, and a screenshot of a real run in BigQuery. |
| 3 | Oct 16 to 22 | **Departments, one school at a time.** The LLM step takes any unit (a university or a school) and returns its sub-units. Each sub-unit comes with a name, a type (department, program, center), a website if known, and **one source URL**. Units with no source URL are kept but flagged. | Running it on Stern and scoring against Shuo's ground truth. Read the raw output for Stern: click five source URLs. | Agree with Shuo on what counts as a department. Write the answer in `docs/MODELING_RULES.md`, starting with the easy cases. | Department lists for Stern, Courant, Steinhardt, with the score for each. |
| 4 | Oct 23 to 29 | **The full tree.** A `--depth` option: depth 1 is schools (today), depth 2 adds departments. Output is a nested JSON tree plus the usual CSV. Every level is written to the run log. | Running `discover --depth 2 Q49210`. Open the tree. Pick 10 departments at random: right school? right type? Check 3 of them on Wikidata by hand. | Note every oddity you see: a department under two schools, two departments with the same name, a "school" that is really a program. These feed week 6. | NYU's full tree, and your list of oddities. |
| 5 | Oct 30 to Nov 5 | **Use the better LLM setup.** `discover` runs two generators and a judge instead of one LLM, with the combination set by a flag. Start with the one the current evaluation found best (Anthropic judging OpenAI plus Gemini). Web search or grounding turned on for Anthropic and Gemini too. | Running NYU depth 2 again. The precision and recall against Shuo's ground truth must not go down. Every candidate must now have a source URL that the source checker (Shuo, week 3) says is real. | Compare cost and time per university before and after. | Midpoint report: what works, what does not, cost per university. |
| 6 | Nov 6 to 12 | **The messy cases.** Code that follows `docs/MODELING_RULES.md`: joint departments get two parent links, same-name departments stay separate, renamed units are handled the way the rules say. One test per rule. | Feeding it the oddities from week 4. Each must come out the way the rules say. | Finish `docs/MODELING_RULES.md` with Panos: at least 5 real cases, each with the decision and the reason. | The rules doc and the passing tests. |
| 7 | Nov 13 to 19 | **Twelve universities.** Switch the flag to the combination Shuo chose at the end of her week 5, then run depth-2 discovery on all 12 ground-truth universities, written to the run log, with a summary table per university. | Picking one university and recounting its summary row by hand from the tree. Re-running one university: the cache must make it fast and the numbers identical. | Hand the run ids to Shuo for evaluation and review. Read the failures with her. | The 12-university summary table. |
| 8 | Nov 20 to 26 | **A correct export.** The QuickStatements exporter links with P749 (not P361), puts a reference on every statement (source URL and the date it was retrieved), writes one file per level (schools, then departments), and refuses any statement whose source URL has no human verdict. Light week, Thanksgiving. | Pasting the file into the QuickStatements web tool in preview mode (do not run). Every statement shows a reference. Then, in the review sheet, mark 3 rows reject and 2 rows fix with a corrected name, and export again: the 3 must be absent, the 2 must carry the corrected names. Remove one verdict entirely: the export must refuse that row. | Read every line of the NYU export. | The NYU export file, previewed and accepted by the tool. |
| 9 | Nov 27 to Dec 3 | Fixes only. Whatever the first upload needs. | Re-running the week 4 and week 8 checks. | **First upload**, with Shuo and Panos: pick the cleanest university, export, review every line, upload under your own Wikidata account, record the batch id here. Query Wikidata back with the SPARQL at the end of this file. | Departments of one university live on Wikidata. |
| 10 | Dec 4 to 10 | Cleanup. All tests pass. Boxes ticked in this file. | Running the tests yourself. Reading this file: does it say what was done? | Write your final report. | Report: what the pipeline can do, its numbers, cost per university, and what the next person should do first. |

**Done when:** `discover --depth 2` works on 12 universities, every run is in the run log,
every candidate has a source URL, the export is correct and referenced, and one
university's departments are live on Wikidata.

**If something is late.** Week 3 needs Shuo's ground truth (her week 2). If it is late,
build the Stern list yourself from the Stern website. Week 7 needs Shuo's choice of LLM
setup (her week 5). If it is late, keep the week 5 default.

---

## 5. Shuo's track: check that it is true

Your question for the semester: **for every fact we propose, how do we know it is true?**
Three answers, built in this order: a web page says so, an LLM confirms the page says so,
a person confirms it. Nothing enters Wikidata without the third.

| Week | Dates | You ask the agent to build | You check it by | You do yourself | Hand to Panos |
|---|---|---|---|---|---|
| 2 | Oct 9 to 15 | **A scorer.** Loads a department ground-truth CSV (one row per department, with a `source_url` column) and scores any list of names against it: precision and recall. | Feeding it your own ground-truth list plus 2 made-up department names. It must report recall 100% and exactly 2 wrong names (precision = N/(N+2) for N real names). Then remove 1 real name: recall must be (N-1)/N. | Build the ground truth for Stern, Courant, Steinhardt from their websites, one URL per department. Start from the old BigQuery table (`docs/BACKGROUND.md` explains it) but confirm every row yourself. | Ground-truth CSV, about 40 to 60 rows, every row with a URL you visited. Give it to Anya. |
| 3 | Oct 16 to 22 | **Source checker.** Given a unit name and a URL: fetch the page, record the HTTP status, say whether the name appears in the page text, save a copy of the page. Results go in Anya's `evidence` table. | Giving it 10 URLs you picked: 2 dead, 2 real but about something else, 6 correct. It must sort all 10 the way you did. | Run it on every URL the LLMs cited for the 12 universities. Read 20 failures and sort them: dead link, wrong page, right page but different name, made-up URL. | Table: share of cited URLs that exist and support the claim, per LLM provider. |
| 4 | Oct 23 to 29 | **LLM verifier.** Given a claim ("X is a department of Y") and the text of a page, answer supported, not supported, or unclear, and quote the sentence that decides it. | Labeling 100 claim-and-page pairs yourself first, then comparing. Spot-check 10 quoted sentences against the page: the quote must really be there. | Decide what verifier accuracy is good enough to use it for sorting the review sheet. Write the number down and why. | Verifier accuracy against your labels, with the confusion matrix. |
| 5 | Oct 30 to Nov 5 | **Department-level evaluation.** The eval harness extended to departments and to each LLM provider and judge combination, reading from Anya's run log. | Running it twice: identical numbers (the cache works). Pick one university and recount 5 rows by hand. | Choose the generator and judge combination for departments, with numbers. Give the choice to Anya. | Midpoint report: precision, recall, source-support rate, verifier accuracy. |
| 6 | Nov 6 to 12 | **Review sheet.** A `review` command that writes one row per proposed statement from the run log: parent, name, type, website, source URL, which providers agreed, verifier answer. Doubtful rows first. Three empty columns: `verdict` (accept, reject, fix), `corrected_value`, `notes`. An accept needs a URL. Each verdict is saved as its own row in the `reviews` table (one per reviewer per candidate), never overwriting another reviewer's. | Marking 3 rows reject and 2 fix, saving, re-opening: the verdicts persist. Anya's export (her week 8) must honor them. | Review NYU's department sheet, timed. Panos reviews the same sheet independently. Compare every disagreement. | `docs/REVIEW_GUIDE.md`, one page, written from the disagreements. |
| 7 | Nov 13 to 19 | **Prolific pilot.** A Prolific task for the review sheet: 50 rows, one qualification question, two workers per row, and a script that computes agreement with your verdicts. | Doing the task yourself as a worker first. Fix any instruction that confused you before launch. | Launch the pilot. Budget guide: about 20 cents per entity in the old project. Read every row where the two workers disagreed. | Agreement with expert verdicts, time and cost per row, and a go or no-go on scaling. |
| 8 | Nov 20 to 26 | Fixes for the failure modes found in weeks 3 to 7. Light week, Thanksgiving. | Re-running the week 3 and week 4 checks. They must still pass. | Write the review protocol: who reviews what, how many reviewers, what agreement is needed before upload. | `docs/REVIEW_PROTOCOL.md`. |
| 9 | Nov 27 to Dec 3 | **Pre-upload check.** A command that reads an export file and refuses it if any statement lacks a reference or a human verdict. | Removing one verdict and running it: refused. Restoring it: accepted. | **First upload**, with Anya and Panos. Every row of the batch verified. Watch the uploaded items for two weeks: if a Wikidata editor changes anything, find out why. | The first referenced, human-verified batch live on Wikidata, and a note on every edit by others. |
| 10 | Dec 4 to 10 | Cleanup. All tests pass. Boxes ticked in this file. | Running the tests yourself. Reading this file: does it say what was done? | Write your final report. | Report: all the numbers above in one place, plus what the next person should do first. |

**Done when:** Every statement in the first upload has a URL a person checked, the
acceptance rate and reviewer agreement are recorded, and a second person could run the
review process from the two docs alone.

**If something is late.** Week 5 needs Anya's depth-2 output (her week 4). If it is late,
evaluate on the three ground-truth schools only. Week 6 needs Anya's run log (her week 2).
If it is late, read from the CSV files instead and switch later.

---

## 6. Where the two tracks meet

| When | From | To | What |
|---|---|---|---|
| End of week 2 | Shuo | Anya | Ground-truth CSV for three schools |
| End of week 2 | Anya | Shuo | The run log schema: what a `candidates` row contains |
| End of week 3 | Anya and Shuo | each other | `docs/MODELING_RULES.md`, first version |
| End of week 5 | Shuo | Anya | Which LLM setup to use for departments (Anya applies it in week 7) |
| End of week 7 | Anya | Shuo | Run ids for the 12 universities |
| Week 8 | Shuo | Anya | Verdicts in the `reviews` table, which the export must honor |
| Week 9 | Both with Panos | Wikidata | The first upload |

Meet together with Panos once a week. Meet each other whenever a hand-off is due.

---

## 7. Stretch goals (after week 10)

Pick one. Same shape as everything above: you define the rule and check the data, the
agent builds the code.

- **Batch mode.** Run discovery over all U.S. universities from
  `wikidata_discover/results/universities_us.json`, able to stop and resume. One combined
  review sheet.
- **Faculty linking.** For one department, find the faculty page, extract names and
  titles, match them to existing Wikidata people and ORCID records, link with P108
  (employer). One department first.
- **Review at scale.** If the Prolific pilot says go: a Prolific task per batch, several
  reviewers per row, agreement recorded with every upload.
- **Beyond the U.S.** Make the country a parameter of `harvest`.

---

## 8. Parked (not now)

Good ideas that are not the bottleneck. Do not start them unless Panos asks.

- Direct Wikidata API writes with a bot account (needs Wikidata bot approval).
- Salary data for public-university faculty.
- Packaging as an installable command, continuous integration, type checking.
- Reconciling the IPEDS institution list against Wikidata.

---

## 9. Reference: useful SPARQL queries

Paste these into https://query.wikidata.org.

```sparql
# All children of a university (NYU here)
SELECT ?child ?childLabel ?childTypeLabel WHERE {
  ?child wdt:P749 wd:Q49210 .
  OPTIONAL { ?child wdt:P31 ?childType . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
```

```sparql
# Departments with no parent organization (orphans). Both department classes we use.
SELECT ?dept ?deptLabel WHERE {
  VALUES ?deptClass { wd:Q1183543 wd:Q2467461 }
  ?dept wdt:P31 ?deptClass .
  FILTER NOT EXISTS { ?dept wdt:P749 ?parent }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
```

```sparql
# How many schools and departments each U.S. university has.
# Same university predicate as the harvester (subclasses of university included).
SELECT ?univ ?univLabel (COUNT(DISTINCT ?school) AS ?nSchool) (COUNT(DISTINCT ?dept) AS ?nDept)
WHERE {
  VALUES ?deptClass { wd:Q1183543 wd:Q2467461 }
  ?univ wdt:P31/wdt:P279* wd:Q3918 ; wdt:P17 wd:Q30 .
  OPTIONAL { ?school wdt:P749 ?univ ; wdt:P31 wd:Q31855 .
    OPTIONAL { ?dept wdt:P749 ?school ; wdt:P31 ?deptClass . }
  }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
GROUP BY ?univ ?univLabel
ORDER BY DESC(?nDept)
```

---

## 10. Glossary

- **Wikidata.** A free database of facts that Wikipedia and many others read from. Anyone
  can edit it. We are adding to it.
- **QID.** Wikidata's id for a thing. NYU is Q49210. Stern is Q770467.
- **Property.** Wikidata's name for a kind of fact. P749 means "parent organization".
  P31 means "is a". The ones we use are listed in `AGENTS.md`.
- **Statement.** One fact: item, property, value. "Stern, parent organization, NYU."
- **Reference.** The source attached to a statement: a URL and the date we looked at it.
- **QuickStatements.** A Wikidata tool that takes a text file of statements and adds them
  in bulk. Our exporter writes those files. A person uploads them.
- **Orphan.** A unit that exists in Wikidata but is not linked to its parent.
- **Precision.** Of the units the LLM proposed, the share that were real.
- **Recall.** Of the real units, the share the LLM found.
- **Ground truth.** The correct answer, built by hand, that we score against.
- **Run log.** Our record of everything a run did: every LLM call, every page fetched,
  every unit proposed. Lives in BigQuery and Cloud Storage.
- **Provider.** An LLM company: OpenAI, Anthropic, Google (Gemini).
- **Judge.** An LLM that reviews a merged list from other LLMs and removes what is not real.
- **Prolific.** A website where we pay people to do short review tasks.
