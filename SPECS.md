# SPECS.md

The full build specification for each week of `TASKS.md`. This file is for the coding agent. Students read the short version of each week in `TASKS.md`; the agent builds to the full
version here.

How to use it:

1. The student names a track and a week.
2. Read that week here, and the matching week in `TASKS.md` (its "You check it by" steps are
   the acceptance test).
3. Read the parts of `AGENTS.md` it points to ("Run log", "Hierarchy file", "Running
   collection in the cloud").

Section numbers in this file ("section 11") refer to `TASKS.md`. When this file and `AGENTS.md` disagree, stop and ask. When a week changes, change it here and in `TASKS.md` in the same pull request.



## Anya's track: build the hierarchy

### Anya, week 2 (Oct 9 to 15)

**Record everything.** A run log in BigQuery with four tables: `runs` (one row per command
run), `llm_calls` (every prompt and raw response), `evidence` (every web page fetched),
`candidates` (every unit proposed, with links to the calls and pages that produced it). Big
text goes to Cloud Storage; the row keeps the path. Local JSON files when GCP is unreachable.
Also: `config.py` reads API keys from Secret Manager when `.env` has none.

### Anya, week 3 (Oct 16 to 22)

**What Wikidata already has, and what is missing.** A `snapshot <QID>` command that saves a
university's whole subtree as Wikidata has it today: every unit below it through P749, P361,
P527, P355, P199, crawled one unit at a time over full statements (every rank except
deprecated, with each statement's rank and qualifiers kept, using the per-unit query in section
11), plus every unit's own upward P749 and P361 statements (the parents query in section 11),
so a second parent outside the university's tree is kept too (NYU Shanghai also has P749 to
East China Normal University), with labels, other names, P31, website (P856), ROR id (P6782),
and parents. A one-time import of the `us-tier1` results into `candidates`, marked as legacy:
the cached LLM answers in the bucket become `llm_calls` rows marked legacy (no raw response or
prompt was kept), the evidence ids stay empty, and each row points to the original CSV in the
bucket, so nothing looks more traceable than it is. A gap report over the 1,519 universities:
per university, units on Wikidata, units the LLM proposed, how many linked, orphan, missing,
and how many units the old crowd table (`docs/BACKGROUND.md`) has, where it has the university.

### Anya, week 4 (Oct 23 to 29)

**Departments, with evidence.** The LLM step takes any unit (a university or a school) and
returns its sub-units. Each comes with a name, a type (department, program, center), a website
if known, **one source URL**, and its country with the page or address that shows it (the
parent's country is only a starting guess that must be confirmed, since a unit can sit abroad:
NYU Abu Dhabi). Web search or grounding turned on for Anthropic and Gemini too. Each provider
can also be run on its own (a `--provider` option), so Shuo can compare them on departments;
`discover` keeps using the first provider that answers until week 8. Every cited page is
fetched once and saved: HTTP status, final URL after redirects, content hash, page text, all in
the `evidence` table and linked to the candidate. A unit without a URL is kept and flagged. The
URLs come from an LLM, so the fetcher only follows `http` and `https`, refuses any address that
is not public (localhost, private networks, cloud metadata) on the first request and on every
redirect, and stops after 5 redirects, 5 MB, or 20 seconds. It connects to the exact address it
checked, so a name that resolves to a public address when checked and a private one a moment
later (DNS rebinding) cannot slip through. A refused URL is recorded as refused.

### Anya, week 5 (Oct 30 to Nov 5)

**The hierarchy file.** `discover --depth 2` writes one hierarchy file per university (format
in AGENTS.md): one node per unit with a stable id, parents and whether each link exists on
Wikidata, QID or none, alignment status, evidence, and the run and calls that proposed it.
Every in-scope unit from the week 3 snapshot goes in too, even if no LLM named it, and gets its
departments looked up like any other. So does the Wikidata subtree of any unit that alignment
matches to an existing item outside the snapshot: it is crawled and merged before the file is
written. It also gets the week 4 evidence step for itself (a saved source page, from its
website or a search, and its country), since the snapshot holds only what Wikidata says. Ids
come from a register that keeps a unit's id when it is renamed, moved, or later matched to a
QID. The same nodes go into a `nodes` table in BigQuery. Every level is written to the run log.

### Anya, week 6 (Nov 6 to 12)

**No duplicates.** Before calling a unit "new", search harder: full-text search, not only label
prefix (the prefix search missed "Boston University Wheelock College..."); the name with the
university's name in front; other names and abbreviations; the website domain (P856); items
whose parent is any unit already in the tree. Then an LLM picks one of the candidates or none
(choose from the list, not yes or no per item). Inside our own tree, merge nodes that are the
same unit under two names. When Wikidata itself has two items for one unit, flag it for a
person and never merge it ourselves. Every node records the candidates looked at and the reason
for the choice. Behind a flag, a second way to decide: ask yes or no for each candidate
separately. It is the baseline for research question A1.

### Anya, week 7 (Nov 13 to 19)

**The messy cases.** Code that follows `docs/MODELING_RULES.md`: joint departments get two
parents (with the rank and qualifiers the rules set for each link), same-name departments stay
separate, renamed units are handled the way the rules say, campuses and offices are handled the
way the rules say. One test per rule.

### Anya, week 8 (Nov 20 to 26)

**Twelve universities.** An `--extract` flag that picks the extraction setup (one provider, or
two generators and a judge). Set it to the setup Shuo chose at the end of week 6, then run
depth 2 on all 12 ground-truth universities, written to the run log, with a summary table per
university. Light week, Thanksgiving.

### Anya, week 9 (Nov 27 to Dec 3)

**A correct export.** The QuickStatements exporter reads the hierarchy file and the `reviews`
table. It links with P749 (not P361), puts a reference on every statement from the evidence for
that statement (the page that shows the unit belongs to that parent for each P749, the
country's own source for P17, with the date it was retrieved; the manifest records which),
writes one file per level, and handles each status: **new** gets the minimum statement set,
with a P749 to every parent (both, for a new joint unit) and P17 from the node's own country
(never assumed from the university: NYU Abu Dhabi is not in the U.S.), **orphan** gets only the
missing P749s, with the rank and qualifiers `docs/MODELING_RULES.md` sets for joint units,
plus, for a P749 that exists with the wrong rank or qualifiers, the missing qualifiers added to
it (only when the item has exactly one P749 to that parent; QuickStatements adds qualifiers to
the first matching statement, so with two the whole fix is a hand edit) and a hand-edit line in
the manifest for any rank or qualifier value that differs (QuickStatements cannot change a
rank), **linked** gets no statement, **pending** (an existing unit whose only unlinked parent
is new) waits for `ingest-qids`, **uncertain** is never exported. A "fix" verdict is never
exported directly: the correction is applied to the hierarchy file (a corrected source URL goes
through the fetcher first), which makes a new node version, and that version needs its own
accept. Which verdicts authorize a unit (how many accepts, whether any reject blocks it,
experts only or not) is read from `review_protocol.json`, so the rule Shuo writes in week 10 is
the rule the export applies; the week 9 exporter ships a `review_protocol.json` with the
default (one blind expert accept and no reject), and Shuo's week 10 protocol replaces it.
Accepts are counted per distinct reviewer, never per row: one person's blind and second-pass
accepts count once. A reviewer's latest blind verdict is what counts toward the accepts; a
reject or a fix from that reviewer in either pass blocks (a fix means only the corrected
version can be accepted). At least one accept must come from an expert, whatever the protocol
says, because Prolific workers judge only the claim and never see the QID, country, type,
website, rank, or qualifiers. If the file is missing or invalid, the export refuses to run.
Next to each QuickStatements file it writes a manifest: for every statement, the node_id,
node_version, and review ids that authorize it, and for every in-scope node of that level not
exported, the reason (already linked, rejected, uncertain, not enough verdicts, parent not yet
uploaded). It refuses any unit without a human verdict on its current version: a verdict given
to an earlier version of the unit (different name, parent, QID, alignment candidates, or
evidence) does not count. A joint unit gets one P749 for each parent link that is missing. A
department whose parent school is new is held back: after the school file is uploaded, an
`ingest-qids` command records the schools' new QIDs (each confirmed by a person) and reruns
alignment for their departments, so a department that already had a QID now shows its link to
the new school as missing; only then is the department file written. Check this with one
existing department under a new school: the department file adds its P749. And with one
existing joint unit whose second P749 lacks its qualifiers: the file adds them, and a wrong
rank shows up in the manifest as a hand edit.

### Anya, week 10 (Dec 4 to 10)

First, fixes the upload needs. Then **page first**, for research question A2: an
`--extract page-first` mode that finds the unit's own page listing its sub-units (on the unit's
website, else by search), saves it, and extracts units from the saved text only, each citing
that page.

### Anya, week 11 (Dec 11 to 17)

Cleanup. All tests pass. Boxes ticked in this file.

## Shuo's track: check the hierarchy

### Shuo, week 2 (Oct 9 to 15)

**A scorer.** Loads a department ground-truth CSV (one row per department: parent, name, type,
`source_url`, `qid`) and scores any list against it: precision and recall on names, and
alignment accuracy on QIDs (right QID, or rightly "new"). When parents are given, also edge F1
(right parent) and ancestor F1 (right university). Matching is one to one and within the same
parent: one proposed name can match only one true department, a repeated name counts once, and
two departments with the same name under different schools are different rows.

### Shuo, week 3 (Oct 16 to 22)

**Parallel extraction: do independent LLMs agree?** Each provider lists the schools of the 12
eval universities and of the 6 small universities below (as soon as their ground truth is in)
on its own, 3 runs each, through the existing extractors. Each run gets a sample number that is
part of its cache key, so the three runs are three real calls (not one call read back from the
cache three times) and a rerun replays the same three answers. Name variants of one school are
grouped ("Dept. of CS" and "Computer Science"). For each school proposed, one row in a new
`checks` table: how many providers and runs named it. Plus a table: precision at each agreement
level, and how often provider B names a wrong school when provider A did. Plus research
question S1 (section 8): from the overlap between providers, estimate how many schools each
university has, and compare with the true count. Compute it two ways: counting every grouped
proposal (the version that works on any university, where invented units inflate the count),
and counting only proposals that match a real school in the ground truth (what a perfect
verifier would give). The ground truth is otherwise used only to score the estimates. Report
each provider's invented and non-academic proposals separately. In week 5, add a third way that
works without ground truth: count only proposals whose page check passes.

### Shuo, week 4 (Oct 23 to 29)

**The LLM judge, tested.** Each provider takes a turn as judge of the merged list. Each judge
runs four times: twice with the list in one order and twice in another (each call its own cache
entry, as in week 3). Report: how often a verdict flips between two runs in the same order
(ordinary randomness) and between orders; the order effect is the difference, whether a judge
keeps its own provider's schools more often than others' at the same correctness, and whether
the judge's gain is in precision (removing wrong ones) or recall (keeping right ones). Then the
same on departments: each provider lists the departments of the six ground-truth schools on its
own, through Anya's week 4 step with `--provider` (not the first-answer fallback, which gives
one list), 3 runs each with sample numbers, so every possible generator has its own list to
score and to judge.

### Shuo, week 5 (Oct 30 to Nov 5)

**Does the page exist and name the unit?** Over Anya's `evidence` table: dead links, pages that
say "not found" with status 200, pages off the university's domain, and whether the unit's name
appears on the page (allowing small differences). Your week 3 lists came before Anya turned on
search for Anthropic and Gemini (Anya's week 4), so first rerun each provider on the same
universities, and on the departments of the six ground-truth schools, with search on, then run
Anya's week 4 fetcher over the URLs each provider cited, so every provider has saved pages to
count. Report search-off and search-on separately; the setup choice in week 6 uses search-on.
Also on 300 random school URLs from `us-tier1` (almost all OpenAI, reported separately). Each
result is a row in `checks`.

### Shuo, week 6 (Nov 6 to 12)

**LLM verifier.** Given a claim and the saved page, answer supported, not supported, or
unclear, and quote the sentence that decides it. Long pages are split into fixed, overlapping
pieces that fit the model; the claim is supported if any piece supports it, not supported if
every piece was read and none supports it (a wrong page that never mentions the unit is not
supported, as in your week 5 labels), and unclear only when the page could not be read or the
deciding text is ambiguous. If the quote is not in the saved page word for word, the answer
becomes unclear. Run it with one provider's model and, for cost, with one small checker
(MiniCheck). MiniCheck answers only supported or not, with a probability, and gives no quote,
so it is scored as a yes-or-no checker.

### Shuo, week 7 (Nov 13 to 19)

**One confidence per unit, and a review sheet.** Combine the checks into one confidence per
node of a hierarchy file, using only checks recorded against the node's current version. The
agreement and judge checks from weeks 3 and 4 were made on candidates, before any node existed,
so they are recomputed for each node (from the candidates it came from) and recorded against
its current version first (start with a simple rule, such as "all providers agree and the page
supports it" = high). A `review` command that writes one row per node: parent, name, type, QID
or new, the Wikidata items Anya's alignment considered, country and where it came from, the
English description and website a new item would get, source URL. Every value the export would
write is on the row, including, for each parent link, its rank, qualifiers, and the page that
supports it. Two passes: in the first, rows are in random order and the checks and confidence
are hidden, and the reviewer records a verdict; only then are the checks shown, sorted doubtful
first, and the reviewer may record a second verdict. Columns to fill: `verdict` (accept,
reject, fix), one correction column per field a reviewer can fix (one `fix_` column for every
value shown on the row, including, per parent link, its target, rank, qualifiers, and source).
A correction to a fact that is exported with its own reference (the source, the country, a
parent link) must come with a URL for it, and `notes`. An accept needs a `url_checked` of yes
for every evidence page on the row (the unit's source, the country's source, each parent link's
source), recorded page by page: the reviewer opened it and it supports its fact. A fix records
yes or no per page, so a dead or wrong page can be marked no, and comes with the corrected URL;
that page is fetched into the new version, which needs its own accept with every page yes. Each
verdict is saved as its own row in the `reviews` table (one per reviewer per unit), never
overwriting another reviewer's, and records the run and the node version it was given (see
AGENTS.md), plus which pass it was and a hash of the whole row as shown, so a verdict always
says exactly what the reviewer saw.

### Shuo, week 8 (Nov 20 to 26)

**A Prolific task, ready to launch.** One parent link per item: the unit's name, one of its
parents, and the saved page as a picture (not copyable text). Answer accept, reject, or fix.
The worker judges only the claim and the page ("this page shows X is a unit of Y"); whether the
QID or "new" is right stays with experts, because it needs Wikidata searching that a short paid
task cannot ask for. One qualification question. Gold items made by corrupting units you
checked (wrong parent, a fake unit, the wrong page). Two versions: one shows the verifier's
answer, one does not. Each worker is assigned to one version at random and only ever sees that
one; a worker who took either version is excluded from the other (Prolific's exclusion list),
so no blind answer comes from someone who has seen the verifier's answers. The instructions ask
workers directly not to use AI tools. A script that computes agreement with your verdicts.
Light week, Thanksgiving.

### Shuo, week 9 (Nov 27 to Dec 3)

**Prolific pilot, and the quality estimate.** Draw a random sample of about 150 parent links
from Anya's 12-university run: one item per unit and parent, so the two links of a joint unit
are two items, each judged on its own. Launch with 3 workers per item that has a saved page in
the version that hides the verifier's answer, plus a second set of workers on the same items in
the version that shows it. Workers see only items with a saved page, so every worker analysis
uses that subset and says how many items it has: worker accuracy against your blind "supported"
labels; Dawid-Skene against simple majority; the effect of showing the verifier's answer.
Research question S2 (section 8): precision of all 12 hierarchies (the share of parent links
that are true) estimated from the human sample alone, and from the human sample plus verifier
labels on every link. Precision uses your "true" label, not page support: a real unit with a
dead or poor page is still true, and page support is reported as its own rate. The accuracy of
QIDs and "new" is a property of units, not links, so it is computed over the distinct units in
the sample, each weighted by the inverse of its chance of being drawn (a joint unit with two
links had about twice the chance), and the report says how many distinct units that is. Only
verdicts from the version that hides the verifier's answer go into these estimates; the other
version is used only to measure anchoring. Sampled items with no saved page (no URL, dead, or
refused) stay in the sample: workers do not see them, the verifier counts them as not
supported, your labels decide whether they are true, and the report gives their share.

### Shuo, week 10 (Dec 4 to 10)

**Pre-upload check.** A command that reads an export file and its manifest, and checks them
against the hierarchy file, the `reviews` table, `review_protocol.json` (refusing to run if
that file is missing or invalid), and the complete review of the chosen university on its
current versions (every unit reviewed after the last alignment refresh): if the share of units
accepted without correction is below the protocol's minimum, or any current unit lacks a
verdict, it refuses. This is exact, not an estimate, so it stays valid after the refresh. The
week 9 pooled estimate is recorded with the batch for context but does not gate it, because it
was measured on earlier versions. It first regenerates the export from the hierarchy file, the
reviews, and the protocol, and refuses the file if it differs in any statement, value, rank,
qualifier, or reference. It also refuses it if any statement lacks a reference, has no manifest
line, or has no accepting verdict on the node's current version, or if an in-scope node is
missing from both the file and the manifest's list of nodes left out.

### Shuo, week 11 (Dec 11 to 17)

Cleanup. All tests pass. Boxes ticked in this file.
