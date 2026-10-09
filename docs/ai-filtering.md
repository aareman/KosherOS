# AI answers

ChatGPT and Claude write their answers into the page while they are being
generated. On a filtered account, the inspection proxy reads those answers
the way it reads a page, and the account's language filter and content level
apply to them. There is no separate setting: an account whose pages have
words replaced gets the same in its AI answers, and an account whose pages
are refused at "immodest" has its AI answers refused at the same level.

The site has to be reachable first. An account whose categories or rules
block chatgpt.com or claude.ai never gets this far.

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

- ChatGPT was watched working in a real browser, signed out, in October
  2026. Signed-in ChatGPT and Claude use the stream shapes their
  documentation and public captures describe, and have not yet been watched
  live. These protocols are the sites' own and change without notice; when
  one changes in a way the proxy does not understand, the answer is refused,
  not shown unchecked.
- Text already shown cannot be taken back. A long answer that only earns its
  verdict near the end is cut off from that point, and what came before
  stays on the page.
- The word list and the content terms are the ones used for pages, with the
  same reach and the same misses. This is not a model judging meaning.
- Desktop apps and command-line tools that talk to the AI companies' APIs
  directly are not covered by this page's checks. They are the next part of
  the same work.
