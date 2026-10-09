# AI answers

ChatGPT and Claude write their answers into the page while they are being
generated, and tools such as Claude Code and Codex read theirs from the
companies' APIs the same way. On a filtered account, the inspection proxy
reads those answers the way it reads a page, and the account's language
filter and content level apply to them. There is no separate setting: an
account whose pages have words replaced gets the same in its AI answers,
and an account whose pages are refused at "immodest" has its AI answers
refused at the same level.

The site has to be reachable first. An account whose categories or rules
block chatgpt.com, claude.ai or an API host never gets this far.

## Where it applies

- **ChatGPT and Claude in the browser.**
- **Claude Code**, which reads its answers from api.anthropic.com, and
  **Codex**, which reads them from chatgpt.com over a WebSocket when signed
  in with a ChatGPT account, or from api.openai.com with a key. Both were
  watched working through the proxy.
- **Anything else that talks to an AI API.** The proxy recognises the four
  shapes these APIs send, event by event: Anthropic Messages, OpenAI
  Responses, OpenAI Chat Completions (which xAI, Mistral, DeepSeek, Groq,
  OpenRouter, Together, Perplexity, Fireworks and Cerebras copy) and
  Gemini. So a program that uses one of those APIs is covered whichever
  host it calls, as long as the host is one the proxy reads
  (`SITES` in `kosherd/ai_proxy.py`).

A program that pins its own certificate, or that does not trust the
machine's certificate authority, cannot be read; that traffic fails rather
than passing. Claude Code and Codex both trust it through the environment
the image sets (see [what works](supported.md)).

## What is checked

- **The answer as it streams.** ChatGPT sends an answer as a series of
  pieces; the proxy checks each piece against everything that came before
  it. Words on the account's list are replaced before the piece reaches the
  page. The last part of what has arrived is held back until more arrives,
  so a word split between two pieces is still caught, and a short answer
  appears when it finishes rather than letter by letter.
- **The rendered copy.** ChatGPT also sends the answer a second time, as the
  page's own rendering, and again at the end as HTML. Those copies are
  checked too, so the page cannot show what the stream did not.
- **A conversation being opened.** When an old conversation is loaded, its
  messages are checked before the page gets them.
- **Pictures in an answer.** A picture carried inside an answer follows the
  account's picture level: hidden when the level is "all", otherwise judged
  by the same detector as a picture on a page. A picture downloaded by the
  page as a file goes through the ordinary picture filter.
- **What a tool is told to do.** When an assistant calls a tool (writes a
  file, runs a command), the arguments are held until they are complete and
  checked whole. They are never rewritten, because a changed word in a
  program is a changed program; a call that would need a change is refused
  instead, and the tool sees an error.

An answer that reads as the account's content level is not shown. The page
gets a short notice in its place: *This answer was not shown. It did not
pass the filter set for this account.* The activity log records it as an
AI answer that did not pass the filter, or one with bad language when the
language filter is set to block.

Everything else on these sites goes through untouched: scripts, sign-in,
the anti-bot handshake, uploads, settings. A reply the proxy cannot read
(an encoding it does not know, a stream that stops making sense) is
replaced by the notice, never passed on as it came.

## Limits

- ChatGPT in the browser (signed out), Claude Code and Codex were watched
  working through the proxy in October 2026. Signed-in ChatGPT, Claude in
  the browser and the other APIs use the stream shapes their documentation
  and public captures describe, and have not yet been watched live. These
  protocols are the companies' own and change without notice; when one
  changes in a way the proxy does not understand, the answer is refused,
  not shown unchecked.
- Text already shown cannot be taken back. A long answer that only earns its
  verdict near the end is cut off from that point, and what came before
  stays on the page.
- The word list and the content terms are the ones used for pages, with the
  same reach and the same misses. This is not a model judging meaning.
- Other chat sites (Gemini, Copilot, Perplexity, Grok and the rest) each
  have their own page protocol and are not read yet. A site whose answers
  come through one of the four API shapes above is covered once its host
  is added to the list.
