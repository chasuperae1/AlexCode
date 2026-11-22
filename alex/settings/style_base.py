"""设置界面的基础外壳 CSS。"""

SETTINGS_BASE_CSS = """
    SettingsScreen {
        align: center middle;
        background: $background;
    }
    SettingsScreen #settings-panel {
        width: 100%;
        height: 100%;
        background: $background;
    }
    SettingsScreen #settings-header {
        height: 4;
        padding: 1 2 0 2;
        align-vertical: middle;
        border-bottom: solid $primary 25%;
        background: $background;
    }
    SettingsScreen #settings-title {
        width: 1fr;
        height: 1;
        color: $foreground;
        text-style: bold;
    }
    SettingsScreen #settings-main {
        height: 1fr;
    }
    SettingsScreen #settings-workspace {
        width: 1fr;
        height: 1fr;
    }
    SettingsScreen #settings-nav {
        width: 100%;
        height: 3;
        padding: 0 2;
        align-vertical: middle;
        border-bottom: solid $primary 20%;
        background: $background;
    }
    SettingsScreen #settings-heading {
        width: auto;
        height: 1;
        padding: 0 2 0 0;
        color: $text-muted;
        text-style: bold;
    }
    SettingsScreen .settings-option {
        width: auto;
        height: 1;
        padding: 0 2;
        color: $foreground;
    }
    SettingsScreen .settings-option:hover {
        color: $accent;
        text-style: bold;
    }
    SettingsScreen .settings-option-active {
        color: $foreground;
        background: $primary 35%;
        text-style: bold;
    }
    SettingsScreen #settings-content {
        width: 1fr;
        height: 1fr;
        padding: 2 4;
        background: $background;
        scrollbar-size-vertical: 0;
    }
    SettingsScreen #settings-content.-tokens {
        padding: 1 4 0 4;
    }
    SettingsScreen #settings-content.-tokens #settings-section-header {
        margin-bottom: 0;
    }
    SettingsScreen #settings-section-header {
        height: auto;
        margin-bottom: 1;
    }
    SettingsScreen #settings-section-heading {
        width: 1fr;
        height: auto;
    }
    SettingsScreen #settings-section-title {
        height: auto;
        color: $foreground;
        text-style: bold;
    }
    SettingsScreen #settings-section-subtitle {
        height: auto;
        color: $text-muted;
    }
    SettingsScreen #settings-badges {
        width: auto;
        height: auto;
    }
    SettingsScreen .settings-badge {
        width: auto;
        height: 3;
        margin-left: 1;
        padding: 0 1;
        border: round $primary 35%;
        color: $text-muted;
    }
    SettingsScreen #settings-body {
        width: 1fr;
        height: auto;
        color: $foreground;
    }
    SettingsScreen #settings-memory {
        display: none;
        width: 1fr;
        height: auto;
    }
    SettingsScreen #settings-memory-stats {
        layout: grid;
        grid-size: 3;
        grid-columns: 1fr 1fr 1fr;
        grid-gutter: 0 1;
        height: auto;
        margin-bottom: 1;
    }
    SettingsScreen .settings-memory-stat {
        width: 1fr;
        height: 100%;
        padding: 1 2;
        border: round $primary 22%;
        background: $background;
    }
"""
