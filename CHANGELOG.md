# Changelog

## 1.2.0 (unreleased)

A UI and UX revamp of the desktop app.

- **Home.** A new landing page: what needs your approval, who is working, how much of the week's token budget is used, your whole team at a glance, and a live feed of recent Bot actions. The app opens on it, and `Ctrl+0` brings you back.
- **Appearance.** Settings > App now has an accent colour (six choices, tuned for both themes), a Comfortable or Compact density, and a text size. Buttons, selections and your own message bubbles all follow the accent.
- **Command palette, rebuilt.** `Ctrl+K` shows recents, groups results into Bots, Groups, Pages and Actions, matches loosely ("ibx" finds Inbox) and shows shortcut hints. New actions: switch theme, stop all running tasks, keyboard shortcuts.
- **Shortcuts.** `Ctrl+1` to `Ctrl+9` jump to a Bot, `Ctrl+0` opens Home, `Ctrl+/` lists every shortcut. Switching theme from the palette keeps you on the page you were on.
- Fixed: button text on a light-theme accent was dark on blue; contrast is now picked per accent.

## 1.1.0

- **Slash commands.** Type `/` in any chat (desktop or phone) for a list of commands: `/status`, `/model`, `/mode`, `/approve`, `/deny`, `/approvals`, `/memory`, `/remember`, `/forget`, `/skills`, `/skill`, `/routines`, `/new`, `/rename`, `/stop`, `/pause`, `/resume`, `/usage`, `/version`, `/help`. They run in the service, are never sent to the model, and answer in the chat.
- Phone setup fixes: the Mobile tab refreshes after applying, and the QR code uses your Wi-Fi address instead of a virtual adapter.

## 1.0.0

First public release.
