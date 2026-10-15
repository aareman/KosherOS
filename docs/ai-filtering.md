# AI answers

AI sites and tools write their answers as they are generated: ChatGPT and
Claude into the page, Claude Code and Codex from the companies' APIs, and a
thousand smaller sites in formats of their own. On a filtered account, the
inspection proxy reads those answers the way it reads a page, and the
account's language filter and content level apply to them. There is no
separate setting: an account whose pages have words replaced gets the same
in its AI answers, and an account whose pages are refused at "immodest" has
its AI answers refused at the same level.

The site has to be reachable first. An account whose categories or rules
block a site never gets this far.

## How it finds an answer

There are more AI sites than anyone can list, so the proxy does not go by a
list alone. It reads, on any host:

- **Any streamed text.** An answer that arrives as it is generated comes as
  an event stream or as JSON one line at a time. Every such reply on a
  filtered account is read, whatever the host.
- **The reply to anything that looks like an AI call.** A request whose body
  names a model and carries messages, input, contents or a prompt is a call
  to a chat model, whatever the host. Its reply is read even when it is not
  streamed.

What it finds inside is handled by shape, not by site:

- **Four API shapes get the full treatment:** Anthropic Messages (Claude
  Code, the Claude apps), OpenAI Responses (Codex, and the ChatGPT backend
  it talks to), OpenAI Chat Completions (copied by xAI, Mistral, DeepSeek,
  Groq, OpenRouter, Together, Perplexity, Fireworks, Cerebras and most
  self-hosted gateways) and Gemini. Text is checked piece by piece against
  everything that came before it, with the last part held back until more
  arrives, so a word split between two pieces is still caught.
- **ChatGPT's own page stream** gets the same, with its rendered copy and
  final HTML copy checked too.
- **Any other shape** gets the general treatment: every string that reads as
  prose (it has words with spaces between them, or is a word on the list)
  gets the language filter, and the prose of the whole stream is scored
  together. Identifiers, tokens and URLs are left alone. A word split
  between two events of an unknown shape is not caught; that is the price of
  not knowing the shape.

On the sites and APIs the proxy knows, a reply it cannot read is refused.
On a host it does not know, a reply it cannot read is left alone, so a
notification stream on some unrelated site keeps working.

## What happens to an answer

- Words on the account's list are replaced before the piece reaches the
  page or the tool.
- An answer that reads as the account's content level is not shown. On
  ChatGPT the page gets a short notice in its place: *This answer was not
  shown. It did not pass the filter set for this account.* Elsewhere the
  stream ends with an error the site or tool shows in its own words. The
  activity log records it either way.
- A picture carried inside an answer follows the account's picture level:
  hidden when the level is "all", otherwise judged by the same detector as a
  picture on a page.
- When an assistant calls a tool (writes a file, runs a command), the
  arguments are held until they are complete and checked whole. They are
  never rewritten, because a changed word in a program is a changed program;
  a call that would need a change is refused instead, and the tool sees an
  error.
- Everything else goes through untouched: scripts, sign-in, anti-bot
  handshakes, uploads, settings. A refusal never forwards what it refused.

## Limits

- Watched working through the proxy in October 2026: ChatGPT in the browser
  (signed out), Claude Code, Codex. The other shapes and hosts follow their
  published formats and public captures and have not been watched live.
- Text already shown cannot be taken back. A long answer that only earns its
  verdict near the end is cut off from that point.
- The word list and the content terms are the ones used for pages, with the
  same reach and the same misses. This is not a model judging meaning.
- A site that streams its answers over a WebSocket, or over a format that is
  not text (Grok uses gRPC), is read only where the proxy knows the site.
  A program that pins its own certificate, or does not trust the machine's
  certificate authority, cannot be read; that traffic fails rather than
  passing. Claude Code and Codex trust it through the environment the image
  sets (see [what works](supported.md)).
