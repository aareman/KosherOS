# Working on KosherOS

Read [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and
[docs/contributing.md](docs/contributing.md) for the full guide.

## Discussing sensitive material

This project filters material that its users do not want to encounter.
Keep explanations clear and appropriate without repeating that material.
Apply this guidance to conversation replies, progress updates, commit and
pull-request descriptions, issue comments, documentation, and UI text.

- Describe the category, behavior, and result in neutral terms. For example,
  say "a restricted term was missed" or "the page exceeded the account's
  content threshold" rather than quoting the term or passage.
- Be precise about what failed, what changed, and how it was verified.
  Avoid vague substitutes that leave the reader guessing about the work.
- Use neutral examples when they demonstrate the same behavior. Do not
  reproduce sensitive passages, search queries, site names, or images in
  explanations, screenshots, or test-output excerpts.
- When exact data is needed to diagnose or test a filter, keep it in the
  relevant data or test fixture. Refer to the file, test name, or line in
  the explanation instead of copying the sensitive content into it.
- Preserve accurate filter data and meaningful test coverage. This writing
  guidance does not call for weakening detection, changing identifiers, or
  replacing necessary regression inputs with examples that miss the bug.

Use plain, complete sentences. Explain the outcome in language a parent
can understand, with technical details where they help verify the result.
