---
title: "Incident report: the 14:35 deploy"
author: Dr. A. N. Other
date: 2026-10-19
tags: [tts, regression, "v1.3.0-rc.2"]
---

# Incident report: the 14:35 deploy that wasn't ##

> **TL;DR:** The deploy of v1.3.0-rc.2 failed at 14:35 CEST on Oct. 19, 2026, because
> `settings.py:281-283` read `PORT` as a string. Dr. Müller fixed it by 3 p.m. Then we
> all went home.
>
> > "Don't panic," said the on-call engineer. "It's only prod."

## 1. Files, ports and times

The traceback pointed at src/app/settings.py:281-283, then at main.rs:12:5, and finally at
README.md:3 and CHANGELOG.md:10-20. Line 0 of x.py:0 does not exist, yet the linter blamed
it anyway, and node.js:20 turned out to be a file, not a container tag.

Our service listens on localhost:8080, the database on 192.168.178.88:5432, and the public
site on example.com:443 (see https://example.com:8443/status?verbose=1&since=14:35 for the
dashboard). A second host, a.com:12, answers on a port nobody remembers. Docker maps
-p 8080:80 and pulls python:3.12-slim and node:20; neither of those is a port. IPv6 is
worse: [::1]:8080, fe80::1%en0 and 2001:db8::ff00:42:8329 all reach the same box, and
300.1.1.1:80 and localhost:70000 reach nothing at all.

The alert fired at 9:05, again at 14:35:20, and the maintenance window runs 14:30-15:00, or
23:59 to 00:01 if you count the retries. The recording shows the spike at 00:01:23.456, the
log line says 2026-10-19T14:35:00Z, and the server's offset is +02:00. On a 16:9 screen, 3:1
is the ratio of errors to warnings, and 25:99 is obviously not a time.

Also in the logs: C:\Users\ops\AppData\Local\Temp\crash.dmp, git@github.com:example/readaloud.git,
ssh deploy@build-01.internal -p 2222, and ~/.config/nvim/init.lua.

## 2. Names that look like other names

Set user.id, user_id, userId and getUserId, then pass idToken, but leave idle, grid, IDs and
id2 alone; ids is just the plural. The ASP.NET app talks to a .NET 8 service written in C#
and F#, while the CLI uses std::vector<int>, calls kubectl, journalctl and systemd, and runs
on k8s in AWS (aws in lower case, too). We store JSON, json, YAML and TOML in sqlite and
postgresql, publish to PyPI, and nobody can agree whether GIF is said with a hard G.

In prose, code still creeps in: if a != b && c -> d, then x => x * 2 || y, and a::b::c ends
the thought. Pass '' to skip the field, "" to clear it, and """ to open a docstring. The
unique_provider_subscription_id column and the macOS build both broke, ✓ for the first fix
and ✗ for the second.

## 3. Words that hold together, or should not

It's well-known that rock 'n' roll isn't dead; don’t trust the curly apostrophe either,
nor the modifier one in donʼt. The café's naïve résumé (spelled résumé here, with
combining accents) costs $4.50, €3, £20k or ¥1,000,000, which is 12.5% more than last
year's 1,000-unit run and 50%-off on Black Friday. The mlx_audio package speaks TCP/IP,
AT&T and HTTP/2, e.g. at 3:45pm, i.e. before tea, in the U.S.A. and the U.K. alike.

The 1st, 2nd, 3rd and 21st attempts used ½ cup, then 1/2 cup, of 3×4 bricks: 10^6 of them,
each 1e-9 m thick, ±0.5 mm, ≈3.14 wide and ≥ 99.9% pure, at −40 °C or 98.6°F. Her badge
reads 5'11", 0x1F, 0b1010 and v1.2.3; the lock file wants ^1.2.3, ~=2.0 and >=3.12,<3.13.
Week 2026-W42 runs from 19/10/2026 (or 10/19/2026 in the US), pages 10–12 cover the
1990s–2000s, and support is on +49 30 1234567, or #123 in the tracker, or @ops-team, or
#ff8800 if you mean the colour. The fix is commit 3da99aa, two behind HEAD~2.

## 4. Sentences that refuse to end

Fig. 3 shows the spike. Mr. Smith and Mrs. Jones, vs. Dr. No, etc., signed off. Then the
release notes went out. I said no. Then I left. J. R. R. Tolkien wrote it in 1937. The
answer was 3. Then it was 4. Wait… what? Really‽ He said "stop." Then he stopped. She
asked (quietly) "why?" and nobody answered. Pi is 3.14159 and e is 2.71828... roughly.
We moved to the U.S. Then we moved back.

Stop. Go. Wait. Look. Listen. Run. Hide.

This sentence is deliberately long, so that the chunker has to cut it somewhere sensible:
the deploy started at 14:30, the canary passed its health checks on localhost:8080 and on
192.168.178.88:5432, the rollout reached half the fleet without a single page, the error
budget still looked healthy; and then, at 14:35, the config loader read settings.py:281-283,
found a port written as a string, refused to coerce it, crashed every worker that touched
it — all of them, in every region — and left the on-call engineer staring at a dashboard
that had gone from green to red in less time than it takes to read this sentence aloud.

The checksum was sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08, and
the build log lives at https://example.com/ci/builds/2026/10/19/readaloud/pipelines/main/jobs/bundle-macos-14-arm64/artifacts/logs/full.txt?raw=true&lines=1-100000.

## 5. Markdown that should disappear

**Bold**, *italic*, _also italic_, ***both***, ~~struck out~~, and `inline code with a *
star`. But snake_case_names, 2*3*4 = 24 and file_name_v2.txt must stay as they are.
``A code span with a ` backtick inside`` is legal. \*Escaped asterisks\* stay literal, and
so does \_this\_.

Links: [the docs](https://example.com/docs "Docs title"), an image ![a red cross](cross.png),
an autolink <https://example.com/x_y_z>, a bare URL https://example.com/a_b*c, then
www.example.org, mailto:ops@example.com, write to ops@example.com. Also a reference link to
[the spec][spec], and a footnote.[^1] (See https://example.com/path.)

<kbd>Ctrl</kbd>+<kbd>C</kbd> copies, &amp; and &lt;tags&gt; are entities, &copy; 2026, and a
non-breaking&nbsp;space hides here. <!-- This comment must never be read aloud. -->
Two trailing spaces end this line  
and this one starts after a hard break.

- Top level with `--flag=value` and -rf
  - Nested with $HOME and ${VAR:-default}
    - Deeper with a regex ^\d+$ and #include <stdio.h>
- [ ] An open task
- [x] A done task ✓

1. Ordered
2. Still ordered
   3) An odd marker

---

***

```python
def main(user_id: str) -> None:  # settings.py:12
    print(f"{user_id!r} != ''")
```

~~~bash
#!/usr/bin/env bash
curl -fsSL https://localhost:8443/health && echo ok || exit 1
~~~

    indented code: kubectl get pods -n kube-system --since=14:35

Emoji: 🎉, 👩‍💻, 🇩🇪 and ✔️, in the middle of a sentence.

## 6. Tables

| Feature | macOS | Linux | Windows | Notes |
|:--------|:-----:|:-----:|:-------:|------:|
| Media keys | ✓ | ✗ | ✗ | ⌘ + F8 on a Mac |
| Ports | localhost:8080 | 0.0.0.0:80 | [::1]:443 | `-p 8080:80` |
| Files | settings.py:281-283 | main.rs:12:5 | C:\temp\a.txt | see [the docs](https://example.com) |
| Keys | ⌘C | Ctrl+C | Ctrl+C | `.` · `→` |
| Empty |  |  |  | ✓³ |
| Wide | 東京 | Straße | naïve | a pipe \| in a cell |
| Long | This cell holds a deliberately long sentence that mdcat will have to wrap across several lines, so the reader must stitch it back together without losing a word. | short | short | 12.5% |

| Only a header |
|---------------|

## Closing hashes ##

That's all, folks! ...or is it?

[^1]: The footnote, with user.id and settings.py:498 in it.

[spec]: https://example.com/spec "The spec"
