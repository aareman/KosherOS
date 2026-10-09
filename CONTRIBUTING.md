# Contributing to KosherOS

The full guide is on the site: [How to contribute](https://aareman.github.io/KosherOS/contributing/).
The short version:

1. **Start from an issue.** If there is no issue for what you want to do,
   open one. Work on the way to the first full release is on the milestone
   *The Road to v1.0.0*.
2. **Set up with devenv.** `devenv shell`, then `just test`. The shell
   installs the git hooks; nothing is installed on the host.
3. **Add a test for what you changed.** Unit layer if you possibly can;
   UI work gets a widget test that builds the real screen.
4. **Commit in small steps** with a conventional prefix and a subject
   written for the person running KosherOS, for example
   `fix(youtube): the rules cover its API hosts, not only its site`.
   A `feat` moves the minor version; anything else moves the patch.
5. **Sign off your commits** with `git commit -s`. KosherOS is intended to
   be released under AGPL-3.0-or-later, and the sign-off is the
   [Developer Certificate of Origin](https://developercertificate.org/).
   There is no contributor licence agreement.
6. **Open a pull request** against `master` that links its issue with
   `Closes #N` and is on the milestone.

Always welcome without asking first: a site or a word the shipped lists
miss, a sentence a parent would not understand, a claim in the docs that a
booted machine proved wrong.
