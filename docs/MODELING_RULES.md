# Modeling rules

**Status: Proposed by Anya, 2026-10-29.** A rule marked **Open** has no decision yet.

These rules say which units go into a hierarchy, and with which type. The code, the review
sheet, and the export follow them. Each rule has one real example from weeks 1 to 4. A
"unit" is any part of a university. The terms are in `TASKS.md` section 12.

## 1. Scope

Schools, campuses, departments, degree programs, and labs are in scope. Each one has its own
type.

Offices, institutes, museums, and libraries are out of scope.

Examples:

- In scope: Stern (Q770467) is a school of NYU. Applied Psychology is a department of
  Steinhardt (check 1). The Music and Audio Research Laboratory is a lab of Steinhardt
  (check 1). The LLM typed it "center", because the sub-unit step has no lab type yet.
- Out of scope: the Grey Art Gallery (Q16972698) is a museum of NYU. The Elmer Holmes Bobst Library
  (Q3051566) is a library of NYU. Both are direct units of NYU in the week 3 snapshot.

**Open:** are centers in scope or out of scope? Example: in check 1, the LLM found 21
centers at Stern, such as the Berkley Center for Entrepreneurship. Until this is decided,
the sub-unit step gives each center `in_scope` "check".

## 2. Types

Each unit gets exactly one of these types: school, campus, department, program, lab,
center, office, other.

Never record a department or a program as a school.

Example: in run `us-tier1`, the LLM proposed "Digital Arts" (University of Advancing
Technology) and "Online Program" (Appalachian Bible College) as schools. In the week 3
check 4 sample, Anya marked both as not a school.

## 3. What counts as a department

A department is an academic unit inside a school or college. It must meet all three tests:

1. It has its own faculty.
2. It teaches courses or runs degrees in one field, or in a group of related fields.
3. The university's own website lists it as a department, a division, or an academic unit.

The name alone does not decide.

Example: Stanford's "Department of Environmental Health and Safety" (Q101029382) has
"academic department" as its P31 on Wikidata. The name says "department", but it is a
safety office, not an academic unit. It does not meet test 1 or test 2.

## 4. Department or program

A program has no faculty of its own. A department has its own faculty (rule 3, test 1).

Example: in check 1, the LLM typed 4 Stern units as "program": Business and Society
Program; Entertainment, Media and Technology; Fintech; and Management Communication
Program. Shuo's ground truth lists all 4 as departments. The Stern page puts them under
the heading "Academic Departments and Programs", so the page does not settle it.

**Open:** how to decide when the website does not make the difference clear.

## 5. A unit named "center" that works as a department

Example: Courant's Center for Data Science grants degrees and has its own faculty. Shuo's
ground truth lists it as a department. In check 1, the LLM typed it "center".

**Open:** does it count as a department, because it meets rule 3? Or is it a center, so
that the open question on centers in rule 1 decides its scope?

## 6. Campuses

A campus is a place. Record it with type campus.

A campus can have its own schools, and it can be in another country. Record a campus's
country from the campus's own page. Never copy the country from the parent university.

Examples:

- NYU Abu Dhabi is in the United Arab Emirates. Its own page,
  nyuad.nyu.edu/en/about/the-nyuad-campus/global-university.html, says "the UAE". The page
  was saved in run `subunits-20261029T135111Z-a3e3d4` (week 4, check 4).
- NYU Shanghai is in China. Its own page, shanghai.nyu.edu, was saved in the same run. That
  page names Shanghai but never writes "China".
- In run `us-tier1`, the LLM proposed "USU Wendover" (Utah State University) as a school.
  It is a campus. The week 3 check 4 sample marked it as not a school.

## 7. Offices

Never record an office as a unit. Examples of offices are a registrar, student services,
and career services.

Keep each office in the run log, with the flag "out of scope". The sub-unit step does this
today: an office gets `in_scope` "no".

Examples:

- In the week 1 spot check of `us-tier1`, about 7 of 40 "missing" schools were offices,
  such as "Career Services" and "Division of Student Affairs".
- The week 3 check 4 sample had "Office of the Registrar" (Welch College) and "Student
  Services" (Stone Child College).
- In check 1, the LLM found 13 offices at Steinhardt. None of them is in the department
  list.

## 8. Endowed chairs

An endowed chair is a named professorship. It is a position, not a unit. Never count a
chair as a unit.

Example: in the week 3 snapshot, 904 of Stanford's 1,228 direct units on Wikidata have
"chair" (Q1363750) as their P31. One example is the King Philanthropies Professorship. This
is the main reason why Stanford's gap in the week 3 gap report is -1,219.

## 9. Open questions

- **Open: a unit with parents in two universities.** Example: NYU Shanghai has P749 to NYU
  and also to East China Normal University. It is not decided which tree owns it, or
  which rank and qualifiers each P749 gets.
- **Open: renamed units.** Example: Penn State's graduate school is now the "J. Jeffrey and
  Ann Marie Fox Graduate School". Wikidata has it under the old name, "Penn State Graduate
  School" (Q15265896), with no P749 or P361 to Penn State. The week 3 check 4 sample found
  it. Alignment by name alone did not find it.
- **Open: typos on Wikidata.** It is not decided whether we match through a typo, fix it, or
  report it to a person. Example: the English label of Q10867642 is "University of
  Michigan–Shanghai Jiao Tong Univeristy Joint Institute", with "Univeristy" for
  "University". It is a joint institute with P749 to the University of Michigan (Q230492)
  and to Shanghai Jiao Tong University (Q525169). The week 3 snapshot `snapshot-us-tier1`
  has it, and week 3 check 3 found it. It is the only label with this typo in the 1,519
  snapshots.
