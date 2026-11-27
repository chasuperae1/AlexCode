"""设置界面的分区与卡片 CSS。"""

SETTINGS_SECTIONS_CSS = """
    SettingsScreen .settings-memory-label {
        height: 1;
        color: $text-muted;
        text-style: bold;
    }
    SettingsScreen .settings-memory-value {
        height: 1;
        color: $foreground;
        text-style: bold;
    }
    SettingsScreen .settings-memory-stat.-missing .settings-memory-value {
        color: $text-muted;
    }
    SettingsScreen .settings-memory-sub {
        height: auto;
        color: $text-muted;
    }
    SettingsScreen .settings-memory-count {
        dock: right;
        width: auto;
        height: 1;
        margin-left: 1;
        color: $text-muted;
    }
    SettingsScreen .settings-memory-row {
        height: auto;
        padding: 0 1;
        border-top: solid $primary 12%;
    }
    SettingsScreen .settings-memory-row:focus {
        background: $primary 20%;
    }
    SettingsScreen .settings-memory-list {
        height: auto;
        max-height: 15;
        scrollbar-size-vertical: 1;
    }
    SettingsScreen .settings-memory-text {
        width: 1fr;
        height: auto;
    }
    SettingsScreen .settings-memory-name {
        height: 1;
        color: $foreground;
        text-style: bold;
    }
    SettingsScreen .settings-memory-snip {
        height: 1;
        color: $text-muted;
        text-overflow: ellipsis;
    }
    SettingsScreen .settings-memory-meta {
        width: auto;
        height: auto;
        margin: 0 1;
        color: $text-muted;
    }
    SettingsScreen .settings-memory-view {
        width: 3;
        height: 1;
        color: $text-muted;
    }
    SettingsScreen .settings-memory-row:focus .settings-memory-view {
        color: $accent;
    }
    SettingsScreen .settings-memory-empty {
        height: auto;
        padding: 1 1;
        color: $text-muted;
    }
    SettingsScreen #settings-skill-list,
    SettingsScreen #settings-rule-list,
    SettingsScreen #settings-mcp-list,
    SettingsScreen #settings-token-list {
        display: none;
        width: 1fr;
        height: auto;
    }
    SettingsScreen #settings-preference {
        display: none;
        width: 1fr;
        height: auto;
    }
    SettingsScreen #settings-pref-card {
        width: 1fr;
    }
    SettingsScreen .settings-pref-row {
        height: auto;
        padding: 1 1 0 1;
        align-vertical: middle;
    }
    SettingsScreen .settings-switch-wrap {
        width: auto;
        height: auto;
        align-vertical: middle;
        margin-left: 2;
    }
    SettingsScreen .settings-mode-switch {
        width: auto;
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    SettingsScreen .settings-mode-switch:focus {
        background: $primary 12%;
        color: $foreground;
    }
    SettingsScreen .settings-theme-switch {
        height: 1;
        padding: 0;
    }
    SettingsScreen .settings-theme-switch:focus {
        background: $primary 12%;
    }
    SettingsScreen .settings-theme-segment {
        height: 1;
    }
    SettingsScreen #settings-notify-card {
        width: 1fr;
    }
    SettingsScreen .settings-pref-note {
        height: auto;
        padding: 1 1 0 1;
        color: $text-muted;
    }
    SettingsScreen .settings-card {
        height: auto;
        margin: 0 0 1 0;
        padding: 0 1;
        border: round $primary 22%;
        border-left: thick $primary;
        background: $background;
    }
    SettingsScreen .settings-token-card {
        height: auto;
        padding: 0 1;
        margin: 0;
    }
    SettingsScreen .settings-token-title {
        width: auto;
        height: 1;
        color: $text-muted;
        text-style: bold;
    }
    SettingsScreen .settings-token-note {
        height: auto;
        color: $text-muted;
    }
    SettingsScreen .settings-token-metric {
        height: 1;
        align-vertical: middle;
    }
    SettingsScreen .settings-token-used {
        width: auto;
        height: 1;
        color: $foreground;
        text-style: bold;
    }
    SettingsScreen .settings-token-total {
        width: auto;
        height: 1;
        margin-left: 1;
        color: $text-muted;
    }
    SettingsScreen .settings-token-pill {
        width: auto;
        height: 1;
        margin-left: 2;
        padding: 0 1;
        background: $primary 25%;
        color: $foreground;
    }
    SettingsScreen .settings-token-bar {
        width: 1fr;
        height: 1;
    }
    SettingsScreen .settings-token-legend {
        width: 1fr;
        height: 1;
    }
    SettingsScreen .settings-token-legend Static {
        width: 1fr;
        height: 1;
        color: $text-muted;
    }
    SettingsScreen .settings-token-legend Static:last-of-type {
        text-align: right;
    }
    SettingsScreen #settings-token-grid {
        layout: grid;
        grid-size: 2;
        grid-columns: 1fr 1fr;
        grid-gutter: 0 1;
        height: auto;
        margin: 1 0 0 0;
    }
    SettingsScreen #settings-token-grid .settings-token-card {
        margin: 0;
        height: 100%;
    }
    SettingsScreen .settings-token-row {
        width: 1fr;
        height: 1;
    }
    SettingsScreen .settings-token-row-label {
        width: 1fr;
        height: 1;
        color: $foreground;
    }
    SettingsScreen .settings-token-row-bar {
        width: 14;
        height: 1;
    }
    SettingsScreen .settings-token-row-value {
        width: 9;
        height: 1;
        text-align: right;
        color: $foreground;
    }
    SettingsScreen .settings-token-kv-line {
        width: 1fr;
        height: 1;
        margin-bottom: 1;
    }
    SettingsScreen .settings-token-kv-line:last-of-type {
        margin-bottom: 0;
    }
    SettingsScreen .settings-token-kv-key {
        width: 16;
        height: 1;
        color: $text-muted;
    }
    SettingsScreen .settings-token-kv-val {
        width: 1fr;
        height: 1;
        color: $foreground;
    }
    SettingsScreen .settings-card-head {
        height: auto;
        padding: 1 1 0 1;
    }
    SettingsScreen .settings-theme-spinner {
        width: auto;
        height: 1;
        margin-right: 1;
        color: $text-muted;
    }
    SettingsScreen .settings-card-head:hover {
        background: $primary 10%;
    }
    SettingsScreen .settings-card-head-text {
        width: 1fr;
        height: auto;
    }
    SettingsScreen .settings-server-name {
        height: 1;
        color: $foreground;
        text-style: bold;
    }
    SettingsScreen .settings-pill {
        width: auto;
        height: 1;
        padding: 0 1;
        background: $primary 25%;
        color: $text-muted;
    }
    SettingsScreen .settings-server-count {
        width: auto;
        height: 1;
        margin-right: 1;
        color: $text-muted;
    }
    SettingsScreen .settings-arrow {
        width: 2;
        height: 1;
        color: $text-muted;
    }
    SettingsScreen .settings-dot {
        width: 2;
        height: 1;
        color: $text-muted;
        background: $background;
    }
    SettingsScreen .settings-dot.-on {
        color: green;
    }
    SettingsScreen .settings-dot:focus {
        background: $primary 30%;
    }
    SettingsScreen .settings-mode {
        width: 11;
        height: 1;
        margin-left: 1;
        background: $background;
    }
    SettingsScreen .settings-item {
        height: auto;
        padding: 1 1;
    }
    SettingsScreen .settings-item-separated {
        border-top: solid $primary 15%;
    }
    SettingsScreen .settings-item-text {
        width: 1fr;
        height: auto;
    }
    SettingsScreen .settings-item-name {
        height: 1;
        color: $foreground;
        text-style: bold;
    }
    SettingsScreen .settings-item-description {
        height: auto;
        color: $text-muted;
    }
    SettingsScreen .settings-tool-list {
        display: none;
        height: auto;
    }
    SettingsScreen .settings-tool-list.-open {
        display: block;
    }
    SettingsScreen .settings-view {
        width: 3;
        height: 1;
        color: $text-muted;
        background: $background;
    }
    SettingsScreen .settings-view:hover {
        color: $accent;
    }
    SettingsScreen #settings-doc {
        display: none;
        width: 1fr;
        height: 1fr;
        padding: 2 4 1 4;
        background: $background;
    }
    SettingsScreen #settings-doc-crumbs {
        height: 1;
        color: $text-muted;
    }
    SettingsScreen #settings-doc-head {
        height: auto;
        padding: 1 0;
        border-bottom: solid $primary 18%;
    }
    SettingsScreen #settings-doc-name {
        width: auto;
        height: 1;
        color: $foreground;
        text-style: bold;
    }
    SettingsScreen #settings-doc-pill {
        width: auto;
        height: 1;
        margin-left: 1;
        padding: 0 1;
        background: $primary 25%;
        color: $text-muted;
    }
    SettingsScreen #settings-doc-status {
        width: 1fr;
        height: 1;
        margin-left: 1;
        color: $text-muted;
    }
    SettingsScreen #settings-doc-close {
        width: auto;
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    SettingsScreen #settings-doc-close:hover {
        color: $accent;
        text-style: bold;
    }
    SettingsScreen #settings-doc-scroll {
        height: 1fr;
        background: $background;
    }
    SettingsScreen #settings-doc-md {
        width: 1fr;
        height: auto;
        margin: 1 0 1 0;
        background: $background;
    }
    SettingsScreen #settings-actions {
        height: 3;
        padding: 0 2;
        align-vertical: middle;
        border-top: solid $primary 20%;
        background: $background;
    }
    SettingsScreen #settings-hint {
        width: 1fr;
        height: 1;
        color: $text-muted;
    }
    SettingsScreen #settings-close {
        width: auto;
        height: 1;
        padding: 0 2;
        color: $primary;
        text-style: bold;
    }
    SettingsScreen #settings-close:hover {
        color: $accent;
        text-style: underline;
    }
"""
