# Latest Official Updates Checked

Checked on 2026-04-18.

## Claude Code
- Official changelog: `2.1.109` was published on April 15, 2026.
- `2.1.108` on April 14, 2026 included:
  - plugin marketplace timeout and npm source improvements
  - slash command discovery via the Skill tool
  - a fix for a hook-related security issue involving workspace trust
- Impact on this repo:
  - recommend upgrading local Claude Code to `2.1.109+`
  - no required baseline file-format change was needed for the current plugin scaffold

## GitHub
- Template repositories are still created after the repository exists; this repo should be published first and then marked as a template repository.
- Reusable workflows from a private repository still require explicit Actions access configuration before other private repositories can consume them.
- GitHub Actions added a rerun limit of 50 on April 10, 2026.
- Impact on this repo:
  - added onboarding guidance for private workflow-sharing access
  - no workflow retry loop changes were required because this baseline does not auto-rerun failed workflows
