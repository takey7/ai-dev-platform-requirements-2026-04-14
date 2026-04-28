# Codex GitHub Review Setup

Codex GitHub review has two planes.

- Terminal plane: verify login, inspect PR review artifacts, and request a review with `@codex review`.
- Codex settings plane: connect GitHub and enable Code review / Automatic reviews for each repository.

The settings plane is intentionally not a GitHub CLI toggle. OpenAI's Codex GitHub guide requires turning on Code review in Codex settings:
https://developers.openai.com/codex/integrations/github

## 1. Verify local auth

```bash
gh auth status
codex login status
codex cloud list --json --limit 1
```

`codex cloud list` confirms that the CLI can reach Codex Cloud with the logged-in ChatGPT account. It does not enable repository review by itself.

## 2. Open Codex review settings

```bash
open https://chatgpt.com/codex/settings/code-review
```

In the browser:

- connect GitHub if it is not connected
- make sure the target repository is granted access
- enable Code review for the repository
- enable Automatic reviews if every PR should be reviewed without an explicit comment

For private or newly created repos, repository access may need to be reconfigured from the GitHub connector/app configuration.

## 3. Validate from the terminal

From the consumer repo:

```bash
platform codex-review --target .
```

For a specific PR:

```bash
platform codex-review --target . --request-pr <PR_NUMBER>
```

The command skips duplicate `@codex review` comments by default. To post again:

```bash
platform codex-review --target . --request-pr <PR_NUMBER> --force-comment
```

Then check for a Codex review artifact:

```bash
gh pr view <PR_NUMBER> \
  --json reviews,comments,reviewDecision \
  --jq '{reviewDecision, reviews, comments: [.comments[].body]}'
```

A plain `@codex review` comment is only a request. The platform treats the review as complete after GitHub shows either a review from `codex` / `codex[bot]` or a `Codex Review:` comment from `chatgpt-codex-connector`.

## 4. Orchestrator behavior

The worker flow is:

1. create PR
2. wait for GitHub checks
3. wait for automatic Codex review
4. if no review arrives, post `@codex review`
5. if no Codex review artifact arrives after the fallback grace period, mark the Jira job `blocked`

This prevents a request-only comment from being mistaken for a completed review.
