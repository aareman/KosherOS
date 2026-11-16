---
date: 2026-11-30
draft: true
slug: 30-days-of-development-on-kosheros
---

# 30 days of doing development on KosherOS

<!-- The opening paragraph goes here. What is above "more" shows on the blog's front page. -->

<!-- more -->

<!-- The post goes here. -->

## What shipped in November

<!--
The working list for this post: what merged to master in November 2026,
by the date it merged. Last brought up to date on November 10. Trim it
to what the post uses before `draft: true` comes off.
-->

### For developing on KosherOS

- **SSH to other machines.** An administrator can list the machines an account may ssh to, or open SSH anywhere for an account they trust with it. An entry may name its port. (Nov 2–3, [#258](https://github.com/aareman/KosherOS/pull/258), [#286](https://github.com/aareman/KosherOS/pull/286))
- **Sign in to an account over SSH, with a key.** (Nov 4, [#284](https://github.com/aareman/KosherOS/pull/284))
- **Nix apps are kept current like Flatpaks.** The Store's Updates shelf covers Claude Code, OpenCode, Ghostty and the other apps from the Nix shelf, and `kosher update --local` looks after the packages a developer added to their own profile. (Nov 4, [#288](https://github.com/aareman/KosherOS/pull/288))
- **AI answers pass the account's filter, on any site or tool.** (Nov 6, [#76](https://github.com/aareman/KosherOS/pull/76))
- **Hex dictation is in the Store.** (Nov 6, [#304](https://github.com/aareman/KosherOS/pull/304))
- **A dev VM that stays up.** A change to KosherOS itself can be tried on it in seconds. (Nov 2, [#260](https://github.com/aareman/KosherOS/pull/260))

### The filter

- **Page rules have three levels.** Block, allow, and a new bypass for a site trusted outright, where nothing is filtered. (Nov 2, [#278](https://github.com/aareman/KosherOS/pull/278))
- **Long videos play at the modesty levels.** So do videos a player fetches in pieces, which is most of them. The filter looks at a few moments of the video before letting it play. (Nov 6, [#277](https://github.com/aareman/KosherOS/pull/277))
- **A rabbi giving a shiur is no longer hidden because of the bookshelves behind him.** A person close to the modesty line is measured over their own pixels, not the box around them. (Nov 2, [#281](https://github.com/aareman/KosherOS/pull/281))
- **Pages stay responsive while many pictures are being checked.** Each picture arrives when it is ready. (Nov 9, [#298](https://github.com/aareman/KosherOS/pull/298))
- **Buying something online no longer fails at the last step.** The ad blocker was refusing the fraud check a shop runs before it accepts a card. (Nov 2, [#266](https://github.com/aareman/KosherOS/pull/266))

### The admin app and setup

- **A change in the admin app no longer restarts the filter,** and the page stays where it was. (Nov 4, [#297](https://github.com/aareman/KosherOS/pull/297))
- **Example pictures show what each picture setting does.** (Nov 4, [#296](https://github.com/aareman/KosherOS/pull/296))
- **The activity log can allow pictures** on a page, its folder or the whole site. (Nov 6, [#293](https://github.com/aareman/KosherOS/pull/293))
- **First-boot setup asks for the time zone.** (Nov 6, [#302](https://github.com/aareman/KosherOS/pull/302))
