# What the research literature says

A short review of the work most relevant to each step of AcademiaBot, written to justify
the design in `TASKS.md` and the research questions in its section 8. Every citation was
checked against the publisher page, the proceedings, or arXiv in October 2026. Entries
marked "partly checked" had one detail (venue or a number) we could not confirm.

Each entry gives the finding and what it means for us. Read the part for your stream:
section 1 for Anya, section 2 for Shuo. Section 3 is the summary for both.

---

## 1. Building the hierarchy (Anya)

### 1.1 How complete are lists that LLMs produce?

- **Petroni et al. (2019).** "Language Models as Knowledge Bases?" EMNLP-IJCNLP.
  https://aclanthology.org/D19-1250/
  - Finding: pretrained models hold a lot of relational knowledge, but much more for some
    kinds of fact than others. The paper tests one fact at a time.
  - For us: it is the founding citation. It does not tell us how complete a list is.
- **Hu, Nguyen, Ghosh, Razniewski (2025).** "Enabling LLM Knowledge Analysis via Extensive
  Materialization" (GPTKB). ACL 2025. https://arxiv.org/abs/2411.04920
  - Finding: GPT-4o-mini was asked recursively and produced 101 million triples about 2.9
    million entities, for about $3,500. In a sample of 1,000 triples, 31% were supported
    by a source, 61% were plausible, and 7% were false.
  - Finding: only 24% of the generated entities had an exact-label match on Wikidata, and
    accuracy fell at deeper levels of the crawl.
  - For us: expect more errors at departments than at schools. Expect many units with no
    exact Wikidata match. That is where duplicates and made-up units come from.
- **Giordano, Razniewski (2026).** "Foundations of LLM Knowledge Materialization:
  Termination, Reproducibility, Robustness." Findings of EACL 2026.
  https://arxiv.org/abs/2510.06780
  - Finding: changing the random seed or the temperature changes the output little.
    Changing the model or the language changes it a lot.
  - For us: one run per university is not a stable measurement. Different providers give
    different lists. That is a reason to compare them (Shuo, week 3).
- **Kalo, Razniewski, Zhang, Nguyen (2025).** "LM-KBC 2025: 4th Challenge on Knowledge Base
  Construction from Pre-trained Language Models." ISWC 2025 workshop, CEUR Vol-4041.
  https://ceur-ws.org/Vol-4041/paper7.pdf
  - Finding: with one fixed model, no retrieval, and long lists, the best macro F1 was
    0.444. A generate-then-judge entry scored 0.216.
  - For us: lists produced from memory alone are weak, and a judge does not help by
    default. Our 0.95 at the school level is plausible only because school lists are
    short, well known, and come with web search.
- **Mallen et al. (2023).** "When Not to Trust Language Models." ACL 2023.
  https://aclanthology.org/2023.acl-long.546/
  - Finding: on 14,000 questions about less-known entities, models struggle with unpopular
    facts. Bigger models help mainly on popular ones. Retrieval helps a lot on the rest.
- **Sun et al. (2024).** "Head-to-Tail." NAACL 2024. https://aclanthology.org/2024.naacl-long.18/
  - Finding: across 16 LLMs, accuracy falls steadily from well-known to obscure entities.
  - For us, from both papers: our 12 ground-truth universities are all famous. Add small
    ones (Shuo, week 3). Turn on web search for every provider (Anya, week 4).
- **Singhania, Razniewski, Weikum (2024).** "Recall Them All: Retrieval-Augmented Language
  Models for Long Object List Extraction from Long Documents." https://arxiv.org/abs/2405.02732
  (partly checked: arXiv preprint; journal venue not confirmed)
  - Finding: first gather candidates with retrieval, aiming for recall, then filter them
    for precision. This clearly beats lists from the LLM alone.
  - For us: the closest published design to ours, and the basis for research question A2.

### 1.2 Hierarchies: how to build and how to score them

- **Zeng et al. (2024).** "Chain-of-Layer." CIKM 2024. https://arxiv.org/abs/2402.07386
  - Finding: builds a taxonomy one layer at a time, with a filter that removes made-up
    nodes. The paper reports two scores, edge F1 and ancestor F1. With GPT-4 on WordNet:
    edge F1 57.7 against 38.0 for the baseline, ancestor F1 79.6 against 39.1.
  - For us: we build one layer at a time too. Report both scores. Edge F1 checks the exact
    parent. Ancestor F1 still gives credit to a department placed under the wrong school
    of the right university.
- **Shen et al. (2025).** "TaxoInstruct." Findings of ACL 2025.
  https://aclanthology.org/2025.findings-acl.167/
  - Finding: building a taxonomy comes down to two skills, finding siblings and finding
    parents.
  - For us: "here are the departments we have; which siblings are missing?" is a cheap
    second pass for recall.
- **Sun et al. (2024).** "Are Large Language Models a Good Replacement of Taxonomies?"
  PVLDB 17(11). https://arxiv.org/abs/2406.11131 (partly checked: authors beyond the first)
  - Finding: across 18 LLMs, accuracy drops by up to 30% from common to specialized
    domains, and from the top of the hierarchy to its leaves.
- **Peng, Bonald, Alam (2024).** "Refining Wikidata Taxonomy using Large Language Models."
  CIKM 2024. https://arxiv.org/abs/2409.04056
  - Finding: Wikidata's classes (P31, P279) are noisy, with cycles and confusion between
    instances and classes.
  - For us: do not find units by filtering on P31. Many real schools have an odd type.

### 1.3 Alignment and duplicates

- **Peeters, Steiner, Bizer (2025).** "Entity Matching using Large Language Models." EDBT
  2025. https://arxiv.org/abs/2310.11244
  - Finding: the best LLMs match records as well as fine-tuned models trained on
    thousands of examples, with few or no examples. No single prompt is best for every
    model.
  - For us: test the match prompt separately for each provider.
- **Wang et al. (2025).** "Match, Compare, or Select?" COLING 2025.
  https://arxiv.org/abs/2405.16884
  - Finding: on 8 datasets and 10 LLMs, picking the match from a list of candidates beats
    asking yes or no for each pair.
  - For us: keep `choose_match` as a choice from a list (Anya, week 6).
- **Narayan, Chami, Orr, Ré (2022).** "Can Foundation Models Wrangle Your Data?" PVLDB 16(4).
  https://www.vldb.org/pvldb/vol16/p738-narayan.pdf
  - Finding: prompted foundation models reach the state of the art on entity matching and
    other data-cleaning tasks.
- **Möller, Lehmann, Usbeck (2021).** "Survey on English Entity Linking on Wikidata."
  Semantic Web journal. https://arxiv.org/abs/2112.01989 (partly checked: year)
  - Finding: linkers mostly use labels and sometimes descriptions. They leave types,
    parents, and other statements unused.
  - For us: use the parent, the website domain, and the country as matching evidence.
- **Pellizzari di San Girolamo (2023).** "Conflations and duplications in Wikidata items."
  Wikidata Workshop 2023, CEUR Vol-3640. https://ceur-ws.org/Vol-3640/paper4.pdf
  - Finding: explains how duplicates arise on Wikidata and how they are detected (same
    label plus the same key values, broken uniqueness constraints). Also how they are
    merged, and how "different from" (P1889) marks two items as distinct.
  - For us: when Wikidata already has two items for one unit, flag it for a person.
- **Delpeuch (2020).** "A survey of OpenRefine reconciliation services." OM@ISWC 2020.
  https://arxiv.org/abs/1906.08092
  - Finding: the scores that reconciliation services return say little about whether a
    match is right.
  - For us: do not treat a search rank or score as confidence.
- **ROR and OpenAlex.** ROR records parent and child links between organizations
  (https://ror.readme.io/docs/relationships), but its FAQ puts faculties, schools, and
  departments out of scope (https://ror.org/about/faqs/). OpenAlex takes its institution
  hierarchy from ROR and stores Wikidata ids (Priem, Piwowar, Orr 2022,
  https://arxiv.org/abs/2205.01833).
  - For us: neither is ground truth for schools or departments. They help check a
    university's QID and research centers.

### 1.4 Spot check on our own data (2026-10-03)

We took 40 random schools that the `us-tier1` run labeled "missing" and ran Wikidata
full-text search on them.

- At least 5 already exist on Wikidata. Example: Boston University Wheelock College,
  Q4948183. The pipeline's prefix search did not find them because their labels begin
  with the university's name.
- One school, Kent State Geauga, has two items already: Q61931599 and Q100993336.
- About 7 of the 40 are administrative offices, not academic units.
- 3 of the 40 are campuses.

This is a small sample, not a measurement. It is the reason for Anya's week 6 and research
question A1.

---

## 2. Checking the hierarchy (Shuo)

### 2.1 How often do cited sources support the claim?

- **Liu, Zhang, Liang (2023).** "Evaluating Verifiability in Generative Search Engines."
  Findings of EMNLP 2023. https://arxiv.org/abs/2304.09848
  - Finding: across four generative search engines, only 51.5% of sentences were fully
    supported by their citations. Only 74.5% of citations supported the sentence they
    were attached to.
  - For us: even with web search, expect about a quarter of cited URLs not to support
    their unit. A cited URL has to be checked.
- **Gao, Yen, Yu, Chen (2023).** "Enabling Large Language Models to Generate Text with
  Citations" (ALCE). EMNLP 2023. https://arxiv.org/abs/2305.14627
  - Finding: defines automatic citation recall and citation precision. The best models
    lacked full citation support about half the time.
  - For us: we can reuse its metrics on (unit, URL) pairs.
- **Rashkin et al. (2023).** "Measuring Attribution in Natural Language Generation Models"
  (AIS). Computational Linguistics 49(4). https://aclanthology.org/2023.cl-4.2/
  - Finding: a two-stage annotation protocol. First, can the statement be understood?
    Then, is it supported by the source?
  - For us: a ready-made design for the human review question.
- **Walters, Wilder (2023).** "Fabrication and errors in the bibliographic citations
  generated by ChatGPT." Scientific Reports 13:14045.
  https://www.nature.com/articles/s41598-023-41032-5
  - Finding: without search, 55% (GPT-3.5) and 18% (GPT-4) of the citations were invented.
  - For us: URLs from Anthropic and Gemini, which today answer without search, need the
    strictest checking.
- **Wu et al. (2025).** "An automated framework for assessing how well LLMs cite relevant
  medical references" (SourceCheckup). Nature Communications 16:3615.
  https://www.nature.com/articles/s41467-025-58551-6 (partly checked: numbers from the
  abstract)
  - Finding: 50% to 90% of answers were not fully supported by their sources. Even GPT-4o
    with web search had about 30% of statements unsupported.
  - Finding: the automated checker agreed with three doctors' consensus 88.7% of the time,
    more than the doctors agreed with each other.
  - For us: an automated checker can reach expert-level agreement.

### 2.2 Checking a claim against a page

- **Min et al. (2023).** FActScore. EMNLP 2023. https://arxiv.org/abs/2305.14251
  - Finding: splits text into atomic facts and checks each against a source. The automatic
    estimate came within 2% of human scores.
  - For us: "X is a department of Y" is one atomic fact.
- **Wei et al. (2024).** "Long-form factuality in large language models" (SAFE). NeurIPS
  2024. https://arxiv.org/abs/2403.18802
  - Finding: an LLM using Google Search agreed with crowd annotators 72% of the time. Where
    they disagreed, it was right 76% of the time, at less than a twentieth of the cost.
  - For us: when the cited URL is dead, searching for another page is a cheap fallback.
- **Tang, Laban, Durrett (2024).** MiniCheck. EMNLP 2024. https://arxiv.org/abs/2404.10774
  - Finding: a small model (770M parameters) checks claims against a document as well as
    GPT-4, at 400 times lower cost.
  - For us: checking every unit's page this way is affordable (Shuo, week 6).
- **Zha et al. (2023).** AlignScore. ACL 2023. https://arxiv.org/abs/2305.16739
  - Finding: a 355M-parameter checker that matches or beats GPT-4-based metrics on 22
    datasets. A second cheap baseline.
- **Honovich et al. (2022).** TRUE. NAACL 2022. https://arxiv.org/abs/2204.04991
  - Finding: entailment models and question-answering methods are the strongest checkers,
    and they complement each other.
  - For us: compare checkers example by example, not only on their averages.
- **Amaral, Rodrigues, Simperl (2023).** "ProVe: A pipeline for automated provenance
  verification of knowledge graphs against textual sources." Semantic Web journal.
  https://arxiv.org/abs/2210.14846
  - Finding: checks a Wikidata statement against the page in its reference, in four steps:
    extract the text, turn the statement into a sentence, pick the relevant sentences,
    check the claim. 87.5% accuracy on references with enough text.
  - For us: the same task as our check of the cited page, already done on Wikidata.
- **Amaral et al. (2021),** "Assessing the Quality of Sources in Wikidata Across
  Languages", ACM JDIQ 13(4), https://dl.acm.org/doi/10.1145/3484828, and **Piscopo et al.
  (2017),** "Provenance Information in a Collaborative Knowledge Graph", ISWC 2017,
  https://link.springer.com/chapter/10.1007/978-3-319-68288-4_32
  - Finding: on Wikidata, about 90% of the sampled references were relevant (2021).
    61% were both relevant and authoritative (2017).
  - For us: a bar for our own references to clear.

### 2.3 Does agreement between LLMs mean correctness?

- **Manakul, Liusie, Gales (2023).** SelfCheckGPT. EMNLP 2023. https://arxiv.org/abs/2303.08896
  - Finding: facts that come and go across repeated samples from the same model tend to be
    made up.
  - For us: rerunning one provider gives a cheap error signal per unit.
- **Wang et al. (2023).** "Self-Consistency Improves Chain of Thought Reasoning." ICLR 2023.
  https://arxiv.org/abs/2203.11171
  - Finding: a majority vote over sampled answers raises accuracy.
- **Li et al. (2024).** "More Agents Is All You Need." TMLR.
  https://arxiv.org/abs/2402.05120
  - Finding: accuracy grows with the number of sampled answers, most on hard tasks.
  - For us: how many runs are worth paying for is something to measure.
- **Du et al. (2024).** "Improving Factuality and Reasoning in Language Models through
  Multiagent Debate." ICML 2024. https://arxiv.org/abs/2305.14325
  - Finding: several rounds of debate between model instances reduce made-up facts.
- **Farquhar, Kossen, Kuhn, Gal (2024).** "Detecting hallucinations in large language
  models using semantic entropy." Nature 630. https://www.nature.com/articles/s41586-024-07421-0
  - Finding: count agreement on meaning, not on exact wording.
  - For us: group "Dept. of CS" and "Computer Science" before counting (Shuo, week 3).
- **Kim, Garg, Peng, Garg (2025).** "Correlated Errors in Large Language Models." ICML 2025.
  https://arxiv.org/abs/2506.07962
  - Finding: across more than 350 models, when two models are both wrong they give the same
    wrong answer about 60% of the time. Larger, more accurate models share more errors,
    even across companies.
  - For us: agreement between OpenAI, Anthropic, and Google is weaker evidence than it
    looks. Research question S1 measures how much weaker on our data.

### 2.4 Judges

- **Zheng et al. (2023).** "Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena." NeurIPS
  2023. https://arxiv.org/abs/2306.05685
  - Finding: a strong LLM judge agrees with people about as often as people agree with
    each other, above 80%. It is biased by the order answers are shown in, by length, and
    in favor of itself.
- **Panickssery, Bowman, Feng (2024).** "LLM Evaluators Recognize and Favor Their Own
  Generations." NeurIPS 2024. https://arxiv.org/abs/2404.13076
  - Finding: models recognize their own text and prefer it. The better a model recognizes
    itself, the more it prefers itself.
  - For us: the judge should come from a provider that did not generate. Our best eval
    setup already does this.
- **Wang et al. (2024).** "Large Language Models are not Fair Evaluators." ACL 2024.
  https://aclanthology.org/2024.acl-long.511/
  - Finding: changing only the order of answers changed the winner in 66 of 80 cases. The
    fix is to judge in more than one order and combine.
  - For us: the order test in Shuo's week 4.

### 2.5 People: getting good verdicts cheaply

- **Dawid, Skene (1979).** "Maximum Likelihood Estimation of Observer Error-Rates Using the
  EM Algorithm." Applied Statistics 28(1). https://www.jstor.org/stable/2346806
  - Finding: estimates each reviewer's error rates and the true answer together, without
    gold labels.
- **Sheng, Provost, Ipeirotis (2008).** "Get Another Label?" KDD 2008.
  https://dblp.org/rec/conf/kdd/ShengPI08.html
  - Finding: when labelers are noisy, a second or third label on selected items pays off.
  - For us: extra labels should go to the units that are still uncertain.
- **Ipeirotis, Provost, Wang (2010).** "Quality Management on Amazon Mechanical Turk."
  HCOMP 2010. https://dl.acm.org/doi/10.1145/1837885.1837906
  - Finding: separates a worker's systematic bias, which can be corrected, from real error.
  - For us: a worker who accepts everything is biased, not useless.
- **Oleson et al. (2011).** "Programmatic Gold." HCOMP 2011.
  https://www.semanticscholar.org/paper/761115e9b9564f7ad1f5ca8fe531fabcad7dfbea
  - Finding: gold items made automatically by corrupting known-good items give workers
    targeted feedback and catch cheaters.
  - For us: make gold items with the wrong parent, a fake unit, or the wrong page.
- **Bernstein et al. (2010).** "Soylent: A Word Processor with a Crowd Inside." UIST 2010.
  https://doi.org/10.1145/1866029.1866078
  - Finding: Find-Fix-Verify. Split creative work from independent verification.
  - For us: our design already has this shape. The LLM finds, people verify.
- **Snow et al. (2008).** "Cheap and Fast, But is it Good?" EMNLP 2008.
  https://aclanthology.org/D08-1027/
  - Finding: on five well-defined tasks, non-expert labels agreed closely with experts.
- **Veselovsky, Ribeiro, West (2023),** "Artificial Artificial Artificial Intelligence",
  https://arxiv.org/abs/2306.07899, and **Veselovsky et al. (2025),** "Prevalence and
  Prevention of Large Language Model Use in Crowd Work", Communications of the ACM 68(3),
  https://doi.org/10.1145/3685527
  - Finding: about a third of crowd workers used an LLM on a writing task.
  - Finding: asking directly, and showing the text as an image that cannot be copied, cut
    usage from 27.6% to 15.9%.
  - For us: Prolific verdicts may secretly be ChatGPT verdicts. Show the page as a
    picture, and ask workers not to use AI tools (Shuo, week 8).
- **Gilardi, Alizadeh, Kubli (2023).** "ChatGPT Outperforms Crowd Workers for Text-Annotation
  Tasks." PNAS 120(30). https://doi.org/10.1073/pnas.2305016120
  - Finding: zero-shot ChatGPT beat Mechanical Turk workers by about 25 points, at about a
    thirtieth of the cost.
  - For us: do not treat crowd verdicts as truth. Compare both against expert verdicts.
- **Douglas, Ewell, Brauer (2023),** PLOS ONE, https://doi.org/10.1371/journal.pone.0279720,
  and **Peer et al. (2022),** Behavior Research Methods,
  https://doi.org/10.3758/s13428-021-01694-3
  - Finding: 73% of respondents on Prolific gave high-quality data, against 39% on
    Mechanical Turk.
  - For us: Prolific is a reasonable choice. Still plan to screen out about a quarter of
    responses.

### 2.6 A few human labels plus many machine labels

- **Angelopoulos et al. (2023).** "Prediction-Powered Inference." Science 382(6671).
  https://doi.org/10.1126/science.adi6000
  - Finding: gives valid confidence intervals from a small labeled sample plus model
    predictions on everything. The better the model, the narrower the interval.
  - For us: the method behind research question S2.
- **Zrnic, Candès (2024).** "Active Statistical Inference." ICML 2024.
  https://arxiv.org/abs/2403.03208
  - Finding: send the human labels to the items the model is unsure about, and still get
    valid intervals.
- **Gligorić, Zrnic, Lee, Candès, Jurafsky (2025).** "Can Unconfident LLM Annotations Be
  Used for Confident Conclusions?" NAACL 2025. https://aclanthology.org/2025.naacl-long.179/
  - Finding: used the LLM's stated confidence to pick which items people label. It needed
    over 25% fewer human labels.
  - For us: the closest ready-made recipe, with code
    (github.com/kristinagligoric/confidence-driven-inference).
- **Gao et al. (2019).** "Efficient Knowledge Graph Accuracy Evaluation." PVLDB 12(11).
  https://doi.org/10.14778/3342263.3342642
  - Finding: sample by entity, then facts within it, and stratify. This cut the cost of
    estimating accuracy by up to 60%.
  - For us: sample by university, then units within it.
- **Marchesin, Silvello (2024).** "Efficient and Reliable Estimation of Knowledge Graph
  Accuracy." PVLDB 17(9). https://doi.org/10.14778/3665844.3665865
  - Finding: when accuracy is high, the usual (Wald) interval breaks: it can have zero
    width or pass 1. Use Wilson intervals instead.
  - For us: our precision is near 0.95, so always report Wilson intervals.
- **Ojha, Talukdar (2017),** KGEval, EMNLP 2017, https://aclanthology.org/D17-1183/, and
  **Qi et al. (2022),** KDD 2022, https://doi.org/10.1145/3534678.3539233
  - Finding: one human judgment can settle related facts.
  - For us: a rejected school casts doubt on all its departments.

### 2.7 Who reviews what, and the risk of copying the machine

- **Mohri, Hashimoto (2024).** "Language Models with Conformal Factuality Guarantees." ICML
  2024. https://proceedings.mlr.press/v235/mohri24a.html
  - Finding: calibrates a score threshold on labeled items so that the claims kept are
    right at a guaranteed rate (80% to 90% in the paper).
  - For us: a way to let high-confidence units skip review at a known error rate.
- **Madras, Pitassi, Zemel (2018),** "Predict Responsibly", NeurIPS 2018,
  https://proceedings.neurips.cc/paper/2018/hash/09d37c08f7b129e96277388757530c72-Abstract.html,
  and **Mozannar, Sontag (2020),** "Consistent Estimators for Learning to Defer to an
  Expert", ICML 2020, https://arxiv.org/abs/2006.01862
  - Finding: decide to pass an item to a person based on where the person is better than
    the model, not only on where the model is unsure.
- **Schroeder, Roy, Kabbara (2025).** "Just Put a Human in the Loop?" Findings of ACL 2025.
  https://aclanthology.org/2025.findings-acl.1323/
  - Finding: 410 annotators saw LLM suggestions. They were not faster, but they largely
    adopted the suggestions. Measured against those labels, the LLM looked better than it
    was.
  - For us: if Prolific workers see the verifier's answer, our precision estimate will be
    too high. This is the reason for the two versions of the task in Shuo's week 8.
- **Choi et al. (2024),** "The LLM Effect", EMNLP 2024, https://arxiv.org/abs/2410.04699,
  and **Buçinca, Malaya, Gajos (2021),** "To Trust or to Think", PACM HCI 5,
  https://www.eecs.harvard.edu/~kgajos/papers/2021/bucinca21trust.pdf
  - Finding: AI suggestions speed experts up but anchor them.
  - Finding: making people answer before they see the AI's answer reduces overreliance,
    though people like it less.
  - For us: experts reviewing in week 7 should decide before looking at the checks, then
    look.

### 2.8 Estimating what nobody found

- **Trushkowsky, Kraska, Franklin, Sarkar (2013).** "Crowdsourced Enumeration Queries." ICDE
  2013. Extended in CACM 59(1), 2016. https://cacm.acm.org/magazines/2016/1/195737
  - Finding: estimates how many items a complete list would have from how often new
    answers keep arriving (species estimation). A few workers who give many answers make
    the estimate far too high.
  - For us: each LLM run is a sample. One provider that dominates the samples plays the
    role of that worker.
- **Chung, Mortensen, Binnig, Kraska (2018).** "Estimating the Impact of Unknown Unknowns on
  Aggregate Query Results." ACM TODS 43(1). https://dl.acm.org/doi/10.1145/3167970
  - Finding: the overlap between sources estimates how many items all of them missed.
- **Luggen et al. (2019).** "Non-parametric Class Completeness Estimators for Collaborative
  Knowledge Graphs: The Case of Wikidata." ISWC 2019.
  https://doi.org/10.1007/978-3-030-30793-6_26
  - Finding: the same idea applied to Wikidata's own classes.
- **Wesson et al. (2022).** Annals of Epidemiology.
  https://pmc.ncbi.nlm.nih.gov/articles/PMC9826711/
  - Finding: when the lists depend on each other, capture-recapture estimates are biased.
    Compare several estimators rather than trusting one.
- **Li, Xin, Long, Su (2025).** "Evaluating the Unseen Capabilities: How Many Theorems Do
  LLMs Know?" https://arxiv.org/abs/2506.02058
  - Finding: estimates how much an LLM knows but never said, from repeated runs.
- **Razniewski, Arnaout, Ghosh, Suchanek (2024).** "Completeness, Recall, and Negation in
  Open-World Knowledge Bases: A Survey." ACM Computing Surveys 56(6).
  https://dl.acm.org/doi/10.1145/3639563
  - Finding: a survey of completeness and recall in knowledge bases that are never
    complete.
  - For us: a cheap warning sign is a university with far fewer units than similar
    universities, the idea behind Recoin (Balaraman, Razniewski, Nutt 2018).

---

## 3. What this means for us, in five lines

1. Lists from memory are weak, worse for obscure universities and deeper levels. Use
   search for every provider and measure on small universities too.
2. Matching names to Wikidata by label misses many existing items. Search more than one
   way, use the parent and the website, and measure the false-new rate.
3. A cited URL supports its claim only about three times in four, even with search. Save
   the page and check it, mechanically first, then with a cheap verifier.
4. Agreement between providers, and an LLM judge, are useful but biased signals. Errors
   correlate across companies, judges prefer their own output and depend on order.
   Measure both on our data before relying on them.
5. People are the final check, but they copy machine answers they can see and may use
   LLMs themselves. Hide the machine's answer, use gold items, and estimate overall
   precision from a random human sample plus verifier labels, with intervals.
