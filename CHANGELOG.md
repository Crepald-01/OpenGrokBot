# Changelog

## 1.6.0

Bug fixes and interface polish only. No new features.

**Fixes**
- **Workspace links.** A shortcut or link inside the workspace folder that points somewhere outside it made the Files page, recent files and search fail with a server error. Such links are now left out of the lists (and still cannot be opened or deleted), and Backup no longer includes files reached through a link.
- **Crafted backups.** A hand-made backup zip with backslashes or drive letters in its entry names could have written files outside the restore folder on Windows. Restore now refuses those entries.
- **Downloads with non-Latin names.** Saving a workspace file, exporting a Bot, or exporting a chat failed with a server error when the name contained Cyrillic, Chinese or other non-Latin letters. Export file names are now ASCII-safe, and workspace files stream straight from disk with the right encoded name.
- **Digest.** Token counts just under a million read "1000.0k" and now read "1.0M". The Markdown digest now says "No Bot did anything in this period" when that is true. A digest time written without a leading zero ("9:00") compared as text and could send at the wrong time; it is now compared as a time.
- **Update check.** "1.6" and "1.6.0" are now treated as the same version.
- **Bot names.** Names are cut to 40 characters before the duplicate-name check, so two long names that only differ after character 40 can no longer both be created with the same stored name.
- **Estimated cost.** Editing one price on a row that showed "free" in the other box was rejected as "not a number". "free" now counts as 0 there.
- **Quick Ask.** Pressing the shortcut again while the box is open no longer wipes what you typed. After sending it now shows "Sent to <Bot>" before closing, because the main window's confirmation is not visible when you use the global shortcut from another app.
- **Memory editor.** Removed a "Failed to disconnect" warning written to the log every time a Bot's memory tab was opened.

**Interface polish**
- Sidebar count badges are proper round pills again. They were stretched to the full row height, and were squeezed flat when the Bot list had a scrollbar. The scrollbar no longer sits on top of badges or the + button.
- The Action log table now fills the window instead of leaving a wide empty area on the right.
- Usage: the page scrolls on small windows, and the two tables are sized to their rows instead of hiding rows or leaving a blank gap.
- Settings: long options such as "Closing the window keeps the app in the system tray" were cut off at the window edge. Each now has a short label with its detail in a wrapping line underneath.
- Plugins: content lines up with the tabs and the page edge, and the scroll areas lost their extra frame.
- Files: image files have a proper picture icon (they showed a sparkle), and list separators are spaced consistently.

## 1.5.0

Five big features and five small ones. (There is no 1.4.)

**Major**
- **Files.** A new page that shows everything your Bots have saved in the shared workspace: recent files, folders, search, previews for text and images, Save a copy, Delete, and Open folder. Paths can never leave the workspace.
- **Daily digest.** What every Bot did today, yesterday, in the last 24 hours or this week: tasks, files written, sites visited, approvals, tokens. It is built from the action log (no model call, so it is instant and free). Home shows a one-line summary with a Full digest button, `/digest` works in any chat, and an optional daily notification can arrive at a time you choose.
- **Estimated cost.** Enter what your provider charges per million tokens and Usage shows the estimated spend for the week and today, per Bot and per model. Local models and `:free` models count as free, and models without a price are flagged instead of guessed. `/cost` in chat.
- **Quick Ask.** `Ctrl+J` (or `Ctrl+Alt+Space` from anywhere on your PC, even with the window closed to the tray) opens a small box: pick a Bot, type, press Enter. It goes to that Bot's chat. Slash commands work too. The global shortcut can be turned off in Settings > App.
- **Backup and restore.** Settings > App > Back up… saves your Bots, chats, memory, routines, settings and skills (optionally the workspace files) as one zip. API keys and tokens are never included. Restore… checks the file, keeps your current data as a before-restore copy, restarts the service and applies it.

**Minor**
- **Duplicate Bot.** Same job, instructions, model and limits, but a clean start: no memory, chats or access grants.
- **Update notice.** Home shows a banner when a newer release exists (one plain request to GitHub a day, nothing about you is sent; switch it off in Settings > App, or check on demand).
- **Pause all / Resume all.** From the command palette, the tray menu, or `/pauseall` and `/resumeall`.
- **Pinned Bots.** Pin a Bot to the top of the sidebar (chat menu or palette). `Ctrl+1` to `Ctrl+9` follow the order you see.
- **Match Windows theme.** A third theme choice that follows Windows' light/dark setting, live.

## 1.3.0

- **Search everything.** `Ctrl+K`, then type: besides Bots, pages and actions, it now finds words in any conversation (Bots and group chats) and in every Bot's memory, and opens the chat. Also `/search <words>` in a chat.
- **Quiet hours and Do Not Disturb.** Settings > Notifications has a quiet-hours schedule (it can cross midnight) and one-click Do Not Disturb for 1 hour, 4 hours or until morning. While it is on nothing pops up, beeps or is pushed to your phone, but every notification still lands in the Inbox. Also `/dnd 2h` and `/dnd off`, and a palette action. The sidebar shows when it is on.
- **Daily token budget per Bot.** Edit Bot > Daily budget (or `/budget 50k`). When a Bot has used its budget since midnight it stops, even in the middle of a task, and works again the next day. Routines and proactive work are skipped while a Bot is over budget. Home and `/usage` show how much each Bot has used today.
- **Export and retry.** Export any chat as a Markdown transcript (chat menu, palette, or `/export`, which saves it in the shared workspace). `/retry` sends your last message again.
- Upgrading is automatic: the database gets the new budget column the first time 1.3 starts.

## 1.2.0

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
