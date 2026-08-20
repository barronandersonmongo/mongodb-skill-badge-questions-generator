# 5-Minute Demo

## 0:00 — The problem

- Skill badge quizzes use a small, static set of questions that never changes.
- If the questions and answers leak, people can memorize them and the badge stops proving MongoDB skill.
- Anything stored on the company network may also be surfaced by Glean.
- That fear keeps the questions away from DevRel, even though DevRel needs them to train customers and help them earn badges.
- Some people learn best by taking sample tests, but a static practice test has the same leakage and maintenance problems.
- MongoDB, its features, and its documentation keep changing. Static questions become incomplete or wrong.
- Every new skill badge creates the same slow, manual question-writing problem again.

**What we need:** Thousands of current, grounded questions. Enough variety to give each quiz or practice test a different set, including similar concepts with different wording and answer positions.

**Why:**

- Reduce the value of leaked questions and answers.
- Give DevRel useful material for training and customer support.
- Give learners dynamic sample tests with explanations.
- Keep questions current as MongoDB changes.
- Create questions for new skill badges quickly.

## 0:45 — Generate questions

Pick badges, number of chunks, questions per chunk, and skill level. Start it.

**Why:**

- Control what gets generated.
- Control how much it should cost.

## 1:10 — Show live progress

Show questions created, cost per question, projected cost, and **Stop after current chunk**.

**Why:**

- See what is happening.
- Stop without losing completed work.

## 1:30 — Open a question

Show four answers, one correct answer, explanations, difficulty, topics, and badges. Correct answer position is randomized.

**Why:**

- Test MongoDB skill instead of memorization.
- Prevent answer-position patterns.
- Keep quality while increasing volume.

## 2:00 — Open the source link

Show the exact MongoDB documentation used. Show live and stored copies.

**Why:**

- Verify every question.
- See exactly what the model used.
- Regenerate questions as the documentation changes.

## 2:25 — Search and filter

Search by badge, category, difficulty, meaning, or exact question ID. Copy the URL.

**Why:**

- Help DevRel find the right training material.
- Help learners practice the areas where they need work.
- Avoid exposing a fixed quiz.

## 2:50 — Find duplicates

Run a scoped sweep. Adjust the score. Compare both questions side by side. Select what to delete.

**Why:**

- Build concept coverage and variety.
- Remove reworded copies whose answers leak together.
- Keep a person in control of deletion.

## 3:30 — Show Coverage and Material

Show badges with the fewest questions first. Show unused documentation and exhausted badges.

**Why:**

- Find badges that need more questions.
- See which badges can support another run.
- Know when new source material is required.

## 3:55 — Show Run history

Show model, settings, time, failures, tokens, total cost, and cost per question.

**Why:**

- Know what each run produced.
- Know what each run cost.
- Compare changes to prompts, models, and settings.

## 4:15 — Export JSON

Filter, preview, copy, or download the complete result.

**Why:**

- Deliver skill badge quizzes.
- Build dynamic practice tests.
- Support approved DevRel workflows.

## 4:30 — Quick admin view

Show the Credly badge catalog and about 7,200 documentation pages split into about 27,000 searchable chunks.

**Why:**

- Pick up new documentation.
- Keep pace with product changes.
- Support new badges without starting over manually.

## 4:50 — Close

Replace one static, guarded quiz with a large, current question bank for assessments, customer support, and learning.

**Why:**

- Protect the value of the badge.
- Give DevRel the material needed to support customers.
- Give learners a safe way to practice.
