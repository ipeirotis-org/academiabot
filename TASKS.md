# TASKS.md

**Goal.** Make Wikidata the definitive source for how universities are organized:
university > school > department, with faculty linked to their departments.

This file is the project's to-do list. It is written for a student who is picking the
project up fresh. Read it top to bottom once, then come back to it every week.

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
2. Asks several LLMs (OpenAI, Anthropic, Gemini), each with web search, to list the
   university's schools and colleges.
3. Has one LLM act as a judge to merge the lists and drop hallucinations.
4. Checks each school against what Wikidata already has (fuzzy name matching, then an
   LLM tie-breaker when names are ambiguous).
5. Labels each school as already linked, existing but not linked to the university
   (an orphan), or missing from Wikidata entirely.
6. Writes the missing ones to a CSV and a QuickStatements file in `wikidata_discover/results/`.

**How good is it?** We hand-built the true list of schools for 12 universities
(`wikidata_discover/eval/ground_truth.py`) and measured. The best configuration
(Anthropic as judge over OpenAI + Gemini) reaches about 95% precision and 95% recall.
Full numbers are in `wikidata_discover/eval/results_summary.csv`. Rerun with:

```
python -m wikidata_discover.eval.run_eval
```

**What does not exist yet:**

- Going one level deeper (school > department). The LLM prompt only knows about schools.
- Running over many universities at once.
- Anything about faculty.
- Nothing has been uploaded to Wikidata yet. All output is files on disk.

---

## 2. Your first week: get set up

- [ ] Clone the repo and install dependencies: `pip install -r wikidata_discover/requirements.txt pytest`
- [ ] Copy `env.example` to `.env` and add API keys. Ask Panos for the keys, or pull them
      from GCP Secret Manager (see AGENTS.md, section "Secret Manager").
      You need at least `OPENAI_API_KEY`. For the full ensemble you also need
      `ANTHROPIC_API_KEY` and `GOOGLE_API_KEY`.
- [ ] Run the tests: `python -m pytest tests -q`. All 22 should pass.
- [ ] Run discovery on NYU (Q49210) and read the output CSV.
- [ ] Run discovery on a university you know personally. Does the list look right?
- [ ] Read these four files, in this order. They are the whole pipeline:
      `cli.py` -> `discovery.py` -> `llm_helpers.py` -> `to_qs_wikidata.py`
- [ ] Write one paragraph for Panos: what the pipeline does, and one thing that confused you.

---

## 3. The work, in order

Each milestone has a "done when" line. Do them in order. Milestones 1 through 6 are the
core of a 10-week project. Milestone 7 is the goal we are aiming for. Later items are
stretch goals.

One rule runs through all of them: **nothing goes into Wikidata without a human looking
at it first.** The LLMs propose, the code checks, a person decides.

### Milestone 1: Build the department ground truth

Before changing any code, we need a way to know whether a change helped.

- [ ] Pick 3 NYU schools (suggested: Stern, Courant, Steinhardt).
- [ ] For each, list its real departments from the school's own website.
- [ ] For each department, record whether it is already in Wikidata and, if so, its QID.
- [ ] Save as a CSV next to `ground_truth.py`, and add a Python structure for it in the
      same style as the existing school-level ground truth.

**Done when:** `ground_truth.py` has department lists for 3 schools, and a one-line
command can score a department list against it.

### Milestone 2: Make the LLM extraction work at any level

Today `extract_divisions_*` in `llm_helpers.py` is hard-coded to "top-level units of a
university". Make it general.

- [ ] Change the functions to take a parent entity (name, website, QID) and the kind of
      children wanted (schools, departments, programs).
- [ ] Adjust the prompts so the LLM returns, for each child, a name, a website if known,
      and a proposed type (school / department / program / center / lab).
- [ ] Keep the cache working. The cache key must include the parent and the level.
- [ ] Run it on the 3 schools from Milestone 1 and score the results.

**Done when:** Running extraction on Stern's QID returns a department list that scores
above 80% precision and recall against the ground truth.

### Milestone 3: Recursive discovery

- [ ] Add `--depth N` to the `discover` command. Depth 1 is today's behavior (schools only).
      Depth 2 adds departments under each school. Depth 3 adds programs and centers.
- [ ] At each level, reuse the same matching steps that already exist for schools:
      fuzzy match against existing Wikidata children, then `choose_match` for ambiguous cases.
- [ ] Output the whole tree, not just a flat list. A JSON file with nesting is fine.
- [ ] Update `to_qs_wikidata.py` so the proposed type from Milestone 2 maps to the right
      Wikidata class (P31). The mapping table is in AGENTS.md under "Data model".

**Done when:** `discover --depth 2 Q49210` runs end to end and produces a QuickStatements
file for NYU's missing departments, with each department pointing to its school via P749.

### Milestone 4: Handle the messy cases

Real universities are not clean trees. Collect examples as you hit them.

- [ ] Joint departments that belong to two schools. Model with two P749 statements.
- [ ] Two departments with the same name in different schools (e.g., two "Economics").
      The matcher must not merge them.
- [ ] Units that recently moved or were renamed. Decide on a rule and document it.
- [ ] Add a test for each case to `tests/`.

**Done when:** The tests pass, and there is a short note in AGENTS.md on how joint units
are represented.

### Milestone 5: Measure across many universities

- [ ] Run depth-2 discovery on the 12 universities in the existing ground truth.
- [ ] For universities without department ground truth, have a person spot-check a random
      sample of 20 proposed departments each. This is a preview of Milestone 6.
- [ ] Extend the evaluation harness (`eval/run_eval.py`) to report precision and recall
      per level (schools vs. departments) and per LLM provider.
- [ ] Write down the top 3 ways it fails. Fix the ones that are fixable.

**Done when:** A results table for 12 universities exists in `eval/`, and the top failure
modes are either fixed or documented as known issues in this file.

### Milestone 6: Human review before upload

The LLM pipeline is about 95% accurate at the school level and will be worse for
departments. The remaining errors have to be caught by a person. This milestone builds
the tool for that.

- [ ] Add a `review` command that takes a discovery run and writes a review sheet (CSV,
      or a Google Sheet if that is easier to share). One row per proposed statement:
      parent unit, proposed name, proposed type, website, the source URL the LLM cited,
      and which providers agreed on it. Leave three empty columns for the reviewer:
      `verdict` (accept / reject / fix), `corrected_value`, and `notes`.
- [ ] Sort the sheet so the doubtful rows come first: proposals that only one provider
      found, that have no source URL, or whose fuzzy-match score was borderline.
- [ ] Make the QuickStatements export read the review sheet and emit only rows marked
      accept or fix. Rows marked reject are kept in a file so they are not proposed again.
- [ ] Review NYU's department proposals yourself, then have Panos review the same sheet.
      Compare: where you disagree tells us what the review instructions need to say.
- [ ] Write a one-page review guide: what counts as a department, how to handle renamed
      or merged units, when to reject. Save it as `docs/REVIEW_GUIDE.md`.
- [ ] Use the review results as measurement: the share of accepted rows is the true
      precision of the pipeline on data we are about to publish. Record it per run.

**Done when:** A second person can take a review sheet and the guide, review 50 rows in
under 30 minutes without asking questions, and the export honors their verdicts.

### Milestone 7: Upload to Wikidata

This is what the whole project is for. Everything before this is preparation.

- [ ] Pick the one or two universities with the cleanest reviewed results.
- [ ] Export the accepted rows to a QuickStatements file. Spot-check ten lines by hand.
- [ ] Upload through the QuickStatements web tool. Record the batch ID and date here.
- [ ] Check the result on Wikidata. Query it back with SPARQL to confirm the hierarchy
      is visible (see the queries at the bottom of this file).
- [ ] Watch the items for two weeks. If a Wikidata editor reverts or changes anything,
      note why. That is the most valuable feedback we can get.

**Done when:** At least one university's departments are live on Wikidata, and the upload
steps are written down so the next person can repeat them.

---

## 4. Stretch goals (after Milestone 7)

Pick one. Each is a self-contained project.

- **Batch mode.** Run discovery over all U.S. universities from `results/universities_us.json`,
  with the ability to stop and resume. Produce one combined QuickStatements file.
- **Faculty linking.** For one department, find the faculty page, extract names and
  titles, and match them to existing Wikidata people and ORCID records. Link via P108
  (employer). Start with one department before generalizing.
- **Automated checks before upload.** Write a checker that rejects a QuickStatements file
  if any line is malformed, points to a nonexistent QID, or would create a duplicate.
- **Scale up human review.** Turn the review sheet into a small web page so several
  reviewers can work in parallel, and measure agreement between them.
- **Beyond the U.S.** Make the country a parameter of `harvest`.

---

## 5. Parked (not now)

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

## 6. Reference: useful SPARQL queries

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
# Departments with no parent organization (orphans)
SELECT ?dept ?deptLabel WHERE {
  ?dept wdt:P31 wd:Q1183543 .
  FILTER NOT EXISTS { ?dept wdt:P749 ?parent }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
```

```sparql
# How many schools and departments each U.S. university has
SELECT ?univ ?univLabel (COUNT(DISTINCT ?school) AS ?nSchool) (COUNT(DISTINCT ?dept) AS ?nDept)
WHERE {
  ?univ wdt:P31 wd:Q3918 ; wdt:P17 wd:Q30 .
  OPTIONAL { ?school wdt:P749 ?univ ; wdt:P31 wd:Q31855 .
    OPTIONAL { ?dept wdt:P749 ?school ; wdt:P31 wd:Q1183543 . }
  }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
GROUP BY ?univ ?univLabel
ORDER BY DESC(?nDept)
```
