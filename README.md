# Budget tracker

[![tests](https://github.com/Benkendorfer/budget_tracker/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/Benkendorfer/budget_tracker/actions/workflows/tests.yml?query=branch%3Amain)

- [Budget tracker](#budget-tracker)
  - [Overview](#overview)
  - [How to run](#how-to-run)
    - [Setup](#setup)
    - [The interactive app](#the-interactive-app)
    - [The command line](#the-command-line)
    - [Searching transactions](#searching-transactions)
    - [Renaming and grouping vendors](#renaming-and-grouping-vendors)
    - [Vendor rename rules](#vendor-rename-rules)
    - [Categorising transactions](#categorising-transactions)
    - [Statistics](#statistics)
    - [Transfers between your own accounts](#transfers-between-your-own-accounts)
    - [Importing data](#importing-data)
    - [Where the data lives](#where-the-data-lives)
    - [Running the tests](#running-the-tests)
  - [Technical details](#technical-details)
  - [Database schema](#database-schema)
  - [Data protection / security](#data-protection--security)

## Overview

This is a project for tracking personal budgets. The planned features are the following:

1. [Todo] Import transaction records exported by banks or credit cards.
2. [Todo] Auto-categorize transactions based on their source, description, and amount. Allow the user to override auto-categorization, and learn from the user's inputs.
3. [Todo] Produce visualizations of historical spending and income amounts.
4. [Todo] Produce projections of future balances.
5. [Todo] Display details in a command-line interface.
6. [Todo] Display details in a graphical interface if desired.
7. [Todo] Connect to bank / credit card / broker APIs to read the user's current financial state.

## How to run

### Setup

Requires Python 3.9 or newer. From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

This installs the dependencies (`SQLAlchemy`, `textual`) and puts a `budget` command on
your path. The `[dev]` extra adds `pytest`; drop it if you do not intend to run the tests.

### The interactive app

Running `budget` with no arguments launches the full-screen TUI:

```bash
budget          # equivalent to: budget tui
```

The sidebar lists accounts, vendors, and categories — click any row to filter the
transaction table, and the totals line updates to match. Type commands into the bar at
the bottom:

| Command | Effect |
| --- | --- |
| `import` | Browse `data/to_import/`; `enter` imports the highlighted file |
| `import all` | Import every CSV in `data/to_import/` without browsing |
| `import <path>` | Import a single CSV |
| `rename <raw vendor> = <display name>` | Give one vendor a readable name (see below) |
| `rule <pattern> = <display name>` | Rename every matching vendor, now and in future imports |
| `categorize <vendor> = <category>` | Categorise that vendor's transactions by hand (see below) |
| `categorize <vendor> =` | Undo a manual category |
| `categorize rule <pattern> = <category>` | Categorise every matching vendor, now and in future imports |
| `categorize rules` | Open the rules panel (`categorize` on its own does the same) |
| `filter <text>` | Search description, vendor name, and raw vendor name |
| `filter <field>:<text>` | Search one of `description`, `vendor`, `raw` |
| `filter` | Clear the text filter |
| `stats` | Pick a time window, then see spending per category (see below) |
| `stats <window>` | Skip the picker: `stats 6m`, `stats 1 year`, `stats 2025-01-01..2025-06-30` |
| `transfers` | Pair up movements between your own accounts (`transfers reset` undoes it) |
| `rules` | Open the rules panel — both kinds of rule (`rule` on its own does the same); `escape` returns |
| `all` | Clear all filters |
| `refresh` | Reload from the database |
| `help` | Show this list in-app |
| `quit` | Exit |

Keyboard shortcuts: `ctrl+l` clears filters, `ctrl+r` refreshes, `escape` returns to the
transactions from the rules panel, and `ctrl+c` quits. On a statistics row, the right
arrow drills into that category's transactions (same as `enter`); the left arrow goes
back to the breakdown it came from, once you have drilled into one — see "Statistics"
below. The footer shows both only while they do something.

`ctrl+n` prefills a `rename` command for whichever vendor you are pointing at, and
`ctrl+t` prefills a `categorize` command for the same vendor. With the transaction table
focused, that is the vendor of the transaction under the cursor — which works even for
vendors you have already grouped, because each row remembers the raw merchant string.
Otherwise both fall back to the vendor sidebar: the active vendor filter if there is one,
else the highlighted row.

### The command line

The same operations are available as subcommands, which is handy for scripting:

```bash
# Import a CSV. With no path, pick interactively from data/to_import/.
budget import ~/Downloads/statement.csv
budget import --currency EUR ~/Downloads/statement_eur.csv
# Exports that do not name their account (see "Importing data") need --account.
budget import --account "Checking" ~/Downloads/checking.csv

# List transactions, most recent first (default limit: 50).
budget list
budget list --account "Card 8207" --limit 100
budget list --category Dining
budget list --vendor Coffee          # accepts a raw name or an override name
budget list --search cava            # substring, across all three text fields
budget list --search Kindle --search-in vendor

# Give a raw vendor string a readable display name.
budget rename "COFFEE SHOP A" "Coffee"

# Categorise a vendor by hand, or undo it.
budget categorize "COFFEE SHOP A" "Dining"
budget categorize "COFFEE SHOP A" --clear

# Categorise by pattern instead, now and on every future import.
budget category-rule add "*COFFEE*" "Dining"
budget category-rule list
budget category-rule remove "*COFFEE*"
budget category-rule apply

# Pair up money moved between your own accounts.
budget transfers --days 5
budget transfers --reset
```

`budget --help`, or `budget <subcommand> --help`, documents every flag.

### Searching transactions

`filter` narrows the table to transactions whose text matches, and the totals follow the
filter:

```text
filter cava                 # description, vendor name, or raw vendor name
filter vendor:Lyft          # just the display name
filter raw:Kindle Svcs*     # just the original merchant string
filter description:PAYROLL  # just the description
filter                      # clear it
```

Matching is a case-insensitive substring, so `shop b` finds `COFFEE SHOP B`. The three
fields differ once vendors are renamed: `raw` searches what the bank wrote, `vendor`
searches the name you gave it, and `description` searches the transaction text — so
searching for a display name like `Beanery` finds nothing under `raw`. Wildcards are not
special here; a `%` or `_` in your text is searched for literally.

The active search shows up in the status line (`[filtered: vendor~"Lyft"]`) and stacks
with the sidebar filters. `all`, or `ctrl+l`, clears everything.

On the command line the same search is `budget list --search <text>`, with
`--search-in description|vendor|raw|all`.

### Renaming and grouping vendors

Imports record the bank's raw merchant string as the vendor. `rename` points that raw
string at a readable name, and **reusing the same display name aggregates several raw
vendors into one group**:

```bash
budget rename "COFFEE SHOP A" "Coffee"
budget rename "COFFEE SHOP B" "Coffee"   # both now report under "Coffee"
```

Renames are stored separately from transactions and survive re-importing — the importer
matches vendors on the raw string and never overwrites an existing display name. Note
that the match is exact, so if your bank starts exporting a new variant of the string
(`COFFEE SHOP A #4471`), that variant arrives as a separate, un-renamed vendor. Use a
rule instead when you expect variants.

### Vendor rename rules

Sometimes, one merchant arrives as many raw vendors, like `Kindle Svcs*BY3UO9RV2`, `Kindle Svcs*BS4XF2Z70`, and so
on. A rule renames all of them at once, including any that show up in later imports:

```bash
budget rule add "Kindle Svcs*" "Kindle"    # 5 vendors updated
budget rule add "AMAZON*" "Amazon"         # catches AMAZON MKTPL*, Amazon.com*, ...
budget rule list
budget rule remove "AMAZON*"               # reverts the vendors it had named
budget rule apply                          # re-run every rule
```

In the app, the equivalent command is `rule Kindle Svcs* = Kindle`. Typing `rules` (or a
bare `rule`) replaces the transaction table with a rules panel listing every pattern, the
value it maps to, and how much it currently covers — which is the quickest way to see
whether a pattern is doing anything. Category rules (below) share the panel, so the first
column says which kind each rule is:

```text
 Kind       Pattern                     Value               Count
 vendor     Kindle Svcs*                Kindle                    5
 vendor     AMAZON*                     Amazon                   12
 category   *CAVA*                      Dining                   34
 category   RENT PAYMENT*               Housing                  12

 4 rules   17 vendors named   46 txns categorised   escape to return to transactions
```

`Count` means raw vendors for a vendor rule and transactions for a category rule — in
both cases, what that rule currently owns. A row a category rule matched but could not
take, because you had categorised it by hand, is not counted.

Press `escape` to go back to the transactions. Adding a rule while the panel is open
updates it in place. Removing a rule is CLI-only for now.

Patterns are shell-style globs (`*` and `?`) matched case-insensitively against the raw
vendor name. Note that `*` is a wildcard in the pattern even though banks often emit it
literally — which is precisely why `Kindle Svcs*` is the natural pattern for
`Kindle Svcs*BY3UO9RV2`. A pattern with no wildcard matches exactly, so use `*CAVA*` to
match anywhere in the string. When several rules match, the oldest one wins.

Rules and manual renames coexist predictably, because each vendor records which set its
name:

- **A manual `rename` always wins.** Rules skip vendors you have renamed by hand, so
  applying a broad rule can never undo a deliberate choice.
- **Rules own the vendors they name.** Re-pointing a rule to a different name updates
  them, and removing a rule reverts them to their raw names.
- **Imports apply rules automatically**, so new merchant variants are folded in without
  a manual step. `budget rule apply` is only needed if the database is changed outside
  the app.

Rules are stored, not baked in: they are re-evaluated rather than rewriting transactions,
and `raw_description` always preserves the bank's original text.

### Categorising transactions

Imports keep whatever category the bank supplied, which is often wrong and often blank.
There are two ways to fix that, and they behave differently:

```bash
budget categorize "COFFEE SHOP A" "Dining"   # by hand, this vendor's transactions
budget category-rule add "*COFFEE*" "Dining" # by pattern, now and in future imports
```

In the app the same two are `categorize COFFEE SHOP A = Dining` and
`categorize rule *COFFEE* = Dining`. `ctrl+t` prefills the first for whichever vendor you
are pointing at, so categorising a row you are looking at is one keystroke and a word.
Leaving the right-hand side blank undoes a manual category — `categorize COFFEE SHOP A =`,
or `budget categorize "COFFEE SHOP A" --clear` — the same way a bare `filter` clears the
filter.

The two coexist by the same rules vendor renames do, and each transaction records which
set its category:

- **A manual category always wins.** Rules skip transactions you categorised by hand, and
  so does the next import. Clearing one hands it back to the rules.
- **Rules own the transactions they categorise.** Re-pointing a rule updates them, and
  removing a rule clears them again. A rule overwrites the bank's own category, but never
  touches a detected transfer, so both legs of one keep reading as a transfer.
- **Rules re-apply at the end of every import**, so new transactions arrive already
  categorised. `budget category-rule apply` is only needed if the database is changed
  outside the app.

A rule pattern is a case-insensitive glob matched against **the raw merchant string or
the display name**, so `*CAVA*` keeps working after you rename the vendor, and a rule
written against a name you chose catches every raw vendor grouped under it. When several
rules match, the oldest one wins. A manual `categorize` accepts either name too: give it a
display name and the whole override group is categorised.

The important difference between the two is what happens next month. **A manual category
applies to the transactions that exist right now.** Rows imported later come in
uncategorised — the vendor is not remembered, only its transactions were changed — so use
a rule for anything recurring, and keep manual categories for one-offs and for the rows
where a rule gets it wrong.

### Statistics

`stats` opens a list of time windows — 1 month, 3 months, 6 months, 1 year, 2 years, or a
custom range — and shows what you spent per category over the one you pick, with an
average per month beside each total. `stats 6m` or `stats 2025-01-01..2025-06-30` skips
the list. `escape` returns to the transactions.

Windows end **today**, not at your most recent transaction, so a window that looks thin is
telling you the imports are stale rather than quietly hiding the gap. The status line
always spells out the dates it resolved to.

Press `enter`, or the right arrow, on a category row to see the transactions behind it.
The table comes back filtered to that category **and** to the window you were looking at,
so the rows add up to the number you just clicked rather than to everything that category
ever held. The status line says so:

```text
 85 txns [filtered: category, 2026-05-06→08-06]   net -1,234.56   out -1,234.56   in 0.00
```

The left arrow goes back to the breakdown you drilled in from, restoring whatever
category or date filter you had before — not blanking it — and puts the cursor back on
the row you drilled into. It only does this right after a drill-down: change the filters
some other way, or leave the panel, and the left arrow goes back to being an ordinary key
again. The footer shows `→ Drill down` while a stats row is highlighted and `← Back to
stats` once you have drilled in; neither status line has room left to say so, having both
already measured out to their 92-column budget with a real year of five-figure totals.

The `Uncategorised` row drills in like any other, which is the quickest way to find what
still needs a rule. `ctrl+l`, or `all`, clears the window again along with the rest of the
filters — a drill-down is not sticky.

The panel is scoped by whatever filters are already active: click an account in the
sidebar, or run `filter`, and the statistics narrow to match. Transfers are left out of
the figures, and the status line reports how many (`⇄ 43`) so the money is never missing
without explanation.

Averages are per *average* month (30.44 days) rather than per calendar month, so a window
starting mid-month is not penalised by partial months at either end.

### Transfers between your own accounts

Paying your card from your checking account produces two transactions: money leaving one
account and arriving in the other. Both are real rows, but counting them as spending and
income double-counts money that never left your control.

`budget transfers` (or `transfers` in the app) pairs them up. Two transactions match when
they have **the same amount with opposite signs**, sit in **different accounts**, and post
**within a few days** of each other — `--days` controls the window, five by default.
Paired rows are categorised as `Transfer`, share a `transfer_group_id`, and drop out of
the inflow and outflow figures. They are still listed, and the totals line says how many
were excluded:

```text
1939 txns (110 transfers excluded)   net -995.04   out -165,541.56   in 164,546.52
```

Detected transfers are **greyed out and flagged with `⇄`** in both the app and
`budget list`, and their amounts lose the red/green colouring:

```text
2026-07-20  POS-: MTA*NYCT PAYGO       MTA                  Auto & Transport     -3.00
2026-07-16  ⇄ CARD PAYMENT - MOBILE    Card Payment         Transfer         -4,673.24
```

Without the marker a transfer looks like ordinary spending that is mysteriously missing
from the totals. Note that a row can carry the category `Transfer` from your bank's own
export without being a detected transfer — a wire to an outside broker, say. Only the
`⇄` rows are excluded from the figures.

Detection runs in three places: **automatically at the end of every import** (new rows are
often the second leg of something already stored), and on demand via `budget transfers` or
the app's `transfers` command. It does not run on start-up or refresh. Repeat runs only
look at unpaired transactions, so they are cheap and safe.

Matching is by amount and date alone, so two unrelated transactions of the same size a
few days apart in different accounts can be paired by mistake. Two things guard against
that: a category you set by hand is never overwritten, and `budget transfers --reset`
un-pairs everything and restores the categories detection assigned, leaving manual ones
alone. If you get spurious matches, a smaller `--days` window is the first thing to try.

### Importing data

Every bank lays its CSV out differently, so the first time you import an unfamiliar
layout the importer works out the mapping from the header and a sample of rows, then
asks about anything it could not settle. This works the same way from either interface.

**In the app**, `import` lists the files in `data/to_import/` with what stands in the way
of each one:

```text
 File                                Rows  Status
 statement-june.csv                   516  cards
 new-bank-export.csv                  387  needs setup

 2 file(s), 1 ready   enter to import   escape to return to transactions
```

Press `enter` on a file. A ready one imports straight away; one needing setup starts the
walkthrough, which asks one question at a time — pick a column by pressing `enter` on it,
or type your answer in the command bar. `escape` cancels without saving anything. Once
the layout is learned the import continues by itself, and that file type never asks
again.

**From the command line** the same walkthrough runs as prompts:

```text
$ budget import ~/Downloads/statement.csv
'statement.csv' does not match any format you have defined yet.
Name for this layout [statement]: current

Worked out from the header:
  posted_date_column   Posting Date
  description_column   Description
  amount_column        Amount
  date_formats         ['%m/%d/%Y']
  (no account column — imports of this layout will need --account)

Saved layout 'current'. Future imports of this shape are automatic.
```

Layouts are saved in the database, **not in the source tree**, so the repository never
records which institutions you bank with. `budget format list` shows what has been
learned, `budget format export` prints the definitions as JSON, and
`budget format remove <name>` forgets one.

What it works out for itself: which columns hold the dates, description, category, and
account; whether the amount is one signed column or a `Debit`/`Credit` pair; the date
format, tried against real values from the file; and which columns identify a row for
deduplication (a unique id column if there is one, otherwise the mapped fields).

What it asks about, rather than guess:

- A column it cannot place — it lists the header and you pick by number.
- **Ambiguous dates.** If every sampled day is 12 or lower, `01/02/2026` could be
  January 2nd or February 1st, and guessing would silently mis-date transactions.
- **An account-name prefix.** A card column gives `8207`; only you know it should read
  `Card 8207`. Getting this wrong would create a second account for the same card.

A `Debit` is a charge (stored negative) and a `Credit` is a payment or refund (positive);
a signed amount column already uses that convention. Files are read as UTF-8 or
Windows-1252, so accented merchant names import cleanly. Re-importing is safe: each row
gets a stable hash and rows already present are skipped rather than doubled.

Some exports do not identify their account — a checking file and a savings file from the
same institution can be identical apart from their rows. Those layouts require
`--account`, and nothing is guessed:

```bash
budget import --account "Checking" ~/Downloads/checking.csv
budget import --account "Savings"  ~/Downloads/savings.csv
```

Passing `--account` for a layout that *does* carry an account column overrides the
derived name. `--format` forces a known layout if detection ever picks wrong.

Dropping statements into `data/to_import/` lets you import them without typing paths —
`budget import` prompts you to choose one, and the app's `import` command browses them.
`import all` in the app imports every recognised file in one go and reports the ones that
need setup rather than stopping.

### Where the data lives

The SQLite database is created on first run at `data/budget.db`, and the tables are
created automatically — there is no migration step. Set the `BUDGET_DB` environment
variable to point at a different file, which is useful for keeping a scratch database
separate from your real one:

```bash
BUDGET_DB=/tmp/scratch.db budget import ~/Downloads/statement.csv
```

### Running the tests

```bash
pytest
```

The suite covers CSV importing, vendor overrides, categorisation, and the TUI's shortcuts
and panels. It builds a temporary database per test, so it never touches `data/budget.db`.

## Technical details

The project is mostly written in Python, preserving the possibility of using C++ for hot loops.

The software maintains a local SQL database for transaction records. The project uses `SQLAlchemy` to handle SQL easily within Python, and to maintain the possibility of future cloud hosting of the SQL database if desired.

For now, we use a command-line interface for user interactions. In the future, we hope to expand to a graphical interface.

## Database schema

Conventions:

- Money is stored as an integer count of the currency's minor unit (`value_minor`,
  signed, negative = outflow) to avoid floating-point rounding. The number of minor
  units per major unit is given by `currency.decimal_places` (e.g. 2 for USD, 0 for JPY).
- `budget.value_minor` and `recurring.value_minor` use the same signed convention as
  transactions (negative = expense/outflow, positive = income/inflow).
- The application has a single **base currency** (`app_config.base_currency_id`), which
  the user can change at any time. Budgets are always expressed in the base currency
  (there is no per-budget currency).
- `exchange_rate` records one row per currency per day: `rate` is the value of 1 unit of
  `currency_id` expressed in `base_currency_id` on `rate_date`. `base_currency_id` is
  stored on each row so changing the base currency never corrupts historical rates; the
  base currency itself has an implicit rate of 1. Non-base transactions are converted to
  base **at report time** using the rate on their `posted_date` — converted amounts are
  not stored, so all reports reflect the current base currency.
- Dates/times are stored in UTC as ISO-8601.
- A transaction's `category_id` is nullable (NULL = uncategorized). When
  `transaction_split` rows exist, the parent's `category_id` is NULL and the splits'
  `value_minor` sum to the parent's `value_minor`.
- `import_id` is nullable — NULL means the transaction was entered manually or synced
  from an API rather than imported from a file.
- Transfers between your own accounts share a `transfer_group_id` so both legs can be
  excluded from spending/income totals (avoids double-counting).
- `raw_description` preserves the bank's original text; `description` may be
  cleaned/normalized.
- Each transaction points to a `vendor` (the raw merchant string seen in the import).
  `vendor.vendor_name_id` is nullable: NULL means no override, so the raw `vendor.name`
  is the display name. Setting it points the vendor at a `vendor_name` row, which both
  gives a readable name and lets several raw vendors aggregate under one name. The
  **effective vendor name** (the override if present, else the raw name) is what the UI
  filters and groups by.
- `category_source` records how the category was assigned (`manual` / `rule` / `unset`);
  only `manual` labels are treated as ground truth when learning new rules.
  `categorized_by_rule_id` records which rule fired (NULL if none).
- Rules are applied in ascending `priority` order (most specific first) and skipped when
  `is_enabled` is false.
- Enum-like text columns use a fixed vocabulary, enforced with CHECK constraints:
  `budget.period` & `recurring.cadence` (`weekly`/`monthly`/`quarterly`/`yearly`),
  `rule.match_field` (`description`/`amount`/`account`), `rule.match_type`
  (`contains`/`equals`/`regex`), `category_source` (`manual`/`rule`/`unset`).
- Lookup `value` columns are UNIQUE (`account_type`, `currency`, `tag`, `vendor_name`,
  marked `UK`); `vendor.name` is UNIQUE; `category` is unique on `(parent_id, value)`.
  `exchange_rate` is unique on `(currency_id, base_currency_id, rate_date)`.
- Lookup tables (`account_type`, `currency`, `tag`, `vendor`, `vendor_name`) and pure
  join tables (`transaction_tag`) omit `created_at`/`updated_at`.

```mermaid
%%{init: {'theme':'base', 'themeVariables':{'background':'#ffffff','lineColor':'#ff5c5c'}}}%%
erDiagram
    account ||--o{ transaction : has
    account_type ||--o{ account : classifies
    currency ||--o{ account : denominates
    category |o--o{ transaction : classifies
    category |o--o{ category : "parent of"
    currency ||--o{ transaction : has
    category ||--o{ rule : "matched by"
    transaction ||--o{ transaction_tag : has
    tag ||--o{ transaction_tag : labels
    transaction ||--o{ transaction_split : "split into"
    category ||--o{ transaction_split : categorizes
    category ||--o{ budget : budgets
    account ||--o{ recurring : schedules
    category |o--o{ recurring : classifies
    currency ||--o{ recurring : denominates
    account ||--o{ import : "source of"
    import |o--o{ transaction : "imported in"
    rule |o--o{ transaction : categorized
    account ||--o{ balance_snapshot : "snapshot of"
    currency ||--o| app_config : "base of"
    currency ||--o{ exchange_rate : "priced"
    currency ||--o{ exchange_rate : "quoted in"
    vendor |o--o{ transaction : "vendor of"
    vendor_name |o--o{ vendor : overrides

    account_type {
        int id PK
        str value UK
    }
    account {
        int id PK
        str name
        int account_type_id FK
        int currency_id FK
        int opening_balance_minor
        date opening_date
        datetime created_at
        datetime updated_at
    }
    transaction {
        int id PK
        int account_id FK
        int category_id FK
        int currency_id FK
        int import_id FK
        int categorized_by_rule_id FK
        int vendor_id FK
        date posted_date
        str description
        str raw_description
        int value_minor
        int transfer_group_id
        str category_source
        str import_hash UK
        datetime created_at
        datetime updated_at
    }
    vendor {
        int id PK
        str name UK
        int vendor_name_id FK
    }
    vendor_name {
        int id PK
        str value UK
    }
    currency {
        int id PK
        str value UK
        str symbol
        int decimal_places
    }
    category {
        int id PK
        int parent_id FK
        str value
        datetime created_at
        datetime updated_at
    }
    rule {
        int id PK
        int category_id FK
        str match_field
        str match_type
        str pattern
        int priority
        bool is_enabled
        datetime created_at
        datetime updated_at
    }
    tag {
        int id PK
        str value UK
    }
    transaction_tag {
        int transaction_id PK, FK
        int tag_id PK, FK
    }
    transaction_split {
        int id PK
        int transaction_id FK
        int category_id FK
        int value_minor
    }
    budget {
        int id PK
        int category_id FK
        int value_minor
        str period
        date start_date
        date end_date
        datetime created_at
        datetime updated_at
    }
    recurring {
        int id PK
        int account_id FK
        int category_id FK
        int currency_id FK
        int value_minor
        str description
        str cadence
        date next_date
        date end_date
        bool is_active
        datetime created_at
        datetime updated_at
    }
    import {
        int id PK
        int account_id FK
        str source_file
        int row_count
        datetime imported_at
    }
    balance_snapshot {
        int id PK
        int account_id FK
        date snapshot_date
        int balance_minor
    }
    app_config {
        int id PK
        int base_currency_id FK
        datetime updated_at
    }
    exchange_rate {
        int id PK
        int currency_id FK
        int base_currency_id FK
        date rate_date
        decimal rate
    }
```

## Data protection / security

All user data is stored in the `data/` directory, which is `.gitignored`. Within that directory, all user data is stored locally.
