Review the following markdown in full; implement `mm8-capture.py`, update `mm_status.json` and the methods that utilize its data as necessary.

# MM8 Party Status Updater
`Might & Magic 8` presents a new challenge for the remapper utility, specifically the function of `mm_status.json` in driving targeting, casting, and selection of party members.

In MM8, party size is *dynamic* and the party UI is *centered*. Thus, adding new party members *shifts the position* of every other party member, disrupting `mm_status.json` settings.

The current process for updating party `mm_status.json` is too burdensome to repeat every time party size changes, an *automated capture method* is required. 

Implement `mm8-capture.py` borrowing methods as necessary from similar tools or the parent `pxlreact` project. When imported into the `remapper`, bind the recapture pipeline to `CTRL-ALT-8`.

## Y-Coordinate Consistency
Changing party size shifts party members *horizontally*, but the *vertical* position of UI elements remains constant. We can use this as a basis for detecting UI components and quickly updating `mm_status.json`.

## Ready Status

### Busy -> Ready
Invert the `busy` property in `mm_status.json` to record `ready` status instead; the color of ready status is more distinct from background UI elements so detection should be more reliable.

*Minimize downstream impacts* of this change - update the current method for checking *busy* so it simply returns *not ready* and all other logic should remain unchanged.

### Ready Jewels
In MM8, ready status is indicated by a bright green jewel whose *vertical center* lies at `y = 1411`; each ready jewel can be identified by three horizontal pixels in sequence:

[#6cf0bc][#7bffce][#6aeeb9]

Scanning left-to-right, each such sequence will represent the position of a player's ready-status jewel; there will be between *one* and *five* to discover. In each case, the center pixel can be set as that PC's ready-status indicator pixel; they are ready when the pixel color is #7bffce, and otherwise busy.

## Selected Status

### Unselected -> Selected
Like the update to ready status, invert the `unselected` property in the status data so it records `selected` status instead; again a more distinct color and just conceptually easier to "read." Update the stauts method to return *not selected* where appropriate.

### Selection Aura
In MM8, player selection auras crest above the player portraits at `y = 1203`; each can be recognized when three identical pixels appear in sequence:

[#ffffe7][#ffffe7][#ffffe7]

The same left-to-right, center-pixel strategy used for ready jewels can be repeated here. A *reuseable method* is appropriate here.

## Portraits
Player *portrait positions* (i.e., where to click to target them) can be *derived* from their selection aura position as these are rings around the portraits themselves. 

Once the indicator pixel for selected status is known, set that player's portrait position to `200` pixels below (`portrait_y = selected_y + 200`).

## Health Bars
For capturing health bar locations, assume all players are at *full health* at the time of status capture.

### Top-Y
The top of player healthbars can be found at `y = 1305` and their center pixels appear as a two-pixel sequence:

[#60c268][#60c268]

Either of these pixels can be used as the *vertical* (`x`) component when measuring *health percentage*.

### Bottom-Y, Status Colors
For the purposes of *health percentage* calculations, the bottom of healthbars appear at `y = 1436`, moderate health is indicated as #dee342 and low health as #e72000.

## Overlay Confirmation
To assist with validation and ongoing reliability, implement *on-screen visual confirmation* of the capture routine using temporary overlays on the game screen:

For ready, selected, and portrait elements, render a rectangle surrounding the location of the detected element, #000000 border and *semi-transparent* backdrop; highlight the indicator pixel(s) in pure magenta #ff00ff. Label each rectangle, `8pt Consolas`, top-center alignment above the rectangle, concise labels: `p# rdy`, `p# sel`, `p# por`.

For each healthbar, bound the vertical region in a similar rectangle, and label relative positions `100%`, `50%`, and `1%` using the respective health colors above (regardless of the current color of the healthbars which will be green).

These confirmation indicators should render *temporarily* for approximately ~5 seconds after the capture routine has completed, then they should disappear completely. They exist only to allow the user to confirm that detection was successful.

## Relative X-Coordinates
Although the position of party members shifts left-to-right as party size changes, their **relative** positions should remain relatively constant. e.g., player 2's ready-jewel should always be the *same distance* from player 1's, and likewise for selection auras, healthbars, etc.

*Report* on these *relative distances* in the terminal for now, as they may prove useful in refining detection algorithms in the future.

## Color Tolerance
Start with the assumption that the colors above can be *precisely* detected using their current values, but build a *configurable tolerance* into detection routines in case observed behavior indicates unexpected fluctuations.

## Data Updates
Ensure `mm_status.json` is kept free of stale data; when a recapture is triggered, all prior data should be considered defunct to be replaced by the newly-detected components; this includes the scenario in which the party *gets smaller* and an entire previous player needs to be scrubbed from the party status.

## Considerations & Constraints
- Do not spend effort on *backward compatibility* with any previous solution for party status capture, updating this data, etc. This is the new canonical method for maintaining `mm_status.json`
- Practice responsible *reuse*; UI elements for overlays should already exist elsewhere; avoid *reimplementing* existing functionality if it can be inherited.
- Party size will always be *between 1 and 5*; capturing `0` or `>5` should both be considered an error.
- Supplement on-screen validations with terminal output if necessary to report critical info (e.g., failure conditions)
