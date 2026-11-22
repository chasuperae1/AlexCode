# termui

A Claude Code–style terminal UI widget library built on [Textual](https://textual.textualize.io/):
virtualized lists, checkbox groups, prompt inputs, progress bars, hierarchical menus, status
panels, plus a switchable theme system.

## Features

- **Smooth rendering** — lists are virtualized (`ScrollView` + `render_line`); 100k entries
  render in about 1 ms per frame, with eased animation for cursor movement and scrolling.
- **Keyboard first** — every widget supports arrow-key navigation and shortcuts, no mouse needed.
- **Responsive** — switches between side-by-side and stacked layouts as the window width changes.
- **Theme system** — three built-in themes, `Ctrl+T` switches live, all widget styles reference
  theme variables.
- **Extensible** — two-layer override: `COMPONENT_CLASSES` plus `DEFAULT_CSS`.
- **Testable** — 15 automated cases driven headlessly through the Textual Pilot (maintained locally).

## Install and run

```bash
pip install textual>=8.2

# run the demo
python -m termui.demo
# or
python -m termui
```

Demo key bindings:

| Key | Action |
|---|---|
| `↑` `↓` | Move within a list, menu or checkbox group |
| `→` `←` | Expand / collapse a hierarchy node |
| `Space` | Toggle a checkbox; pause the progress animation |
| `A` | Select all / none in a checkbox group |
| `Enter` | Confirm selection, activate a menu item, submit input |
| `Tab` | Move focus between widgets |
| `Ctrl+T` | Switch theme |
| `Ctrl+R` | Jump the progress bar to a random value |
| `F1` | Show the shortcut help |
| `Ctrl+Q` | Quit |

Inside the prompt input: `↑`/`↓` walk the history, `Ctrl+L` clears it, and `/theme`,
`/expand`, `/collapse`, `/clear`, `/help` control the demo directly.

## Integrating with AlexCode

`alex/tui.py` in this repository is a full integration example: `python -m alex.tui` starts
the chat UI.

- **Chat area** — `ChatLog` (scroll container plus incremental widget mounting); assistant
  replies render through a `Markdown` widget, user messages get a background bubble
  (`.user-bubble`), streaming updates only the current widget (50 ms throttle), scrollbar hidden.
- **Message structure** — one exchange = user bubble → collapsible debug group (thinking and
  tool logs, collapsed by default, `Ctrl+O` expands) → Markdown answer; the answer container is
  mounted on the first text delta so it never appears above the debug group.
- **Meta line** — current model plus context tokens (used / total, percentage, distance to the
  compaction threshold), measured through `Model.count_tokens`.
- **Commands** — `/model [name]` switches models (reads `model_profiles` from
  `config/config.json` and persists the choice), `/reload`, `/clear`, `/theme`, `/tasks`, `/exit`.
- **Event stream** — consumes AgentScope's native events directly: `ThinkingBlock*` shows
  reasoning, `ToolCallStart/ToolResultEnd` shows tools and timings, `mcp__server__tool` is
  recognized as an MCP call, the `Skill` tool shows the skill being read; loaded rules and
  skills are listed at startup.
- **Task summary** — one line, `Tasks 2/3 · running: xxx`, hidden when no task is active.
- **Bottom** — `PromptInput` plus a meta line (model / context); the current action appears in
  the debug group header and only expands on demand.
- **Persistence** — the session is written only after `ReplyEndEvent`, so an interruption never
  leaves a truncated context.

## Widgets

### ScrollList — scrollable list

```python
from termui import ScrollList, ListItem

scroll_list = ScrollList([
    ListItem("Build", "compile + package", value="build"),
    ListItem("Deploy", "not configured", value="deploy", disabled=True),
])
```

| Member | Purpose |
|---|---|
| `items` / `current` | Item list / item under the cursor |
| `set_items(items)` | Replace data and reset the cursor |
| `cursor_index` | Current cursor index |
| `ScrollList.Highlighted` | Cursor moved message |
| `ScrollList.Selected` | Enter pressed message |

### CheckboxGroup — multi-select

```python
from termui import CheckboxGroup

group = CheckboxGroup(
    ["package artifacts", ("push image", "upload"), ("notify team", "notify", True)],
    title="Extra steps",
)
```

Options accept three forms: `"text"`, `(text, value)`, `(text, value, disabled)`. Messages:
`SelectionChanged`, `Submitted`.

### PromptInput — text input

```python
from termui import PromptInput

prompt = PromptInput(
    placeholder="type a command",
    validator=lambda value: "cannot be empty" if not value else None,
    history=["previous command"],
)
```

A failed validation highlights the field and emits `Rejected(value, reason)`; success emits
`Submitted(value)` and appends to history.

### ProgressBar

```python
from termui import ProgressBar

bar = ProgressBar(label="Build progress")
bar.advance_to(65)          # animate smoothly to 65%
bar.indeterminate = True    # indeterminate mode
```

### HierarchyMenu

```python
from termui import HierarchyMenu, MenuNode

menu = HierarchyMenu([
    MenuNode("File", expanded=True, children=[
        MenuNode("New", value="Ctrl+N"),
        MenuNode("Recent", children=[MenuNode("main.py")]),
    ]),
])
```

`Activated(path, node)` fires when Enter is pressed on a leaf; `path` is the full hierarchy path.

### StatusPanel

```python
from termui import StatusPanel

status = StatusPanel(text="ready")
status.notify("saved", level="success", timeout=3)
status.busy = True   # show the spinner
```

Levels: `info` / `success` / `warning` / `error`; colours come from the theme.

## Themes

```python
from termui import TermUIApp, ThemeSpec, THEMES, register_themes

# option 1: built-in themes (slate / slate-light / high-contrast)
class MyApp(TermUIApp):
    ...

# option 2: custom
THEMES["mine"] = ThemeSpec(
    name="mine",
    primary="#7aa2f7", secondary="#565f89", accent="#bb9af7",
    success="#9ece6a", warning="#e0af68", error="#f7768e",
    foreground="#c0caf5", background="#1a1b26",
    surface="#16161e", panel="#1f2335",
)
```

Widget CSS references only theme variables such as `$primary` / `$surface` / `$panel` /
`$text-muted`, so switching `app.theme` recolours every mounted widget immediately.

## Architecture

```
termui/
├── theme.py                 # ThemeSpec + theme registration and switching
├── app.py                   # TermUIApp base class (registers themes, Ctrl+T)
├── demo.py                  # demo application
└── widgets/
    ├── scroll_list.py       # virtualized list
    ├── checkbox_group.py    # checkbox group
    ├── prompt_input.py      # prompt input
    ├── progress.py          # progress bar
    ├── hierarchy_menu.py    # hierarchy menu
    └── status_panel.py      # status panel
```

Layering: theme (design variables) → widgets (state + keyboard + messages) → application
(assembly and interaction). Widgets never depend on each other; they communicate through
Textual messages.

## Customising and extending

1. **Restyle one widget** — subclass it and override `DEFAULT_CSS`.
2. **Fine-grained colours** — override the `COMPONENT_CLASSES` style classes.
3. **Add a widget** — implement `render()` (plain widgets) or `render_line()` (scrollable ones),
   and call `refresh()` when state changes.

```python
from termui import ScrollList

class CompactList(ScrollList):
    DEFAULT_CSS = """
    CompactList { border: none; background: $background; }
    """
```

## What is verified

Cases are maintained locally by the author and not published. Current coverage:

- list navigation / selection / disabled items, select-all in checkbox groups
- input validation and history
- progress animation and keyboard nudging
- menu expand / collapse / activation
- auto-dismissing status messages, theme cycling, narrow-width responsiveness
- rendering performance at 100k entries

After changing a widget, walk through the interactions above manually and note terminal size
and OS in the pull request.

## Performance

- Only visible rows are rendered; `virtual_size` is decoupled from the item count.
- Measured locally: 100k entries, 30 visible rows, about **1.08 ms** per frame (16.7 ms budget).
- Animation is delegated to the Textual animator; no custom frame loop.
- The status panel spinner refreshes at 15 Hz only while `busy`, with zero cost when idle.

## Terminal compatibility

Textual handles terminal capabilities (alternate screen, mouse, true colour, resize); widgets
never emit raw ANSI sequences.

| Terminal | Status |
|---|---|
| Windows Terminal | tested on Windows 11 (demo + full suite) |
| VS Code integrated terminal | tested on Windows 11 |
| iTerm2 / macOS Terminal | relies on Textual's cross-platform renderer; not tested on this Windows machine |
| Terminals without true colour | Textual falls back to 256 colours; themes still work |

Known limitation: legacy Windows `conhost` occasionally lags on resize; Windows Terminal is
recommended.
