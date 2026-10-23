# Skills

Task guides for coding agents working in this repository, in the open Agent Skills layout:
one folder per skill, each with a `SKILL.md` (YAML front matter `name` and `description`,
then the steps). Any agent can read them.

They point at the rules rather than repeat them: `AGENTS.md` (hard rules, conventions) stays
the source of truth. `tests/test_skills.py` checks that every skill is
well-formed, that every `mappd` command it names exists, and that every repository path it
names is there.

| Skill | Use it when |
| --- | --- |
| [new-process](new-process/SKILL.md) | adding a business process |
| [eval-loop](eval-loop/SKILL.md) | changing a prompt, a model or an agent |
| [side-effect](side-effect/SKILL.md) | code that changes the outside world |
| [migration](migration/SKILL.md) | changing a table |
| [dataset](dataset/SKILL.md) | building or extending eval data |
| [workflow-debug](workflow-debug/SKILL.md) | a case is stuck, failed, or an action is `unknown` |
| [release-check](release-check/SKILL.md) | before merging or deploying |
