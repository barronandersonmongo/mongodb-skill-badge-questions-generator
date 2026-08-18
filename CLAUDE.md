# MongoDB Skill Badge Questions Generator

Keep the README.md file up to date an in sync with changes as they progress.
Ensure automated tests are created and maintained for every feature and function created in this program.
Successful changes pass the tests.  If you need to change an existing test, please make it clear the reason for the change and the expecations for the behavior.  Do not change the test simply because the program wont pass the test, that defeats the purpose of tests.  Instead, define the tests well based on requirements and modify the program to pass the tests.
Every test carries a docstring block with `Intent:` (why it exists), `Success:` (what passing proves), and `Feature:` (the business feature or function under test). Once written, these blocks are never edited — they are the recorded requirement. To change behavior, change the program, or add a new test alongside with its own block. `tests/test_test_documentation.py` enforces this; see `tests/README.md`.

Github pull requests should be used instead of direct modifications via a push.

## What this is
An internal authoring tool for MongoDB teams who build skill badge quizzes.
It generates, validates, stores, filters, and exports quiz questions.

It is **not** a quiz-taking platform. No learners, no scoring, no attempts.

The hard part — and the point of the tool — is question *quality*, not volume.

## Working style
- Keep responses high level. Details only when asked.
- Keep the stack minimal and mainstream. Avoid esoteric frameworks.

## Stack
- Python 3.14, stdlib `venv` + `pip`
- FastAPI + Jinja2 (server-rendered HTML, no build step)
- MongoDB Atlas (storage + vector search for duplicate detection)
- Claude via the `anthropic` SDK (generation and validation)
- Bootstrap 5 + Chart.js + vanilla JS, loaded from CDN

No Node, no npm, no bundler, no Docker, no task queue, no RAG framework.

## Data model notes
- Questions carry `categories` and `skill_badges` as **arrays** — one question
  may belong to several of each.
- Questions must be queryable by those fields and exportable as JSON.
- Output needs to be easy to copy-paste.

## Repo
https://github.com/barronandersonmongo/mongodb-skill-badge-questions-generator

## Open questions
- Which skill badge to target first (determines the doc corpus).
- Whether the MongoDB MCP server exposes documentation search, or whether docs
  need to be sourced another way. MCP server is not yet authorized.
- Embedding provider for duplicate detection (Voyage AI is the current lean).
