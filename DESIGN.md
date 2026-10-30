# DESIGN.md: Social Media Post Scheduler UI/UX System

A calm, fast, highly readable personal publishing environment for a solo developer in India scheduling across Telegram, Discord, and upcoming networks.

---

## 1. Design Principles

1. **Calm, Dense Utility**: Built for daily personal workflow. No artificial greetings, celebratory animations, or oversized vanity cards; metrics are quiet numbers and lists are instantly scannable.
2. **Single Focal Centerpiece**: Visual emphasis is concentrated on the **Composer & Live Preview**; everything else (Dashboard, Calendar, Logs) stays structured, clean, and restrained.
3. **Explicit State Over Color Alone**: Every status conveys certainty through a dedicated inline icon and text badge (`Scheduled`, `Sending`, `Posted`, `Failed`), never ambiguous color dots.
4. **Zero-Latency System Native**: 100% self-hosted, zero external CDNs, zero JavaScript frameworks. Lightning-fast response, keyboard-accessible, and respectful of system themes and reduced motion.

---

## 2. Color Tokens

### Light Theme
- `--bg-canvas`: `#f8f9fa` (Cool neutral studio canvas)
- `--bg-surface`: `#ffffff` (Crisp container background)
- `--border-subtle`: `#e5e7eb` (Subtle 1px structural separator)
- `--text-primary`: `#111827` (Deep charcoal black for maximum legibility)
- `--text-secondary`: `#6b7280` (Muted slate for secondary metadata and timestamps)
- `--accent`: `#1d4ed8` (Royal blue for primary buttons, active toggles, and focus rings)

#### Status Tokens (Light)
- `--status-scheduled`: `#2563eb` / Surface: `#eff6ff` (Calm blue)
- `--status-sending`: `#b45309` / Surface: `#fef3c7` (Warm amber)
- `--status-posted`: `#15803d` / Surface: `#f0fdf4` (Forest green)
- `--status-failed`: `#b91c1c` / Surface: `#fef2f2` (Crimson red)

---

### Dark Theme
- `--bg-canvas`: `#111317` (Deep graphite, not pitch black)
- `--bg-surface`: `#1a1d24` (Subtly elevated charcoal card surface)
- `--border-subtle`: `#2b303c` (Quiet structural boundary)
- `--text-primary`: `#f3f4f6` (Warm off-white for comfortable contrast)
- `--text-secondary`: `#9ca3af` (Muted silver for secondary metadata)
- `--accent`: `#3b82f6` (Vibrant cobalt blue)

#### Status Tokens (Dark)
- `--status-scheduled`: `#60a5fa` / Surface: `#1e293b`
- `--status-sending`: `#fbbf24` / Surface: `#332514`
- `--status-posted`: `#34d399` / Surface: `#0f291e`
- `--status-failed`: `#f87171` / Surface: `#361414`

---

## 3. Typography & Hierarchy

### Font Families
- **Primary UI Text**: System font stack (`system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif`). Zero download latency, sharp platform-native rendering.
- **Monospace Elements** (IDs, character counters, UTC/IST raw dates): `ui-monospace, "Cascadia Code", "Segoe UI Mono", Menlo, Consolas, monospace`.

### Type Scale
- `11px` (`0.6875rem`): Platform byte indicators, caps badge labels, keyboard shortcut hints.
- `13px` (`0.8125rem`): Metadata, timestamps, status badge text, audit log message bodies.
- `14px` (`0.875rem`): Body copy, table cells, form labels, navigation links.
- `16px` (`1.0rem`): Input fields, live preview text, modal titles.
- `20px` (`1.25rem`): Section titles, subheadings.
- `24px` (`1.5rem`): Page titles (`New post`, `Dashboard`, `Calendar`).

---

## 4. Layout Architecture & ASCII Wireframes

### App Shell (Desktop & Mobile)

```text
DESKTOP SHELL (>= 768px):
+----------------------------------------------------------------------------------+
| [Logo/Brand]           | Page Title / Actions                     [Theme Toggle] |
+------------------------+---------------------------------------------------------+
| [ + New Post ]         |                                                         |
|                        |                                                         |
| - Dashboard            |                       MAIN CONTENT                      |
| - Calendar             |                                                         |
| - Posts Archive        |                                                         |
|                        |                                                         |
| Status: Poller Active  |                                                         |
| IST: 00:20:00          |                                                         |
+------------------------+---------------------------------------------------------+

MOBILE SHELL (< 768px):
+----------------------------------------------------------------------------------+
| [Brand]                              Title                        [Theme Toggle] |
+----------------------------------------------------------------------------------+
|                                                                                  |
|                                    CONTENT                                       |
|                                                                                  |
+----------------------------------------------------------------------------------+
|   [Dashboard]           [ + New Post ]         [Calendar]         [Archive]      |
+----------------------------------------------------------------------------------+
```

---

### Dashboard Wireframe

```text
+----------------------------------------------------------------------------------+
| Dashboard                                                      [+ New post button]|
+----------------------------------------------------------------------------------+
| [!] FAILED POSTS (Only rendered when status='failed' targets exist)               |
|  * Post #14 (Telegram): "Chat not found"  -- Attempt 3/3       [ Retry now button ]|
+----------------------------------------------------------------------------------+
| STATS ROW:   [3 Scheduled]       [12 Posted]       [1 Failed]                    |
+----------------------------------------------------------------------------------+
| UP NEXT (Next 5 Scheduled Posts)                                                 |
|  * Tomorrow 09:00 AM (in 8 hours)  | Telegram, Discord  | "Product launch text"  |
|  * Tomorrow 06:30 PM (in 17 hours) | Telegram           | "Evening insights"     |
+----------------------------------------------------------------------------------+
| RECENT ACTIVITY                                                                  |
|  * [Posted]    Today 11:30 PM      | Telegram           | "Weekly recap"         |
|  * [Posted]    Today 08:00 PM      | Discord            | "Community update"     |
|  * [Failed]    Today 06:15 PM      | Telegram           | "Broadcast ping"       |
+----------------------------------------------------------------------------------+
```

---

### Composer (New Post) Wireframe

```text
+----------------------------------------------------------------------------------+
| Create Post                                                    [Ctrl + Enter]    |
+------------------------------------------------+---------------------------------+
| LEFT COLUMN: FORM EDITING                      | RIGHT COLUMN: LIVE PREVIEW      |
|                                                |                                 |
| Platform Targets:                              | [Telegram Tab]   [Discord Tab]  |
| [X] Telegram   [X] Discord                     | +-----------------------------+ |
|                                                | | Preview: Telegram Channel   | |
| Title (optional):                              | | *Title Preview*             | |
| [ Enter headline or title...                 ] | |                             | |
|                                                | | Post body preview rendered  | |
| Post Body:                                     | | with linebreaks and text.   | |
| [ Write your post here...                    ] | |                             | |
| [                                            ] | |                             | |
| [                                            ] | +-----------------------------+ |
|                                                | Platform limits:                |
| Character counter:                             | Telegram: 240 / 4096            |
| Telegram: 240/4096 (OK) | Discord: 240/2000    | Discord:  240 / 2000 (OK)       |
|                                                +---------------------------------+
| Schedule Time:                                                                   |
| Presets: [ Post now ] [ In 1 hour ] [ Tonight 8 PM ] [ Tomorrow 9 AM ]           |
| Custom:  [ Date & Time Picker (IST) ]                                            |
| Result:  "Will post Fri, 30 Oct at 8:00 PM IST"                                  |
|                                                                                  |
| [ Schedule post button ]                                                         |
+----------------------------------------------------------------------------------+
```

---

### Calendar View Wireframe

```text
+----------------------------------------------------------------------------------+
| Calendar                                    [ < Prev ]  [ Today ]  [ Next > ]    |
| October 2026                                                                     |
+----------------------------------------------------------------------------------+
| Mon        | Tue        | Wed        | Thu        | Fri        | Sat    | Sun    |
+------------+------------+------------+------------+------------+--------+--------+
| 26         | 27         | 28         | 29         | 30 (Today) | 31     | 1      |
|            |            |            | 2 Posts    | 1 Post     |        |        |
|            |            |            | [TG] 08:00 | [DC] 20:00 |        |        |
+----------------------------------------------------------------------------------+
| SELECTED DAY DETAILS: Friday, 30 October 2026                                    |
|  * 08:00 PM IST | [Discord]  | [Scheduled] | "Community update broadcast"        |
+----------------------------------------------------------------------------------+
```

---

### Post Detail Wireframe

```text
+----------------------------------------------------------------------------------+
| < Back to Dashboard                  Post #12 Details                            |
| Title: "Simultaneous Cross-Platform Broadcast"                                    |
| Created: 30 Oct 2026, 12:10 AM IST                      [ Copy Post Text ]       |
+----------------------------------------------------------------------------------+
| TARGET DISPATCH STATUS:                                                          |
| Platform  | Status     | Scheduled (IST)      | Attempts | Message ID | Action   |
| Telegram  | [Posted]   | 30 Oct 2026 00:12:00 | 1        | #11        | -        |
| Discord   | [Posted]   | 30 Oct 2026 00:12:00 | 1        | #1565434.. | -        |
+----------------------------------------------------------------------------------+
| EXECUTION & AUDIT TIMELINE:                                                      |
| [00:12:00 IST] INFO    Target #12 claimed for dispatch                           |
| [00:12:01 IST] INFO    Successfully published to telegram. Message ID: 11        |
| [00:12:01 IST] INFO    Target #13 claimed for dispatch                           |
| [00:12:02 IST] INFO    Successfully published to discord. Message ID: 1565434... |
+----------------------------------------------------------------------------------+
```

---
*End of Design System specification.*

