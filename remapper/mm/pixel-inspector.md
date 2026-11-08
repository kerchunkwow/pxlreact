# Pixel Inspector
Remove/sunset the pixel-report functionality currently bound to `CTRL-ALT-C` and build `pixel-inspector.py` as a standalone module (imported by remapper at runtime) that exposes a more robust solution:

- On `CTRL-ALT-C`, begin polling the pixel directly under the mouse cursor as well as adjacent pixels in a dynamically configured region surrounding the cursor
- Simultaneously, open a *minimal*, *always-on-top*, *movable* window that displays large versions of the polled pixels, updated dynamically as the mouse moves (i.e., a pixel magnifier)

*While this window is visible and polling is active*, respect these keybinds:
- `ESC` immediately quit monitoring, close the window, and be sure to do any necessary cleanup to release keys, etc.

- `right-click` **outside the polling window** pauses polling effectively "freezing" the currently displayed pixels; a second `right-click` resumes polling

- `right-click` **within the polling window** copies details of the indicated pixel on to the Windows clipboard for rapid pasting into other applications; format as JSON with relevant details about the pixels location and color in HEX and RGB

**Note**: The location of the pixel on the monitor beneath the mouse, NOT within the inspector window; the goal is to "extract" pixel data from the game screen during play, so it has to be the x,y that corresponds to that position in the game.

## GUI Polish
- Use the minimal necessary components for the pixel monitor window: borderless, no scrollbars, minimal titlebar
- Use a small, monospaced font for any UI text/labels
- Ideally, remember the location of the window on exit so it opens in the same place the next time

## Reuse
As usual, try not to implement anything wholly new that can be borrowed from the `pxlreact` parent project.

