# Improve and Document: stopwatch.py
Focus on `stopwatch.py`; ignore other files within the project. It is designed to be a standalone utility independent of other modules or files.

Review the script to understand its basic form and function, then help Plan two phases of work:

1. Improve `stopwatch.py` in any way that might be meaningful in regards to its stated design goals.

2. Create a markdown file that contains a prompt which describes the form and function of `stopwatch.py` in a way that would allow another LLM to recreate it from scratch; organize the markdown file so it is not specific to user's environment, but has a "fill in the blank" section on system specs such that the prompt can be shared with others on different systems.

Complete the improvements *first* before creating the prompt language -- the prompt should describe the tool in its improved condition.

## Specific Improvements

### Configuration: Keybinds
Refactor the configuration of stopwatch.py into globals defined at the top of the module; remove reference to hard-coded keys in the instructions; allow each key to be custimized: start, stop, reset, exit.

### Configuration: Appearance
Refactor such that the stopwatch's appearance is easily configurable.

### Configuration: Time-Color Association
Further refactor the color of the stopwatch display to define a custom color, then optionally define a series of alternative colors mapped to specific times; the stopwatch should change to these colors once those times have elapsed, e.g.,: default: green, 1.0: magenta, 5.0: yellow.

### Configuration: Optional Auto-Reset Time
Support the addition of an auto-reset time at which the stopwatch performs a stop/reset automatically. The default is off, or no auto-reset. If a value is defined, the behavior would be that once started, the stopwatch proceeds normally until reaching that time after which it resets.

Might as well add a "loop" option to this as well which includes a "restart" after each auto stop/reset when this mode is enabled.

### Any Refactoring, Re-engineering, or Performance Improvements
This utility was written some time ago by less capable LLMs. Evaluate it critically and suggest any changes. It is a simple module, so it's no big deal if we need to re-architect it if doing so will meaningfully improve its function.

## Prompt
Once improved as above, author a `stopwatch.md` file that describes, concisely and precisely, the function of this module in a way that will allow another LLM to plan its creation achieving similar results for other users on other systems.
