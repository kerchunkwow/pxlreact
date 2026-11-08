# cdshape.py
Implement standalone module providing visual indicators of time passing and the expiration of predefined periods. *Purpose*: of this module is to trigger "countdown" or "cooldown" shapes on screen that serve as visual indicators of the passage of time since a given key has been pressed.

Consider the concept of "cooldowns" in popular games; we are creating shapes that provide customizable visual feedback on cooldown periods.

## Start: stopwatch.py
Use `stopwatch.py` as a reference/starting point; stick to core design principles: always on top, easily configurable, lightweight, low dependency.

## Features
Instead of on-screen stopwatch with digits, cdshape.py uses simple geometric shapes and rudimentary animation to show time passing.

`cdshape.py` supports:
- `multiple timers`: use key->timer map to configure multiple indicators associated with different keys (and different indicators)
- `configurable timer visuals`: choose from simple shapes, and "start empty and fill" or "start full and drain" visual behaviors to show the passage of time
- `performance, precise updates`: shapes & animation that can achieve ~20ms precision consistently on modern hardware; rectangles/bars that fill or drain, circles that "sweep" like a clock, etc.
- `expiration indicator`: a clear visual and optionally audiotory event triggered on expiration of a given time to alert user to its passing

Each timer configurable position; ideally independent drag-and-drop; if too complex, allow position configured at runtime.

## Design
Practice separation of concerns, allowing the definition of an "indicator" to be kept separate from its assocation with a given key. Different shapes/behaviors of indication should easily be attached to new keys and time periods/durations without interruption.

## Do not Alter stopwatch.py
Create a new, independent module; do not alter the existing `stopwatch.py` file.
