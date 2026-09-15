# Time limits: how long, and when

Besides *what* a person may reach, a parent can say *how long* and *when*
the computer may be used at all. Both are set per account on the **Time**
tab of the person's page in KosherOS Admin, and both are off until a
parent turns them on: an account nobody has thought about has no daily
limit and may sign in at any hour, so a family that never opens the tab
sees no change.

Administrators are never limited. A parent must always be able to sign in
and change a setting; to limit somebody, make their account a user account
first.

## A daily limit

"Two hours a day" means two hours of *using* the computer. The count runs
while the person is signed in and at the keyboard; a screen left to go idle
does not count, and neither does time signed out. It starts again at
midnight, and a restart does not hand the day back — the count is kept on
disk.

The one-click choices are 30 minutes, 1, 2 and 3 hours, and no limit; "a
different amount" takes any number of minutes.

## Allowed hours

The calendar is a week: seven days across, the hours of the day down. A
parent clicks or drags across it to paint when the account may be used.
Amber hours are allowed and blue hours are not — the same colours as
everywhere else in the admin app, where blue is the filter holding
something and amber is something left open.

Four starting points can be painted with one click and then adjusted:

| Preset | Hours |
|---|---|
| Always | any hour of any day |
| After school | school days 3 pm – 8 pm, weekends 9 am – 8 pm |
| Not late at night | every day 6 am – 9 pm |
| Weekdays only | Monday to Friday at any hour, not at the weekend |

## What the person sees

Nobody is signed out without warning. Fifteen minutes before the daily
time runs out, or before the allowed hours end — whichever comes first —
a notification appears on the person's screen; another at five minutes;
and a last one when the time is up, saying the account will be signed out
in a minute. The screen locks at that point and the session ends a minute
later. Signing in again before the account is allowed is refused at the
login screen.

The person can see all of this for themselves: **My Filter** shows their
daily limit, how much of today is left, and today's allowed hours. A
person who can read their own rules accepts them far more readily than
one who only ever meets a locked screen.

## What the parent sees

Every card on the family board carries a line such as "1 h 20 min used of
2 h", and the Time tab shows the same together with when today's allowed
hours end. A session the filter ended, or a sign-in it refused, is written
to the activity log like a blocked page, so "why was Yosef signed out at
9 o'clock?" has an answer in the same place as everything else.

Changing a time limit takes the guardian password, when one is set, like
every other change that can loosen the protection.

## How it is enforced

The limits are enforced by the system daemon, kosherd, and by nothing the
person's own session controls:

- Once a minute kosherd asks systemd-logind who is signed in and whether
  they are idle, and adds the minute to each active, limited account's
  count (kept in `/var/lib/kosher-time/`).
- Warnings are a signal on the system bus; a small helper in each session
  (`kosher-time-notify`) turns them into desktop notifications. Killing the
  helper loses the warning, not the limit.
- When the time is up, kosherd locks the person's sessions through logind
  and ends them a minute later.
- New sign-ins are gated by `pam_time`, reading `/etc/security/time.conf`,
  which kosherd renders from the policy on every change: each limited
  account's allowed hours, and a line refusing every hour for an account
  whose daily time is used up, replaced when the new day starts. A refusal
  at the login screen is noted in the activity log.
- A session that appears while the account is blocked — a console login,
  say — is ended at once and logged as a refused sign-in.
