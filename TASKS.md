# TASKS.md

**Goal.** Make Wikidata the definitive source for how universities are organized:
university > school > department, with faculty linked to their departments.

This file is the project's plan. It is written for a student who is picking the project
up fresh and who will direct a coding agent rather than write most of the code. Read it
top to bottom once, then come back to it every week.

Last updated: 2026-10-02

---

## 1. What exists today

In plain terms, the code can do one thing well: **given a university, find its schools
and colleges.**

```
python -m wikidata_discover.scripts.wikidata_division_discover discover Q49210
```

That command:

1. Looks up the university on Wikidata (name, website, existing child units).
2. Asks an LLM to list the university's schools and colleges. It tries OpenAI first
   (with web search), and falls back to Anthropic, then Gemini (without web search) only
   if the earlier one fails or returns nothing.
3. (Not yet in this command, see below.) The ensemble that runs several LLMs and has one
   judge the merged list exists in the evaluation code, not in `discover`.
4. Checks each school against what Wikidata already has (fuzzy name matching, then an
   LLM tie-breaker when names are ambiguous).
5. Labels each school as already linked, existing but not linked to the university
   (an orphan), or missing from Wikidata entirely.
6. Writes the missing ones to a CSV and a QuickStatements file in `wikidata_discover/results/`,
   plus a small JSON report in `wikidata_discover/results/reports/`.

**How good is it?** We hand-built the true list of schools for 12 universities
(`wikidata_discover/eval/ground_truth.py`) and measured. The best configuration
(Anthropic as judge over OpenAI + Gemini) reaches about 95% precision and 95% recall.
Full numbers are in `wikidata_discover/eval/results_summary.csv`. Rerun with:

```
python -m wikidata_discover.eval.run_eval
```

**What does not exist yet:**

- The `discover` command does not use the ensemble that the evaluation found best. It
  uses one provider. Wiring the ensemble in is part of Milestone 2.
- Web search is only on for OpenAI. Anthropic and Gemini answer from memory, so source
  URLs they return are not verified.
- Going one level deeper (school > department). The LLM prompt only knows about schools.
- Running over many universities at once.
- Anything about faculty.
- Nothing has been uploaded to Wikidata yet. All output is files on disk.

For where the project came from and why we model things the way we do, read
`docs/BACKGROUND.md` once. It is short.

---

## 2. How to work on this project

You will not write most of the code yourself. You direct a coding agent (Claude Code or
similar) that works inside this repository. The agent reads `AGENTS.md` for conventions,
and it reads this file for what to do next.

Your job is the part the agent cannot do:

- **Decide what to build and in what order.** This file is the plan. Keep it current.
- **Check the result.** Run the command, open the output file, look at the data. Never
  accept "done" from the agent without seeing it work yourself.
- **Judge the data.** Is this really a department? Is this the same unit under a new name?
  The agent will guess. You know, or you can find out.
- **Own what goes into Wikidata.** Nothing is uploaded until a person has reviewed it.

Working rules that keep the project manageable:

1. One milestone per branch and pull request. Small, reviewable steps.
2. Start each agent session by pointing it at this file and naming the milestone.
3. Give the agent the "done when" line verbatim. It is the acceptance test.
4. Ask the agent for a short plan before it writes code. Read it. Push back if it is
   doing more than the milestone asks.
5. At the end of each session, have the agent tick the boxes in this file, add anything it
   learned to `AGENTS.md`, and summarize what changed in the pull request description.
6. If the agent says tests pass, run them yourself: `python -m pytest tests -q`.
7. Keep a running log for Panos: date, what was attempted, what worked, what you decided.

---

## 3. Your first week: get set up

- [ ] Clone the repo. Ask the agent to install dependencies and run the tests. All of them
      should pass (25 at the time of writing).
- [ ] Get API keys from Panos (or from GCP Secret Manager, see AGENTS.md) and put them in
      `.env`, copying `env.example`. At least one of `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
      or `GOOGLE_API_KEY` is required. Prefer OpenAI, the only one with web search today, and
      set all three for the evaluation harness. Never commit `.env`.
- [ ] Run discovery on NYU (Q49210) and open the CSV it produces in `wikidata_discover/results/`.
- [ ] Run it on a university you know well. Is the list of schools right? Note what is wrong.
- [ ] Ask the agent to walk you through the pipeline, step by step, using `discovery.py`
      as the guide. Then ask it the question that confused you most.
- [ ] Run the evaluation and compare its numbers to `wikidata_discover/eval/results_summary.csv`:
      `python -m wikidata_discover.eval.run_eval`
- [ ] Write one paragraph for Panos: what the pipeline does, and one thing you would change.

---

## 4. The work, in order

Each milestone lists what you ask the agent to build, what you do yourself, and a "done
when" line you can check without reading code. Do them in order. Milestones 1 through 6
are the core of a 10-week project. Milestone 7 is the goal. Later items are stretch goals.

One rule runs through all of them: **nothing goes into Wikidata without a human looking
at it first.** The LLMs propose, the code checks, a person decides.

### Milestone 1: Build the department ground truth

Before changing anything, we need a way to know whether a change helped. This milestone
is mostly your work, not the agent's.

**You do:**
- [ ] Pick 3 NYU schools (suggested: Stern, Courant, Steinhardt).
- [ ] Ask the agent to pull what the old crowdsourced dataset has for those schools from
      BigQuery table `nyu-datasets.academiabot.organization` (see `docs/BACKGROUND.md`).
      It has 7 Stern departments, 3 for Courant, and 11 for Steinhardt. Use it as a
      starting list, not as the answer. It is several years old and mixes in programs,
      administrative offices, and abbreviations.
- [ ] From each school's own website, list its real departments. Record the name, the
      URL where you found it, and whether it is already in Wikidata (search wikidata.org;
      if found, record the QID).
- [ ] Save as a CSV in `wikidata_discover/eval/`.

**Agent builds:**
- [ ] A loader for your CSV in the same style as the school-level `ground_truth.py`.
- [ ] A command that scores any list of department names against your ground truth and
      prints precision and recall.

**Done when:** You can hand the scoring command a list of department names for Stern and
get back a precision and recall number, and you agree with how it counted.

### Milestone 2: Make the LLM extraction work at any level

Today the LLM prompts only know how to ask "what are the schools of this university?".
We need "what are the sub-units of this unit?".

**Agent builds:**
- [ ] Extraction functions that take any parent unit (name, website, QID) and the kind
      of children wanted (schools, departments, programs), and return for each child a
      name, a website if known, a source URL, and a proposed type. One source URL per
      unit (today the schema asks for one per response and then discards it). A unit
      with no source URL is kept but flagged.
- [ ] The response cache keyed by parent and level, so reruns are free.
- [ ] `discover` wired to the evaluated ensemble (two generators plus a judge) instead of
      the first provider that answers, with a flag to pick the judge.
- [ ] Web search or grounding turned on for the Anthropic and Gemini calls too, so every
      cited source URL comes from a real search result.

**You do:**
- [ ] Run extraction on each of your 3 schools. Score against your ground truth.
- [ ] Read the raw LLM output for one school. Are the source URLs real? Click five.
- [ ] Decide which provider or judge combination to use for departments. It may differ
      from the school-level answer.

**Done when:** Extraction on Stern scores above 80% precision and recall against your
ground truth, and you have checked that cited source URLs point to real pages.

### Milestone 3: Recursive discovery

**Agent builds:**
- [ ] A `--depth N` option on the `discover` command. Depth 1 is today's behavior.
      Depth 2 adds departments under each school. Depth 3 adds programs and centers.
- [ ] The same matching steps at every level (fuzzy match against existing Wikidata
      children, then the LLM tie-breaker for ambiguous names).
- [ ] A nested JSON output of the whole tree, and QuickStatements output where each
      department's proposed type maps to the right Wikidata class (table in AGENTS.md)
      and the parent link uses P749, not P361 as the exporter does today.
- [ ] Every exported statement carries a reference: reference URL (S854) and retrieved
      date (S813). The exporter refuses to emit a statement that has no source URL.

**You do:**
- [ ] Run `discover --depth 2 Q49210`. Open the tree. Spot-check 10 departments: right
      school? right type? already in Wikidata or not?
- [ ] Open the QuickStatements file. Paste 3 lines into the QuickStatements tool in
      preview mode (do not run) to confirm the syntax is accepted.

**Done when:** Depth-2 discovery on NYU runs end to end, the tree looks right to you on a
spot check, and the QuickStatements preview accepts the file.

### Milestone 4: Handle the messy cases

Real universities are not clean trees. This milestone is about decisions, and the
decisions are yours.

**You do:**
- [ ] Collect real examples as you hit them: a department in two schools (joint), two
      departments with the same name in different schools, a unit that was renamed or
      merged, a "school" that is really a program.
- [ ] For each, decide the rule. Write the rules in `docs/MODELING_RULES.md` in plain
      language with the examples. Check them with Panos.

**Agent builds:**
- [ ] Code that follows your rules (for example two P749 statements for a joint department).
- [ ] A test for every example you collected.

**Done when:** `docs/MODELING_RULES.md` exists with at least 5 real cases, each has a
test, and the tests pass.

### Milestone 5: Measure across many universities

**Agent builds:**
- [ ] The evaluation harness extended to report precision and recall per level (schools
      vs. departments) and per LLM provider.
- [ ] A results table saved in `wikidata_discover/eval/` for depth-2 discovery on the 12 ground-truth universities.

**You do:**
- [ ] For universities with no department ground truth, take a random sample of 20
      proposed departments each and check them against the university website.
- [ ] Write down the top 3 ways the pipeline fails. Ask the agent to fix the fixable ones.
      Record the rest as known issues in this file.

**Done when:** A results table for 12 universities exists, your spot-check numbers are
recorded next to it, and the top failure modes are fixed or written down.

### Milestone 6: Human review before upload

The pipeline is about 95% accurate at the school level and will be worse for departments.
The remaining errors have to be caught by a person. This milestone builds the tool for that
and then uses it.

**Agent builds:**
- [ ] A `review` command that takes a discovery run and writes a review sheet (CSV, or a
      Google Sheet if that is easier to share). One row per proposed statement: parent
      unit, proposed name, proposed type, website, the source URL the LLM cited, which
      providers agreed. Three empty columns for the reviewer: `verdict` (accept / reject /
      fix), `corrected_value`, `notes`.
- [ ] Doubtful rows sorted first: found by only one provider, no source URL, or a
      borderline fuzzy-match score.
- [ ] QuickStatements export that reads the review sheet and emits only accepted or fixed
      rows. Rejected rows are remembered so they are never proposed again.

**You do:**
- [ ] Review NYU's department proposals. Time yourself. An "accept" or "fix" verdict
      requires a URL on the row where you saw the fact. If the LLM's URL is wrong, replace it.
- [ ] Have Panos review the same sheet independently. Compare verdicts. Every
      disagreement is a sentence that belongs in the review guide.
- [ ] Write the one-page review guide: what counts as a department, how to handle renamed
      or merged units, when to reject. Save as `docs/REVIEW_GUIDE.md`.
- [ ] Record the acceptance rate per run. That is the true precision of what we publish.

**Done when:** A second person can take a review sheet and the guide, review 50 rows in
under 30 minutes without asking questions, and the export honors their verdicts.

### Milestone 7: Upload to Wikidata

This is what the whole project is for. Everything before this is preparation.

**You do:**
- [ ] Pick the one or two universities with the cleanest reviewed results.
- [ ] Export the accepted rows. Read every line of the QuickStatements file. It will be
      short enough.
- [ ] Upload through the QuickStatements web tool, under your own Wikidata account.
      Record the batch ID and date here.
- [ ] Query Wikidata back with SPARQL (queries at the bottom of this file) to confirm the
      hierarchy is visible.
- [ ] Watch the items for two weeks. If a Wikidata editor reverts or changes anything,
      find out why. That is the most valuable feedback we can get.

**Agent builds:**
- [ ] `docs/UPLOAD_PROCEDURE.md`: the steps above, written so the next person can repeat them.

**Done when:** At least one university's departments are live on Wikidata, and the
procedure is written down.

---

## 4b. Evaluation track (Shuo): sources, LLM validation, human review

This track runs in parallel with the milestones above and feeds them. Its owner is
responsible for one question: **for every fact we propose, how do we know it is true?**
Three answers, built in this order: a web page says so, an LLM confirms the page says so,
a person confirms it. Nothing enters Wikidata without the third.

Weekly plan, Oct 2 to Dec 10. Each week names what you ask the agent to build, what you
do by hand, and what you hand to Panos.

| Week | Dates | Agent builds | You do by hand | Deliverable |
|---|---|---|---|---|
| 1 | Oct 2 to 8 | Nothing yet. Setup only. | Install, run tests, run the eval harness, read `docs/BACKGROUND.md` and the 12-university ground truth. | One paragraph: what the eval harness measures and what it does not. |
| 2 | Oct 9 to 15 | A loader for a department ground-truth CSV with a `source_url` column (Milestone 1). | Build the ground truth for Stern, Courant, Steinhardt from their websites, one URL per department. Pull the old BigQuery rows as a starting list. | Ground-truth CSV, about 40 to 60 rows, every row with a URL. |
| 3 | Oct 16 to 22 | **Source checker.** Given a unit name and a URL: fetch the page, record HTTP status, and report whether the name (normalized) appears in the page text. Store a snapshot of the page. | Run it on every URL the LLMs cited for the 12 universities. Read 20 failures and classify them: dead link, wrong page, right page but name differs, hallucinated URL. | Table: share of cited URLs that exist and support the claim, per provider. |
| 4 | Oct 23 to 29 | **LLM verifier.** Given a claim ("X is a department of Y") and the page text, return supported / not supported / unclear with a quoted passage. | Label 100 claim-page pairs yourself, then compare the verifier's answers to yours. | Verifier accuracy against your labels, with the confusion matrix. |
| 5 | Oct 30 to Nov 5 | Eval harness extended to department level and per provider, plus the judge configurations (Milestone 5). | Decide, with numbers, which generator and judge combination to use for departments. | Midpoint report to Panos: precision, recall, source-support rate, verifier accuracy. |
| 6 | Nov 6 to 12 | **Review sheet and export** (Milestone 6): review command, doubtful rows first, URL required for accept, export honors verdicts. | Review NYU's department sheet. Panos reviews the same sheet independently. Compare. | `docs/REVIEW_GUIDE.md`, one page, written from the disagreements. |
| 7 | Nov 13 to 19 | A Prolific task template for the review sheet: 50 rows, qualification question, two workers per row, and a script that computes agreement with your verdicts. | Run the pilot on Prolific. Budget guide: about 20 cents per entity in the old project. | Pilot results: agreement with expert verdicts, time per row, cost per row, and a go or no-go on scaling. |
| 8 | Nov 20 to 26 | Fixes for the top failure modes found in weeks 3 to 7. Light week, Thanksgiving. | Write the review protocol: who reviews what, how many reviewers, what agreement is required. | `docs/REVIEW_PROTOCOL.md`. |
| 9 | Nov 27 to Dec 3 | QuickStatements references (S854, S813) on every statement. A pre-upload check that refuses any statement without a human-checked URL. | Verify every row of the first upload batch (Milestone 7) with the other student and Panos. Watch the uploaded items for reverts. | The first referenced, human-verified batch live on Wikidata. |
| 10 | Dec 4 to 10 | Nothing new. Cleanup and tests. | Write the final report. | Report: all the numbers above in one place, plus what the next person should do first. |

**Done when:** Every statement in the first Wikidata upload has a URL that a person has
checked, the acceptance rate and inter-reviewer agreement are recorded, and a second
person could run the review process from the two docs alone.

**Dependencies on the other track.** Week 5 needs depth-2 discovery output (Milestone 3).
If it is late, run the department eval on the three ground-truth schools only. Week 9
needs the exporter rework (Milestone 3). If it is late, add references with a small
post-processing script and fold it into the exporter afterwards.

---

## 5. Stretch goals (after Milestone 7)

Pick one. Each is a self-contained project with the same shape: you define the rule and
check the data, the agent builds the code.

- **Batch mode.** Run discovery over all U.S. universities from `wikidata_discover/results/universities_us.json`,
  with the ability to stop and resume. Produce one combined review sheet.
- **Faculty linking.** For one department, find the faculty page, extract names and
  titles, and match them to existing Wikidata people and ORCID records. Link via P108
  (employer). Start with one department before generalizing.
- **Automated checks before upload.** A checker that rejects a QuickStatements file if
  any line is malformed, points to a nonexistent QID, or would create a duplicate.
- **Scale up human review.** If the Prolific pilot (evaluation track, week 7) says go,
  run review at scale: a small web page or a Prolific task per batch, several reviewers
  per row, agreement measured and recorded with every upload.
- **Beyond the U.S.** Make the country a parameter of `harvest`.

---

## 6. Parked (not now)

These were on earlier versions of this list. They are good ideas but not the bottleneck.
Do not start them unless Panos asks.

- Storing results in BigQuery and caches in Google Cloud Storage instead of local files.
- Running the pipeline as Cloud Functions or Cloud Run with a scheduler.
- Direct Wikidata API writes with a bot account (requires Wikidata bot approval).
- Salary data for public-university faculty.
- Packaging as an installable CLI, CI pipeline, type checking.
- Reconciling the IPEDS institution list against Wikidata.

GCP project `wikidata-academia` is already set up with permissions for all of this.
Details are in AGENTS.md.

---

## 7. Reference: useful SPARQL queries

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
