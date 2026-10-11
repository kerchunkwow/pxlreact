# All-Party Bookcast (Buff)
We're gonna whip up a fancy feature for our friend LordMidoo (shotout)!

@bookcast.py implements book-casting for Might & Magic: shortcuts that select caster, magic school, spell, and an optional target for fast casting from player spellbooks.

We need a way to repeat a bookcast once for each player in the party to create an "all party buff" sequence. Review @bookcast.py and its immediate dependencies, then implement support for a looping bookcast that targets each PC in sequence with a busy/wait between each cast to allow the caster time to recover.

## Standalone, Integrate Later
Implement `bookcast-loop.py` as a separate module for now; avoid modifying `bookcast.py` until we validate new feature

## Flow/Loop
Building on `bookcast.py`, loop as:
1. select caster (if not already active)
2. open spellbook
3. click school
4. click/cast spell
5. click target
6. if targets remain, proceed to 7; else exit/complete
7. wait until caster is ready (poll busy state)
8. repeat from 1

## Portrait (PC) Coordinates
For now, use hard-coded values for x,y of each player portrait captured earlier. Add these to `remap.toml`:

```
pc1 = 827,1293
pc2 = 1127,1293
pc3 = 1427,1293
pc4 = 1727,1293
```

Use a list of coordinates, don't assume 4 PCs; future titles may have more.

## Short Circuit/Early Exit
Avoid situations where user gets "stuck" in a bookcast loop during combat or dangerous situation; capture `ESC` and implement an offramp to exit the sequence early and cleanup any state needed to allow clean restart (resume *not* necessary).

## User Validation
AI agent validation is unnecessary; `bookcast.py` is validated already, and the loop will require careful prep. Just implement and user will validate manually.

Do add basic terminal output messages reporting status/progress through a bookcast loop so any issues can be diagnosed.
