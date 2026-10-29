# Using your AI subscriptions through Hera (read before enabling)

Hera can drive the **official command-line tools** of some AI subscriptions as engines (`claude_code` for Claude Code, `antigravity` for Google's Antigravity `agy`). This page explains what that means, what Hera does and does not do, and what the vendors' terms say. It is not legal advice: the terms change, so read the current ones for your plan.

## What Hera does

- It runs the vendor's own CLI as a local subprocess (`claude -p ...`, `agy -p ...`), exactly as you could from a terminal, using the login **you** made in that CLI.
- It never reads, copies, stores, or forwards your subscription's OAuth token. The CLI talks to the vendor; Hera only sends the prompt and reads the output.
- The built-in account login of Hera's gateway is generic (you supply the `client_id` of your own application). Hera ships no Anthropic or Google subscription client IDs, and its only preset with a login flow is OpenRouter's key page.
- The router uses `claude_code` only as a last resort and says so when it does.

## What Hera does not do (and you should not do with it)

- It does not offer "Sign in with Claude/Google" for other people, and it must not be used to resell or share one subscription.
- Do not extract a subscription token and plug it into Hera as an API key or into any other third-party client.

## What the vendors say (checked 29 Oct 2026; verify before relying on it)

- **Anthropic.** The Claude Code legal page says OAuth authentication is for ordinary use of Claude Code and other native Anthropic apps, and that Pro/Max limits assume ordinary, individual use of Claude Code and the Agent SDK. Third-party tools may not route requests through Free/Pro/Max credentials. Source: [Claude Code legal and compliance](https://code.claude.com/docs/en/legal-and-compliance). Reports on enforcement and dates differ between secondary sources, for example [AlternativeTo](https://alternativeto.net/news/2026/2/anthropic-officially-bans-using-subscription-authentication-for-third-party-claude-use) and [WinBuzzer](https://winbuzzer.com/2026/02/19/anthropic-bans-claude-subscription-oauth-in-third-party-apps-xcxwbn/).
- **Google.** The Gemini CLI FAQ states that using third-party software to harvest or piggyback on Gemini CLI's OAuth authentication to reach Google's backend is a violation of the applicable terms, and recommends a Vertex AI or Google AI Studio **API key** for third-party agents. The Apache 2.0 license covers the CLI's code, not the service behind it. Source: [Gemini CLI FAQ](https://geminicli.com/docs/resources/faq/) and [Terms and privacy](https://www.geminicli.com/docs/resources/tos-privacy).

## Practical guidance

- Prefer **API keys** (many providers have free tiers; see `hera providers list --catalogo --gratis`) for anything automated or shared.
- If you enable a subscription engine, keep it personal and moderate, and accept that the vendor decides what counts as ordinary use.
- To turn the subscription engines off, set `engines.claude_code.enabled: false` and `engines.antigravity.enabled: false` in `config.yaml`.
