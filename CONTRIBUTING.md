# Contributing

Thanks for helping. [AGENTS.md](AGENTS.md) has the setup commands, the project
layout and the rules every change must follow, for people and coding agents
alike. The short version:

- Canvas Reader only ever reads Canvas. Changes that submit, edit or send
  anything won't be accepted.
- Never drop content silently: a partial answer says it is partial.
- Add or update tests for every behavior change, and run the full suite before
  opening a pull request.
- Record user-visible changes under "Unreleased" in [CHANGELOG.md](CHANGELOG.md).
- Never commit tokens, keys, tunnel IDs, personal paths or real course data.
  Tests use synthetic data only.

Found a security problem? Follow [SECURITY.md](SECURITY.md) instead of opening an
issue.
