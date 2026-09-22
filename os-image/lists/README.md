# The word lists, one file per language

The filter ships three word lists in `os-image/files/usr/share/kosher/`:

| Shipped file | What it is for |
|---|---|
| `wordlist.json` | bad language, replaced with a milder word on the page |
| `content-terms.json` | the words a page is judged by, with a level and a weight |
| `search-blocklist.json` | searches that will not run at all |

Those three are **built, not edited**. The source is this directory: one
file per language, each holding that language's share of all three
lists. Edit the language file, then run

    python3 scripts/build-lists.py

(or `just lists`) to regenerate the shipped files. A test fails when the
shipped files are out of date, so the two cannot drift apart. The shipped
files stay flat because the daemon, the proxy, the search service, the
portal and the admin app all read them and none of them care what
language a word is in; a maintainer does, which is what this directory
is for.

## A language file

```json
{
  "language": "he",
  "name": "Hebrew",
  "comment": "what this list covers and what it leaves out, and why",
  "replacements": {"word": "milder word"},
  "searches": ["term", "..."],
  "content": {
    "nsfw":       {"25": ["..."], "12": ["..."]},
    "suggestive": {"25": ["..."], "12": ["..."], "6": ["..."]},
    "immodest":   {"12": ["..."], "8": ["..."], "4": ["..."], "3": ["..."]}
  }
}
```

The levels and weights mean what they mean in `kosherd/content.py`:
25 points convicts a page at that level, points count toward every milder
level, one distinct term never convicts on its own, and a term stops
earning after a few repeats.

## What the matcher does for you, so the list does not have to

See `kosherd/textfold.py` for the details. In short:

* A word is matched as a **whole word in any script**, and for Hebrew and
  Arabic also behind the prefixes those languages glue onto a word
  (והביקיני, والبورن). List the bare word.
* **Vowel points and accents between letters are ignored**, so a pointed
  word is the same word. List it unpointed.
* **An accented letter also matches its plain spelling** — cabrón catches
  cabron, kurwa mać catches kurwa mac, sikiş catches sikis — but only for
  the acute, grave, circumflex and cedilla, and a tilde on a or o. ñ, ö,
  ü, ő, ı, ğ, ł, ż and the like are their own letters and match only
  themselves. ß matches ss, Russian ё matches е, the Arabic alef variants
  match each other and the Persian kaf and ya match the Arabic ones.
* The English plural (`s`, `es`) is added to every content term. No other
  inflection is: **list the forms** a language actually uses.

## What to leave out

A wrong entry costs more than a missing one. On the bad-language list it
rewrites a word on every page in every language that uses the same
alphabet, because every Latin-script list shares one matcher. On the
content list it earns points on every page. So before adding a word ask:

* Is it also an **ordinary word** in this language? זין is the letter,
  כוס is a cup, polla is a hen, troia is Troy, Schwanz is a tail, uda is
  "will succeed", kussen are pillows.
* Is it also an ordinary word in **another Latin-script language**, or
  in English? Spanish negro is the colour black, French string and body
  are programming words, Turkish mayo is mayonnaise, Turkish bok is a
  book in Swedish, piç folds to pic.
* Does the **folded spelling** collide with anything? The build reports
  every pair of entries that fold together.

Each file's `comment` records the words that were considered and left
out, so nobody has to rediscover why.

## Shared words

English is merged first, then the other languages by code. A word two
languages spell identically (puta, idiota, porno) is kept once, with the
first language's replacement; `python3 scripts/build-lists.py --verbose`
lists every such word. Choose a replacement that reads in both, or leave
the word to the language that got there first.
