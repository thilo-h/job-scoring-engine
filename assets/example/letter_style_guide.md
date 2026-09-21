# Letter Style Guide

> The rulebook for the letter **review** path. The applicant writes the letter;
> this document defines what a good one looks like, so two things can check it:
>
> - `src/agent/letter_quality.py` — implements the mechanically checkable rules
>   deterministically, without a model. Each rule below is marked
>   *[code checks]* or *[code corrects]* where that applies.
> - the `letter_review` chat mode — reasons about the rest and proposes
>   targeted edits the applicant accepts or rejects.
>
> Nothing here generates a letter. The rules are written as review criteria,
> which is also why they are numbered: `letter_quality.py` refers to these
> section numbers, and a finding names the rule it came from.
>
> Swap the placeholder names for your own when you use this. Keep the section
> numbering — the code depends on it.

---

## §1 · Head and subject line

- A bold subject line sits above the salutation:
  - EN: `Application: <role>, <team if named in the posting>`
  - DE: `Bewerbung: <Rolle>, <Team falls genannt>`
  - ES: `Candidatura: <Rol>, <equipo si se menciona>`
  No gender markers ("(m/f/d)"), no workload figure. Example:
  `Application: Data Engineer, Platform Team`
- The company block (name + city) is only set when both are known from the
  posting. **Never invent an address**, not even in the body text. *[code sets]*
- The date is right-aligned by the renderer. *[code sets]*

---

## §2 · Length and page count — mind the priority

- Target corridor **450–600 words** (intro + middle + closing), aim for
  about **500**. *[code checks]*
- **One page is the goal, not a hard condition.** Word count drives the
  length. The head and subject line cost space; a letter inside the corridor
  may run slightly over.
- **The word corridor takes precedence over the page count.** 470 words with a
  few lines on page 2 beats 390 words on one page. Never cut below 450 words
  to win a page.
- The renderer measures the actual page fit and reports an overflow as a
  finding — it is information, not an instruction to cut. *[code checks]*
- If you do need to shorten, in this order:
  1. cut redundancy and filler (§10)
  2. condense paragraphs **without losing evidence**
  3. accept the overflow
- **Never** cut substantive evidence just to force a single page.
- Paragraphs average 4–5 lines, **none over 7** (≈ 730 characters). *[code checks]*
- Arithmetic: 450–600 words is roughly 30–40 lines. At 4–5 lines per paragraph
  that means **6–9 paragraphs**. An overlong paragraph gets split, not cut.

---

## §3 · Workload — the most common mistake

- Take the workload **from the posting**.
- If the posting names a range ("40–60%", "80–100%"), name **exactly that
  range** in the closing paragraph. Not "80%" when the posting says "80–100%".
- Only when the posting is silent: fall back to the profile default.
- *[code checks]* the closing paragraph for exactly this figure.

---

## §4 · Tense and factual fidelity

- Finished positions **always in past tense**. Never "my current role",
  "currently at" for a position that has ended. Which stations have ended
  follows from the end dates in the CV markdown.
- The CV markdown is the source of truth. If an end date is missing, do
  **not** present the station as ongoing. *[code checks]*
- No invented experience, certificates, projects, language skills or tools.
  **And no embellishment of real stations**: no team sizes, methods, data
  sources, tools or metrics that are not in the CV. If the evidence does not
  stretch to 450 words, the letter stays shorter. Truth before length.

---

## §5 · Current enrolment

If the posting requires ongoing enrolment (working-student and student roles),
confirm it explicitly in the closing paragraph, with the timeline from the CV.
If a date is missing from the CV — the planned graduation, say — **leave it out
rather than invent it**.

---

## §6 · Text integrity

These errors recur whenever a letter is shortened or rewritten by hand:

- **Duplication:** the old and the new version of a sentence both survive in
  the paragraph. No two sentences with the same opening or near-identical
  content. *[code reports]*
- **Sentence fragment:** a comma became a period, so a sentence now starts
  with "And …", "But …". Never start that way. *[code reports]*
- **Missing space:** `... this summer.I built ...` — a space after every
  punctuation mark. *[code corrects]*
- **Lost hyphen:** `room-programme` stays `room-programme`, never
  `roomprogramme`. *[code corrects]*

---

## §7 · Typography

- **Em dash (—)** for asides, with spaces, consistently the same character.
  Never mixed with en dash (–) or hyphen (-).
- **En dash (–)** for numeric ranges: `40–60 %`, `2022–2023`.
- Percent: German and Spanish with a space (`80 %`), English without (`80%`).
- *[code corrects]* dashes and percent spacing.

---

## §8 · Company research

When research is available, the rule is evidence over praise:

- Only company facts that appear **verbatim** in a source. Every company fact
  used in the letter is recorded with the `source_url` it came from, and the
  URL is verified against the actual search results. *[code checks]*
- Tie a researched fact **to a station in the CV**, never leave it as praise:
  - Good: "The partnership with X addresses problem Y — which is what I
    worked on at Z."
  - Bad: "That shows you take your own transformation seriously."
- **No sentence praising the company without a following self-reference.**
- No claims about internal process or team structure when the only source is
  a profile snippet.

With no research, write from the posting alone and record no company facts.
**Empty means:** the letter contains no statement about the company that does
not come from the posting itself.

---

## §9 · Weighting by role profile

- The emphasis of the posting determines the weight of the paragraphs.
- Where a skill appears only under "nice to have": **at most 2–3 sentences**
  on it, and **never in the closing paragraph**.
- For consulting-type roles: at least one piece of evidence for advisory work,
  **plus one sentence on why consulting** — always checked for junior profiles.

---

## §10 · No redundancy

- No sentences that summarise what the reader just read ("Both experiences
  reflect the responsibilities listed in this role"). *[code reports]*
- **No station and no qualification in two paragraphs** — a degree in
  paragraph 2 **and** in the closing: once is enough. *[code checks]* per station.
- No decorative list of languages. Languages other than the letter's own and
  English **only when the posting names them**, then briefly, in one sentence:
  "I work in German and English, with functional Spanish." *[code checks]*
  against the posting.
- Enclosures: do **not** list the letter itself among them — the letter *is*
  the letter ("CV and references are attached").

---

## §11 · Repository link

- At most **one** role-specific repository, from an explicit allow-list.
  **Never a bare profile or account link.**
- For roles with no technical component: no link at all.
- *[code discards]* anything not on the list.

---

## §12 · Register and phrasing

- **Concrete over generic.** "At <employer> I built …" rather than "I bring
  experience with …".
- **No stock phrases.** Avoid "It is with great pleasure …", "I hereby apply
  …", "perfectly aligned", "passionate about". Direct register.
- For German letters, Swiss spelling: `ss` rather than `ß`. *[code corrects]*
- Keep the form of address consistent — a formal letter never slips into the
  informal second person halfway through. *[code reports]*
- Salutation defaults:
  - EN: "Dear Hiring Team,"
  - DE: "Sehr geehrte Damen und Herren,"
  - ES: "Estimado equipo de <company>,"
  When the posting names a contact, use their name. *[code checks]* consistency.
- Date format:
  - DE: "Musterstadt, 4. Mai 2026"
  - EN: "Musterstadt, May 4, 2026"
  - ES: "Musterstadt, 4 de mayo de 2026"

---

## §13 · Language of the letter

- Posting in Spanish → Spanish
- Posting in English, company in a German-speaking market → German
- Posting in English, international or US company → English
- Posting in German → German
- An explicit language override wins over all of the above.

*[code detects]* the language of a saved letter from its own text, so a letter
edited by hand is reviewed in the language it is actually written in.
