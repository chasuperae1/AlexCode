"""AlexApp 的终端样式表（从 tui.py 拆出，便于独立维护）。"""

APP_CSS = """
    /* 纯黑背景，与 cmd 窗口融为一体 */
    Screen { background: $background; color: $foreground; layers: base overlay; }
    #main {
        layer: base;
        height: 1fr;
        padding: 0;
    }
    #topbar {
        height: auto;
        border: round $primary 30%;
        padding: 0 1;
        margin-top: 1;
    }
    #meta {
        width: 1fr;
        height: 1;
        color: $text-muted;
    }
    .source-link {
        width: auto;
        height: 1;
        margin-right: 3;
        color: $text-muted;
    }
    #settings-link {
        margin-right: 0;
    }
    #settings-spinner {
        width: 2;
        height: 1;
        color: $primary;
        display: none;
    }
    .source-link:hover {
        color: $primary;
        text-style: underline;
    }
    #tasks {
        width: auto;
        height: 1;
        padding: 0 1;
        color: $text-muted;
        display: none;
    }
    #tasks.-visible { display: block; }
    #progress-tag {
        width: auto;
        height: 1;
        padding: 0 1;
        color: $primary;
        display: none;
    }
    #progress-tag.-visible { display: block; }
    #progress-tag.-warn { color: $warning; }
    #progress-tag.-ok { color: $success; }
    #progress-tag.-bad { color: $error; }
    #progress-tag.-muted { color: $secondary; }
    #chat {
        height: 1fr;
        border: round $primary 30%;
        margin-top: 1;
    }
    #bottom { height: auto; background: $background; margin-top: 1; }
    #commands {
        height: auto;
        max-height: 8;
        display: none;
        background: $surface;
        border: tall $primary 40%;
        padding: 0 1;
        margin: 0 0 1 0;
        scrollbar-size-vertical: 0;
    }
    #commands.-visible { display: block; }
    #commands > .option-list--option-highlighted {
        background: $primary 30%;
        color: $foreground;
    }
    #bottom-toolbar {
        height: 1;
        align-vertical: middle;
    }
    #bottom-spacer { width: 1fr; height: 1; }
    #prompt { background: $background; }
    .queue-line {
        padding: 0 1;
        margin-top: 1;
    }
    .queue-summary {
        padding: 0 1;
        margin-top: 1;
    }
    #prompt .text-area--placeholder { color: $text-muted; }
    #statusbar {
        height: 1;
        padding: 0 1;
        margin: 1 0 1 0;
    }
    #workspace-line {
        width: 1fr;
        height: 1;
        color: $text-muted;
    }
    #task-status {
        width: auto;
        max-width: 55%;
        height: 1;
        text-align: right;
        overflow: hidden;
        text-overflow: ellipsis;
        display: none;
    }
    #task-status.-visible { display: block; }
    #jump {
        width: auto;
        height: 1;
        padding: 0 1;
        color: $primary;
        text-style: underline;
    }
    #jump:hover {
        color: $accent;
        text-style: bold underline;
    }
    #subagents {
        height: 1fr;
        border: round $primary 30%;
        margin-top: 1;
        padding: 1 2 0 2;
        display: none;
    }
    .sa-board {
        height: 1fr;
        width: 1fr;
    }
    .sa-page-head {
        height: 1;
        margin-top: 1;
    }
    .sa-title {
        width: 1fr;
    }
    .sa-badges {
        width: auto;
    }
    .sa-sub {
        height: 1;
        color: $text-muted;
        text-wrap: nowrap;
        text-overflow: ellipsis;
        overflow: hidden;
    }
    #sa-list {
        height: 1fr;
        padding: 1 0 0 0;
        scrollbar-size-vertical: 0;
    }
    #sa-cards {
        height: auto;
        width: 1fr;
    }
    .subagent-card {
        height: auto;
        border: round $primary 22%;
        border-left: thick $primary;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    .subagent-card.waiting {
        border-left: thick $warning;
    }
    .subagent-card.done {
        border-left: thick $success;
    }
    .subagent-card.failed {
        border-left: thick $error;
    }
    .subagent-card.-selected {
        background: $primary 12%;
        border: round $primary 45%;
        border-left: thick $primary;
    }
    .subagent-card.waiting.-selected {
        background: $warning 10%;
        border-left: thick $warning;
    }
    .subagent-card.done.-selected {
        border-left: thick $success;
    }
    .subagent-card.failed.-selected {
        border-left: thick $error;
    }
    .sa-head {
        height: 1;
    }
    .sa-cursor {
        width: 2;
        color: transparent;
    }
    .subagent-card.-selected .sa-cursor {
        color: $primary;
    }
    .sa-icon {
        width: 2;
    }
    .sa-name {
        width: 1fr;
        text-wrap: nowrap;
        text-overflow: ellipsis;
        overflow: hidden;
    }
    .sa-pill {
        width: auto;
        margin: 0 1;
    }
    .sa-meta {
        width: auto;
    }
    .sa-desc {
        height: auto;
        max-height: 2;
        padding: 0 0 0 2;
        color: $text-muted;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .sa-line {
        height: 1;
        padding: 0 0 0 2;
        text-wrap: nowrap;
        text-overflow: ellipsis;
        overflow: hidden;
    }
    .sa-perm-head {
        height: 1;
        padding: 0 0 0 2;
    }
    .sa-perm-sub {
        height: 1;
        padding: 0 0 0 2;
        color: $text-muted;
        text-wrap: nowrap;
        text-overflow: ellipsis;
        overflow: hidden;
    }
    .sa-perm-hint {
        height: 1;
        padding: 0 0 0 2;
        color: $text-muted;
    }
    .sa-page-foot {
        height: 2;
        margin-top: 1;
        border-top: dashed $primary 20%;
    }
    .sa-foot-hint {
        width: 1fr;
        color: $text-muted;
    }
    .sa-foot-count {
        width: auto;
        color: $text-muted;
    }
    ChatLog .subagent-row {
        height: 1;
        padding: 0 1;
        margin: 0;
        text-wrap: nowrap;
        text-overflow: ellipsis;
        overflow: hidden;
    }
    ChatLog .subagent-row:hover {
        background: $primary 14%;
    }
    ChatLog .subagent-notice {
        height: 1;
        padding: 0 1 0 2;
        margin: 0 0 0 1;
        border-left: thick $warning;
        text-wrap: nowrap;
        text-overflow: ellipsis;
        overflow: hidden;
    }
    ChatLog .subagent-notice:hover {
        background: $warning 14%;
    }
    """
